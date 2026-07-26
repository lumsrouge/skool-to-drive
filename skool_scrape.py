"""
Standalone Skool lesson extractor -- no browser automation, no AI in the loop.

Pulls a Skool classroom lesson's text, links, video, main-post GIF, comments,
and gated resource files, then renders the same Skool-dark HTML snapshot the
skool-to-drive skill produces by hand. Authenticates by reading the user's
Firefox Skool session cookie (browser_cookie3) -- Chrome/Edge cookies are
DPAPI-encrypted on Windows and can't be read this way.

Comments and gated-file downloads sit behind an AWS WAF JS challenge on
api2.skool.com that a plain `requests` session cannot solve. Those two pieces
are fetched by a one-time authenticated browser call (see
fetch_browser_data.js / SKILL.md "Fast path") and handed to this script as a
JSON file via --browser-data. Everything else runs standalone.

Usage:
    python3 skool_scrape.py "<lesson-url>" [--dest "<Drive subfolder>"] [--browser-data browser_data.json]
"""
import argparse
import base64
import html
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

try:
    import browser_cookie3
except ImportError:
    browser_cookie3 = None

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

AVATAR_PALETTE = [
    "#4d7ea8", "#3f9e6a", "#c9822f", "#8a5fc4",
    "#c4507a", "#4f9b9b", "#b0574c", "#5a7fd6",
]

MENTION_RE = re.compile(r"\[@([^\]]+)\]\(obj://user/[^)]+\)")
LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
BARE_URL_RE = re.compile(r"(?<![\"'>])\b(https?://[^\s<]+)")
# Skool stores markdown-escaped punctuation in `content` (e.g. "\(" so a literal
# paren doesn't get parsed as link syntax) -- verified live 2026-07-11, the raw
# API response for one comment contains literal backslash+paren bytes.
MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+.!-])")

TEXT_MIME = {
    ".json": "application/json", ".py": "text/x-python", ".txt": "text/plain",
    ".md": "text/plain", ".csv": "text/csv", ".js": "text/javascript",
    ".yaml": "text/yaml", ".yml": "text/yaml",
}

HTTP_TIMEOUT = 30
HTTP_RETRIES = 3


def http_get(url, session=None, timeout=HTTP_TIMEOUT, **kw):
    """Bounded-retry GET with a timeout. Every download here used a bare
    requests.get() with no timeout, which is survivable for one lesson but not
    for a whole-course bulk run -- a single hung connection would stall all 107
    lessons indefinitely. Retries only on network errors and 5xx; a 4xx is a
    real answer and comes straight back to the caller."""
    getter = session.get if session is not None else requests.get
    last = None
    for attempt in range(HTTP_RETRIES):
        try:
            r = getter(url, timeout=timeout, **kw)
            if r.status_code < 500:
                return r
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < HTTP_RETRIES - 1:
            time.sleep(1.5 * (attempt + 1))
    raise requests.RequestException(f"GET {url} failed after {HTTP_RETRIES} attempts ({last})")


# ---------------------------------------------------------------- auth

def get_session():
    if browser_cookie3 is None:
        sys.exit("Missing dependency: pip install browser_cookie3 requests")
    try:
        cj = browser_cookie3.firefox(domain_name="skool.com")
    except Exception as e:
        sys.exit(f"Could not read Firefox cookies ({e}). Log into skool.com in Firefox first.")
    names = {c.name for c in cj}
    if "auth_token" not in names:
        sys.exit("No Skool auth_token cookie found in Firefox. Log into skool.com in Firefox first.")
    s = requests.Session()
    s.cookies = cj
    s.headers.update({"User-Agent": USER_AGENT})
    return s


# ---------------------------------------------------------------- fetch

def parse_lesson_url(url):
    m = re.search(r"skool\.com/([^/]+)/classroom/([a-zA-Z0-9]+)\?.*?\bmd=([a-f0-9]+)", url)
    if not m:
        sys.exit(f"Couldn't parse group/course/md from URL: {url}")
    return m.group(1), m.group(2), m.group(3)


def fetch_build_id(session, group, course):
    r = session.get(f"https://www.skool.com/{group}/classroom/{course}")
    r.raise_for_status()
    m = re.search(r'"buildId":"([^"]+)"', r.text)
    if not m:
        sys.exit("Couldn't find buildId on classroom page (are you logged in / is the URL right?).")
    return m.group(1)


def fetch_lesson_json(session, build_id, group, course, md):
    url = (f"https://www.skool.com/_next/data/{build_id}/{group}/classroom/{course}.json"
           f"?md={md}&group={group}&course={course}")
    r = session.get(url, headers={"x-nextjs-data": "1"})
    r.raise_for_status()
    return r.json()["pageProps"]


def fetch_lesson_json_retrying(session, build_id, group, course, md):
    """Returns (page_props, build_id). Skool's buildId rotates whenever they
    deploy, which 404s the _next/data endpoint for every remaining lesson of a
    long bulk run. Re-fetch the buildId once and retry; returns the (possibly
    new) build_id so the caller can keep using it."""
    try:
        return fetch_lesson_json(session, build_id, group, course, md), build_id
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code != 404:
            raise
        fresh = fetch_build_id(session, group, course)
        if fresh == build_id:
            raise
        print(f"  buildId rotated {build_id} -> {fresh}, retrying")
        return fetch_lesson_json(session, fresh, group, course, md), fresh


def find_module_node(course_tree, md):
    def walk(o):
        if isinstance(o, dict):
            if o.get("id") == md:
                return o
            for v in o.values():
                hit = walk(v)
                if hit:
                    return hit
        elif isinstance(o, list):
            for v in o:
                hit = walk(v)
                if hit:
                    return hit
        return None
    return walk(course_tree)


def find_pinned_posts(page_props):
    """A module can have MORE THAN ONE pinned post underneath it, not just one --
    each is its own distinct discussion with its own comments/attachments and
    must stay encapsulated in its own card, never flattened together."""
    posts = page_props.get("pinnedPosts")
    if isinstance(posts, list) and posts:
        return [p.get("post") for p in posts if p.get("post")]
    return []


def parse_attachment_ids(attachments_raw):
    """Post-level `metadata.attachments` is a comma-separated string of file IDs
    (verified live 2026-07-11 on a lesson with 4 screenshot PNGs) -- not a JSON
    array, and not limited to a single GIF as originally assumed."""
    if not attachments_raw or not isinstance(attachments_raw, str):
        return []
    return [a.strip() for a in attachments_raw.split(",") if a.strip()]


def resolve_attachments(attachment_ids, attachments_info):
    """`attachments_info` comes pre-resolved from the browser-fetch step
    (api2.skool.com/files is WAF-gated) as a list of {id, file_name, content_type,
    read_url}. read_url is a standalone public assets.skool.com link -- downloads
    fine via plain requests, no cookie. Handles any mix of GIF/PNG/JPG attachments."""
    info_by_id = {a["id"]: a for a in (attachments_info or [])}
    out = []
    for aid in attachment_ids:
        info = info_by_id.get(aid)
        if not info or not info.get("read_url"):
            print(f"  attachment {aid} not in --browser-data (rerun the browser-fetch step)")
            continue
        resp = http_get(info["read_url"])
        if resp.status_code != 200:
            continue
        out.append((info.get("file_name") or f"{aid}.bin", resp.content))
    return out


def resolve_video_from_data(video_links_data_raw):
    """Post-level `videoLinksData` is a JSON-string list already carrying
    url/thumbnail/title/len_ms -- no API call needed."""
    if not video_links_data_raw:
        return None
    data = json.loads(video_links_data_raw) if isinstance(video_links_data_raw, str) else video_links_data_raw
    if not data:
        return None
    v = data[0]
    len_ms = v.get("len_ms") or 0
    mins, secs = divmod(int(len_ms / 1000), 60)
    provider = "loom" if "loom.com" in (v.get("url") or "") else ("youtube" if "youtu" in (v.get("url") or "") else "other")
    return {
        "kind": provider,
        "watch_url": v.get("url"),
        "thumb_primary": v.get("thumbnail"),
        "thumb_fallback": v.get("thumbnail"),
        "title": v.get("title"),
        "note": f"{mins}:{secs:02d}" if len_ms else None,
    }


def resolve_skool_video(page_props, meta, lesson_url):
    """Skool-HOSTED video (Mux-backed), which has no `videoLink` at all.

    Found 2026-07-26: 91 of 107 lessons in a real classroom carry an empty
    `videoLink` and a `videoId` instead, so a videoLink-only implementation
    dropped the video from 89 lessons without a trace. The playable stream is
    signed and short-lived, but `pageProps.video.thumbnailUrl` is a plain public
    assets.skool.com link (same as inline body images -- no cookie, no WAF), and
    `pageProps.video` belongs to the requested `md` (its `id` matches
    `metadata.videoId`, verified across all 91).

    The video itself stays un-downloaded, per the standing video policy: link to
    the live lesson (it's paid-gated, so the link is the honest archive) and keep
    a local poster frame so the page still looks right offline.
    """
    v = (page_props or {}).get("video") or {}
    vid = (meta.get("videoId") or "").strip()
    if not vid or v.get("id") != vid or not v.get("thumbnailUrl"):
        return None
    len_ms = v.get("duration") or meta.get("videoLenMs") or 0
    mins, secs = divmod(int(len_ms / 1000), 60)
    # Prefer Mux's rendition of the same frame: assets.skool.com serves a
    # full-resolution PNG (~1.25 MB), Mux the identical frame as a ~107 KB JPEG
    # -- 12x smaller, and this is downloaded once per lesson across a whole
    # course (91 lessons => ~114 MB vs ~10 MB). The thumbnail token is
    # long-lived, and we copy the bytes locally anyway, so expiry is moot.
    urls = []
    if v.get("playbackId") and v.get("thumbnailToken"):
        urls.append(f"https://image.mux.com/{v['playbackId']}/thumbnail.jpg?token={v['thumbnailToken']}")
    urls.append(v["thumbnailUrl"])
    return {
        "kind": "skool",
        "watch_url": lesson_url,
        "thumb_primary": urls[0],
        "thumb_fallback": None,
        "thumb_download_urls": urls,  # localised by localize_video_thumb()
        "title": meta.get("title"),
        "note": f"{mins}:{secs:02d}" if len_ms else None,
    }


def localize_video_thumb(video, lesson_dir):
    """Save the poster frame next to the HTML so the page renders offline and
    doesn't depend on a remote URL staying alive. Falls back through the
    candidate URLs so a Mux hiccup degrades to the assets.skool.com PNG rather
    than losing the video card entirely."""
    if not video:
        return video
    for url in video.get("thumb_download_urls") or []:
        fname = download_public_image(url, lesson_dir, "video-thumbnail")
        if fname:
            video["thumb_primary"] = f"./{fname}"
            video["thumb_fallback"] = None
            return video
    return video


def resolve_video(link):
    """Fallback for a bare URL (module-level videoLink) with no rich videoLinksData.
    Returns dict: kind, thumb_url, watch_url, title, duration_note (or None)."""
    if not link:
        return None
    yt = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{6,})", link)
    if yt:
        vid = yt.group(1)
        return {
            "kind": "youtube",
            "watch_url": link,
            "thumb_primary": f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
            "thumb_fallback": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "title": None,
            "note": None,
        }
    if "loom.com" in link:
        try:
            r = requests.get("https://www.loom.com/v1/oembed", params={"url": link}, timeout=15)
            r.raise_for_status()
            d = r.json()
            mins = int(d.get("duration", 0) // 60)
            secs = int(d.get("duration", 0) % 60)
            return {
                "kind": "loom",
                "watch_url": link,
                "thumb_primary": d.get("thumbnail_url"),
                "thumb_fallback": d.get("thumbnail_url"),
                "title": d.get("title"),
                "note": f"{mins}:{secs:02d}",
            }
        except Exception:
            return {"kind": "loom", "watch_url": link, "thumb_primary": None,
                     "thumb_fallback": None, "title": None, "note": None}
    return {"kind": "other", "watch_url": link, "thumb_primary": None,
            "thumb_fallback": None, "title": None, "note": None}


# ---------------------------------------------------------------- content rendering

def render_content(text):
    """Plain-string markdown-ish content: mentions, [text](url) links, bare URLs. Used
    for pinned-post `content` and all comments."""
    if not text:
        return ""
    text = html.escape(text, quote=False)
    text = MD_ESCAPE_RE.sub(r"\1", text)
    text = MENTION_RE.sub(lambda m: f'<span class="mention">@{html.escape(m.group(1))}</span>', text)
    text = LINK_RE.sub(lambda m: f'<a href="{html.escape(m.group(2))}">{html.escape(m.group(1))}</a>', text)

    def linkify_bare(m):
        prefix = text[max(0, m.start() - 20):m.start()]
        if "<a href=" in prefix:
            return m.group(1)
        return f'<a href="{html.escape(m.group(1))}">{html.escape(m.group(1))}</a>'
    text = BARE_URL_RE.sub(linkify_bare, text)
    return text.replace("\n", "<br>")


IMG_EXT_BY_CONTENT_TYPE = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
}


def download_public_image(url, dest_dir, file_id):
    """Inline `image` nodes in a module's prosemirror desc carry a direct
    assets.skool.com URL -- public, no cookie/WAF needed (verified live
    2026-07-11, unlike gated resources/post attachments)."""
    resp = http_get(url)
    if resp.status_code != 200:
        return None
    ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
    ext = IMG_EXT_BY_CONTENT_TYPE.get(ctype, ".png")
    fname = f"{file_id}{ext}"
    long_path(dest_dir / fname).write_bytes(resp.content)
    return fname


UL_TYPES = ("bulletList", "unorderedList", "bullet_list")
OL_TYPES = ("orderedList", "ordered_list")


def render_prosemirror_desc(desc, lesson_dir, warn=None):
    """Module `desc` field: "[v2]" + JSON list of ProseMirror block nodes.

    Renders RECURSIVELY. The original version walked only the top level and
    assumed list items contained nothing but paragraphs, which silently dropped
    real content -- found 2026-07-26 by reconciling the source JSON against the
    rendered HTML across 107 live lessons:
      - `bulletList` is a FOURTH list-type spelling alongside `unorderedList` /
        `bullet_list` / `orderedList`; 22 of them rendered as nothing at all.
      - `codeBlock` (9) was unhandled, including one nested inside a blockquote.
      - Nested lists (`listItem > unorderedList`) lost their sub-items, because
        the old item loop only descended through `paragraph` children.
      - `hardBreak` inline nodes vanished.
    Unknown node types are now recorded in `warn` and rendered best-effort
    rather than disappearing silently -- the failure mode that hid all of the
    above was that a dropped node looks exactly like a node that was never there.
    """
    if not desc or not desc.startswith("[v2]"):
        return ""
    return _render_blocks(json.loads(desc[4:]), lesson_dir, warn if warn is not None else set())


def _render_blocks(nodes, lesson_dir, warn):
    return "\n".join(b for b in (_render_block(n, lesson_dir, warn) for n in nodes or []) if b)


def _render_block(node, lesson_dir, warn):
    ntype = node.get("type")

    if ntype == "image":
        attrs = node.get("attrs", {})
        src = attrs.get("src") or attrs.get("originalSrc")
        file_id = attrs.get("fileID") or "img"
        if not src:
            return ""
        fname = download_public_image(src, lesson_dir, file_id)
        if not fname:
            warn.add("image:download-failed")
            return ""
        alt = attrs.get("alt") or attrs.get("title") or ""
        return f'<img class="attach-img" src="./{html.escape(fname)}" alt="{html.escape(alt)}">'

    if ntype == "paragraph":
        inline = _render_inline_seq(node.get("content"))
        return f"<p>{inline}</p>" if inline else ""

    if ntype == "heading":
        inline = _render_inline_seq(node.get("content"))
        level = node.get("attrs", {}).get("level", 3)
        tag = "h2" if level <= 3 else "h3"
        return f"<{tag}>{inline}</{tag}>" if inline else ""

    if ntype == "blockquote":
        inner = _render_blocks(node.get("content"), lesson_dir, warn)
        return f"<blockquote>{inner}</blockquote>" if inner else ""

    if ntype in UL_TYPES or ntype in OL_TYPES:
        tag = "ol" if ntype in OL_TYPES else "ul"
        items = [f"<li>{_render_blocks(item.get('content'), lesson_dir, warn)}</li>"
                 for item in node.get("content") or []]
        items = [i for i in items if i != "<li></li>"]
        return f"<{tag}>{''.join(items)}</{tag}>" if items else ""

    if ntype == "listItem":  # defensive: only reachable on malformed trees
        return _render_blocks(node.get("content"), lesson_dir, warn)

    if ntype == "codeBlock":
        code = "".join(r.get("text", "") for r in node.get("content") or [])
        return f'<pre class="codeblock"><code>{html.escape(code)}</code></pre>' if code else ""

    if ntype in ("horizontalRule", "horizontal_rule"):
        return "<hr>"

    warn.add(ntype)
    inner = _render_blocks(node.get("content"), lesson_dir, warn)
    if inner:
        return inner
    inline = _render_inline_seq(node.get("content"))
    return f"<p>{inline}</p>" if inline else ""


def _render_inline_seq(nodes):
    return "".join(_render_inline(n) for n in nodes or [])


def _render_inline(node):
    ntype = node.get("type")
    if ntype == "hardBreak":
        return "<br>"
    if ntype != "text":
        # A block node (paragraph/listItem) can appear where inline runs are
        # expected; descend instead of returning "" and losing its text.
        return _render_inline_seq(node.get("content"))
    text = html.escape(node.get("text", ""), quote=False)
    href = None
    wrap_bold = wrap_italic = False
    for mark in node.get("marks", []):
        mtype = mark.get("type")
        if mtype == "link":
            href = mark.get("attrs", {}).get("href")
        elif mtype in ("bold", "strong"):
            wrap_bold = True
        elif mtype == "italic":
            wrap_italic = True
    if wrap_bold:
        text = f"<strong>{text}</strong>"
    if wrap_italic:
        text = f"<em>{text}</em>"
    if href:
        text = f'<a href="{html.escape(href)}">{text}</a>'
    return text


# ---------------------------------------------------------------- comments

def avatar_color(name):
    h = sum(ord(c) for c in name)
    return AVATAR_PALETTE[h % len(AVATAR_PALETTE)]


def initials(name):
    parts = [p for p in name.split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def flatten_comments(children, depth=0, out=None):
    if out is None:
        out = []
    for node in children or []:
        post = node["post"]
        m = post.get("metadata", {})
        user = post.get("user", {})
        author = f'{user.get("first_name", "")} {user.get("last_name", "")}'.strip() or "Unknown"
        out.append({
            "id": post.get("id"),
            "author": author,
            "created_at": post.get("created_at", ""),
            "content": m.get("content", ""),
            "upvotes": m.get("upvotes", 0),
            "has_gif": bool(m.get("attachments_data")),
            "depth": depth,
        })
        flatten_comments(node.get("children"), depth + 1, out)
    return out


def merge_comment_windows(pinned_data, tail_data, total_comments_ui_count):
    seen, merged = set(), []
    for data in (pinned_data, tail_data):
        if not data:
            continue
        children = data.get("post_tree", {}).get("children", [])
        for c in flatten_comments(children):
            if c["id"] not in seen:
                seen.add(c["id"])
                merged.append(c)
    gap_note = None
    if total_comments_ui_count and len(merged) < total_comments_ui_count:
        gap_note = (
            f"Showing {len(merged)} of {total_comments_ui_count} comments. Skool's comment API "
            "only exposes the oldest 25 and newest 25 comments per post (the \"pinned=true\" / "
            "\"tail=true\" windows); no working pagination was found for the mid-thread comments "
            "in between — they're not shown here. GIF reactions attached to comments are noted "
            "but not downloaded, per policy (only the main post's GIF is copied)."
        )
    return merged, gap_note


def render_comments_html(comments):
    if not comments:
        return ""
    rows = []
    for c in comments:
        indent = 44 * c["depth"]
        date = c["created_at"][:10]
        body = render_content(c["content"])
        gif_note = '<span class="gif-note">[GIF reaction — not copied, per policy]</span>' if c["has_gif"] else ""
        if not body and gif_note:
            body = gif_note
        elif gif_note:
            body = body + " " + gif_note
        actions = f'\U0001f44d {c["upvotes"]} &nbsp;·&nbsp; Reply' if c["upvotes"] else "Reply"
        rows.append(
            f'<div class="crow" style="margin-left:{indent}px">'
            f'<div class="c-avatar" style="background:{avatar_color(c["author"])}">{html.escape(initials(c["author"]))}</div>'
            f'<div class="c-main"><div class="c-bubble">'
            f'<p class="c-meta"><strong>{html.escape(c["author"])}</strong> &nbsp;·&nbsp; {date}</p>'
            f'<p class="c-body">{body}</p></div>'
            f'<div class="c-actions">{actions}</div></div></div>'
        )
    return "".join(rows)


# ---------------------------------------------------------------- resources / gated files

def parse_resources(resources_raw):
    if not resources_raw:
        return []
    resources = json.loads(resources_raw) if isinstance(resources_raw, str) else resources_raw
    out = []
    for r in resources:
        if r.get("file_id"):
            out.append({"kind": "gated", "title": r.get("title"), "file_id": r.get("file_id"),
                        "file_name": r.get("file_name"), "content_type": r.get("file_content_type")})
        elif r.get("link"):
            out.append({"kind": "link", "title": r.get("title"), "url": r.get("link")})
    return out


def download_gated_file(url, dest_path):
    r = http_get(url)
    r.raise_for_status()
    long_path(dest_path).write_bytes(r.content)
    return len(r.content)


def make_res_html(res, lesson_dir, signed_urls):
    if res["kind"] == "link":
        return f'<a class="res" href="{html.escape(res["url"])}" target="_blank">{html.escape(res["title"] or res["url"])} (external link)</a>'
    file_id, file_name = res["file_id"], res["file_name"] or res["file_id"]
    signed_url = signed_urls.get(file_id)
    if not signed_url:
        return f'<p class="meta">Gated file "{html.escape(file_name)}" not fetched — rerun with --browser-data.</p>'
    dest = lesson_dir / file_name
    size = download_gated_file(signed_url, dest)
    ext = Path(file_name).suffix.lower()
    size_kb = f"{size / 1024:.1f} KB"
    if ext in TEXT_MIME and size < 200_000:
        b64 = base64.b64encode(long_path(dest).read_bytes()).decode("ascii")
        mime = TEXT_MIME.get(ext, "text/plain")
        return (f'<a class="res" href="data:{mime};base64,{b64}" download="{html.escape(file_name)}">'
                f'{html.escape(file_name)} ({size_kb}, {html.escape(res["title"] or "")})</a>')
    return (f'<a class="res" href="./{html.escape(file_name)}" download>'
            f'{html.escape(file_name)} ({size_kb}, {html.escape(res["title"] or "")})</a>')


# ---------------------------------------------------------------- rendering (Skool-dark template)

STYLE = """
:root{--bg:#222;--card:#262626;--card-plain:#333;--chip:#2e2e2e;--border:rgba(255,255,255,.09);--text:#e4e4e4;--muted:#909090;--accent:#93b3f3}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--text);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;margin:0;padding:0;display:flex;align-items:flex-start;min-height:100vh}
.main-content{flex:1;min-width:0;max-width:760px;margin:40px auto;padding:0 20px 60px}
.meta{color:var(--muted);font-size:13px}
.sidebar{flex:0 0 280px;width:280px;align-self:flex-start;position:sticky;top:0;max-height:100vh;overflow-y:auto;background:#1b1b1b;border-right:1px solid var(--border);padding:18px 14px;transition:margin-left .2s,opacity .15s}
body.sidebar-collapsed .sidebar{margin-left:-280px;opacity:0;pointer-events:none}
.sidebar-head{margin-bottom:12px}
.sidebar-title{font-weight:700;color:#fff;font-size:15px;margin-bottom:9px}
.progress-track{background:#3a3a3a;border-radius:999px;height:8px;overflow:hidden}
.progress-fill{background:#3ecf6e;height:100%}
.progress-label{color:var(--muted);font-size:12px;margin-top:5px}
.sidebar-nav{margin-top:12px}
.nav-set{margin-bottom:2px}
.nav-set summary{font-weight:700;color:var(--text);font-size:13.5px;padding:8px 6px;cursor:pointer;list-style:none;border-radius:8px;display:flex;align-items:center;justify-content:space-between;user-select:none}
.nav-set summary::-webkit-details-marker{display:none}
.nav-set summary::after{content:"⌄";color:var(--muted);transition:transform .15s}
.nav-set[open] summary::after{transform:rotate(180deg)}
.nav-set summary:hover{background:rgba(255,255,255,.05)}
.nav-modules{padding-left:2px}
.nav-item{display:flex;align-items:center;gap:8px;padding:7px 6px 7px 12px;border-radius:8px;color:var(--text);text-decoration:none;font-size:13px}
.nav-item:hover{background:rgba(255,255,255,.05)}
.nav-item.current{background:rgba(147,179,243,.14);color:#fff;font-weight:600}
.nav-item-title{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tick{flex:0 0 16px;width:16px;height:16px;border-radius:50%;border:1.5px solid #666;background:transparent;cursor:pointer;padding:0;position:relative}
.tick:hover{border-color:#999}
.tick.done{background:#3ecf6e;border-color:#3ecf6e}
.tick.done::after{content:"";position:absolute;left:4px;top:1.5px;width:4px;height:8px;border:solid #16281c;border-width:0 2px 2px 0;transform:rotate(40deg)}
.sidebar-foot{margin-top:16px;padding-top:12px;border-top:1px solid var(--border)}
#save-progress-btn{width:100%;background:var(--chip);border:1px solid var(--border);color:var(--text);padding:9px;border-radius:8px;cursor:pointer;font-size:13px}
#save-progress-btn:hover{border-color:#555}
#save-status{margin-top:7px;line-height:1.4}
#sidebar-toggle{position:fixed;top:14px;left:14px;z-index:20;background:var(--chip);border:1px solid var(--border);color:var(--text);width:34px;height:34px;border-radius:8px;cursor:pointer;font-size:15px;transition:left .2s}
body:not(.sidebar-collapsed) #sidebar-toggle{left:294px}
.title-row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 14px}
.title-row h1.p-title{margin:0}
.title-tick{flex:0 0 24px;width:24px;height:24px;border-radius:50%;border:2px solid #666;background:transparent;cursor:pointer;position:relative}
.title-tick:hover{border-color:#999}
.title-tick.done{background:#3ecf6e;border-color:#3ecf6e}
.title-tick.done::after{content:"";position:absolute;left:7px;top:3px;width:6px;height:12px;border:solid #16281c;border-width:0 2.5px 2.5px 0;transform:rotate(40deg)}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:24px;margin:18px 0 8px}
.card.plain{background:var(--card-plain);border-radius:10px;padding:28px 32px}
.p-header{display:flex;gap:12px;align-items:flex-start;margin-bottom:16px}
.p-avatar{flex:0 0 44px;width:44px;height:44px;border-radius:50%;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:16px}
.p-who{font-weight:700;color:#fff;font-size:15px}
.p-sub{color:var(--muted);font-size:13px;margin-top:2px}
h1.p-title{font-size:23px;font-weight:700;color:#fff;margin:0 0 14px}
.p-body p{margin:0 0 14px;color:var(--text)}
.p-body h2{font-size:20px;font-weight:500;color:var(--text);margin:28px 0 10px}
.p-body a,.p-body a:visited{color:var(--accent);text-decoration:none}
.p-body a:hover{text-decoration:underline}
.p-body blockquote{border-left:3px solid var(--accent);padding:4px 14px;margin:16px 0;color:var(--muted);background:rgba(255,255,255,.03)}
.p-body ul,.p-body ol{margin:0 0 14px;padding-left:22px}
.p-body li{margin:4px 0}
.p-body li>p{margin:0}
.p-body li>ul,.p-body li>ol{margin:4px 0}
.p-body blockquote p:last-child{margin-bottom:0}
.p-body pre.codeblock{background:#1b1b1b;border:1px solid var(--border);border-radius:8px;padding:12px 14px;overflow-x:auto;margin:0 0 14px}
.p-body pre.codeblock code{font:13px/1.5 Consolas,"Courier New",monospace;color:#d8d8d8;white-space:pre}
.pinned-label{color:var(--muted);font-size:13px;margin:28px 0 -6px}
.mention{color:var(--accent);font-weight:500}
.thumb-link{display:block;position:relative;max-width:100%;margin:16px 0;border-radius:12px;overflow:hidden}
.thumb-link img{display:block;width:100%;height:auto}
.play{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:68px;height:48px;background:rgba(0,0,0,.75);border-radius:14px;display:flex;align-items:center;justify-content:center}
.play::after{content:"";border-style:solid;border-width:12px 0 12px 20px;border-color:transparent transparent transparent #fff;margin-left:4px}
.attach-img{max-width:100%;width:100%;height:auto;border-radius:12px;display:block;margin:14px 0}
.res{display:block;padding:10px 13px;background:var(--chip);border:1px solid var(--border);border-radius:10px;margin:6px 0;color:var(--text);text-decoration:none;font-size:14px}
.res:hover{border-color:#555}
.actions{display:flex;gap:18px;align-items:center;margin-top:18px;padding-top:14px;border-top:1px solid var(--border);color:var(--muted);font-size:14px}
.comments-toggle{margin:28px 0 4px}
.comments-toggle summary{font-size:17px;font-weight:700;color:#fff;margin:0;cursor:pointer;list-style:none;display:flex;align-items:center;gap:7px;user-select:none}
.comments-toggle summary::-webkit-details-marker{display:none}
.comments-toggle summary::before{content:"▶";font-size:11px;color:var(--muted);transition:transform .15s}
.comments-toggle[open] summary::before{transform:rotate(90deg)}
.comments-toggle summary:hover{color:var(--accent)}
.comments{margin-top:12px}
.crow{display:flex;gap:10px;padding:10px 0}
.c-avatar{flex:0 0 36px;width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:13px}
.c-main{flex:1;min-width:0}
.c-bubble{background:var(--bg);border:1px solid var(--border);border-radius:15px;padding:8px 13px}
.c-meta{margin:0;font-size:13px;color:var(--muted)}
.c-meta strong{color:#fff;font-weight:700}
.c-body{margin:4px 0 0;font-size:15px;color:var(--text);line-height:1.5}
.c-actions{margin:4px 0 0 4px;font-size:13px;color:var(--muted)}
.gif-note{color:#8a8a8a;font-style:italic;font-size:13px}
hr{border:none;border-top:1px solid var(--border);margin:32px 0 14px}
"""


def video_html(video):
    if not video or not video.get("thumb_primary"):
        return "", ""
    link_label = {"youtube": "Open on YouTube", "loom": "Open on Loom",
                  "skool": "Watch on Skool"}.get(video["kind"], "Open link")
    onerror = (f"onerror=\"this.onerror=null;this.src='{video['thumb_fallback']}'\""
               if video.get("thumb_fallback") and video["thumb_fallback"] != video["thumb_primary"] else "")
    thumb = (f'<a class="thumb-link" href="{html.escape(video["watch_url"])}" target="_blank">'
             f'<img src="{html.escape(video["thumb_primary"])}" {onerror} alt="Video thumbnail">'
             f'<div class="play"></div></a>')
    caption_bits = [f'<a href="{html.escape(video["watch_url"])}" style="color:var(--accent)">{link_label}</a>']
    extra = " — \"" + video["title"] + "\"" if video.get("title") else ""
    if video.get("note"):
        extra += f", {video['note']}"
    caption = f'<p class="meta">{caption_bits[0]}{extra}</p>'
    return thumb, caption


def render_post_block(post_title, author_name, date_sub, body_html, attachment_files,
                       upvotes, comment_count, comment_rows, comments_html_str, gap_note,
                       label="Pinned discussion post"):
    """Renders one pinned community post as its own self-contained card, with its
    OWN comments nested directly inside it -- these are two distinct pieces of
    content from the module's own lesson body (verified live 2026-07-11: a
    lesson's module `desc` can be a full formal lesson body, with a separate,
    shorter discussion post pinned underneath it), not alternatives. A lesson
    can have more than one pinned post; each must stay self-contained (own
    comments/attachments) rather than pooling everything under one heading,
    or a multi-post lesson turns into an unreadable mess."""
    attach_block = ""
    if attachment_files:
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        parts, img_count = [], 0
        for f in attachment_files:
            if Path(f).suffix.lower() in image_exts:
                parts.append(f'<img class="attach-img" src="./{html.escape(f)}" alt="{html.escape(f)}">')
                img_count += 1
            else:
                parts.append(f'<a class="res" href="./{html.escape(f)}" download>{html.escape(f)}</a>')
        attach_block = "\n  ".join(parts)
        if img_count:
            plural = "s" if img_count > 1 else ""
            attach_block += f'\n  <p class="meta">Attached image{plural} from the pinned post — downloaded locally, per policy.</p>'
        other_count = len(attachment_files) - img_count
        if other_count:
            plural = "s" if other_count > 1 else ""
            attach_block += f'\n  <p class="meta">Attached file{plural} from the pinned post — downloaded locally, per policy.</p>'
    comments_section = ""
    if comment_rows is not None:
        gap_html = f'<p class="meta">{html.escape(gap_note)}</p>' if gap_note else ""
        comments_section = f"""
  <details class="comments-toggle">
    <summary>Comments ({len(comment_rows)} of {comment_count})</summary>
    {gap_html}
    <div class="comments">
    {comments_html_str}
    </div>
  </details>
"""
    title_html = f'<h2 class="p-title" style="font-size:19px">{html.escape(post_title)}</h2>' if post_title else ""
    return f"""
<p class="pinned-label">{html.escape(label)}</p>
<div class="card">
  <div class="p-header">
    <div class="p-avatar" style="background:{avatar_color(author_name)}">{html.escape(initials(author_name))}</div>
    <div>
      <div class="p-who">{html.escape(author_name)}</div>
      <div class="p-sub">{html.escape(date_sub)}</div>
    </div>
  </div>

  {title_html}

  <div class="p-body">
    {body_html}
  </div>

  {attach_block}

  <div class="actions">\U0001f44d {upvotes} &nbsp;·&nbsp; \U0001f4ac {comment_count} comments</div>
{comments_section}</div>
"""


def render_post_stub(post_title, author_name, owner_folder, owner_title, comment_count, label):
    """Compact stand-in for a discussion post that is pinned under more than one
    lesson. The first lesson to claim the post renders it in full; the others get
    this card, which links to that local sibling copy. Without it, one popular
    cross-pinned post would duplicate its entire comment thread (sometimes 100+
    comments) into every lesson referencing it -- which is exactly the
    duplication the bulk manifest exists to prevent."""
    href = f"../{owner_folder}/{owner_folder}.html"
    title_html = f'<h2 class="p-title" style="font-size:19px">{html.escape(post_title)}</h2>' if post_title else ""
    return f"""
<p class="pinned-label">{html.escape(label)}</p>
<div class="card">
  {title_html}
  <div class="p-body">
    <p class="meta">Posted by {html.escape(author_name)} &nbsp;·&nbsp; {comment_count} comments &nbsp;·&nbsp;
    also pinned under another lesson, where it is archived in full.</p>
    <p><a href="{html.escape(href)}">Open it in &ldquo;{html.escape(owner_title)}&rdquo; &rarr;</a></p>
  </div>
</div>
"""


def lesson_folder_name(set_title, lesson_title):
    """Sibling lesson folders are named "<Day/section> - <lesson title>", not
    just the bare lesson title -- several lesson titles repeat verbatim across
    every day (e.g. "1. Send 10 applications" appears under every "Day N" --
    confirmed live 2026-07-11), so a bare-title scheme would collide different
    lessons from different days into the same folder during a full-community
    bulk scrape. This exact convention is used both for the CURRENT lesson's
    own output folder (see main()) and for every sidebar link's relative
    target (see render_sidebar()), so they always agree."""
    name = f"{set_title} - {lesson_title}" if set_title else lesson_title
    return sanitize_folder_name(name)


def build_nav(course_tree, current_md):
    """Walk the course JSON tree (page_props['course'], same tree find_module_node
    already searches) into the sidebar's group/lesson structure -- top-level
    children are "set" nodes (Skool's "Day 1"/"Day 2"/etc groupings), each
    holding "module" leaf nodes that are the actual lessons. Each module's own
    `metadata.completed` flag gives the REAL completion state as of capture
    time, seeding the local tick state (verified live 2026-07-11)."""
    if not course_tree:
        return None
    root_meta = course_tree.get("course", {}).get("metadata", {})
    sets = []
    current_folder = None
    for child in course_tree.get("children", []):
        c = child.get("course", {})
        if c.get("unitType") != "set":
            continue
        set_title = c.get("metadata", {}).get("title") or "Untitled"
        modules = []
        for leaf in child.get("children", []):
            lc = leaf.get("course", {})
            if lc.get("unitType") != "module":
                continue
            lm = lc.get("metadata", {})
            lesson_title = lm.get("title") or "Untitled"
            folder = lesson_folder_name(set_title, lesson_title)
            if lc.get("id") == current_md:
                current_folder = folder
            modules.append({
                "id": lc.get("id"),
                "title": lesson_title,
                "completed": bool(lm.get("completed")),
                "folder": folder,
            })
        if modules:
            sets.append({"title": set_title, "modules": modules})
    total = root_meta.get("numModules") or sum(len(s["modules"]) for s in sets)
    done = root_meta.get("userCompleted") or sum(1 for s in sets for m in s["modules"] if m["completed"])
    pct = round(100 * done / total) if total else 0
    return {
        "title": root_meta.get("title") or "Course", "done": done, "total": total, "pct": pct,
        "sets": sets, "current_md": current_md, "current_folder": current_folder,
    }


def render_sidebar(nav, folder_map=None):
    """Recreates Skool's left classroom menu -- collapsible day groups, a per-
    lesson completion tick, and a course progress bar. Every row (including the
    current lesson) links to a RELATIVE sibling folder -- "../<Day - Title>/
    <Day - Title>.html" -- not the live Skool URL, per the user's 2026-07-11
    bulk-scrape plan: every lesson in the community gets extracted into its own
    sibling folder under the same Drive destination, so the whole community
    becomes a self-contained, cross-linked offline copy. A link works as soon
    as that sibling lesson has actually been extracted; until then it 404s
    locally -- expected during a partial/in-progress bulk run, not a bug.
    Ticks are locally toggleable and independent of the real Skool account --
    see saveProgress() in SCRIPT_JS for how the toggled state gets persisted
    back into this very HTML file.

    `folder_map` ({md: folder_name}) overrides the per-lesson folder name when a
    bulk run has computed the whole course's names up front. That matters
    because sanitize_folder_name() truncates at 60 chars, so two lessons whose
    "Day N - Title" strings agree in their first 60 characters would otherwise
    resolve to the SAME folder -- one silently overwriting the other, with the
    sidebar linking to whichever won. A course-wide map can see the collision
    and disambiguate; a single-lesson run cannot. Omitted (None) => exactly the
    previous behaviour."""
    if not nav or not nav["sets"]:
        return ""
    groups_html = []
    for s in nav["sets"]:
        rows = []
        for m in s["modules"]:
            is_current = m["id"] == nav["current_md"]
            done_class = "done" if m["completed"] else ""
            folder = (folder_map or {}).get(m["id"], m["folder"])
            href = f'../{folder}/{folder}.html'
            current_class = " current" if is_current else ""
            rows.append(
                f'<a class="nav-item{current_class}" href="{html.escape(href)}">'
                f'<button class="tick {done_class}" data-lesson="{html.escape(m["id"])}" '
                f'onclick="toggleTick(event)" title="Mark complete" type="button"></button>'
                f'<span class="nav-item-title">{html.escape(m["title"])}</span></a>'
            )
        is_open = any(m["id"] == nav["current_md"] for m in s["modules"])
        groups_html.append(
            f'<details class="nav-set"{" open" if is_open else ""}>'
            f'<summary>{html.escape(s["title"])}</summary>'
            f'<div class="nav-modules">{"".join(rows)}</div></details>'
        )
    return f"""<button id="sidebar-toggle" onclick="document.body.classList.toggle('sidebar-collapsed')" title="Toggle menu" type="button">☰</button>
<div class="sidebar" id="skool-sidebar">
  <div class="sidebar-head">
    <div class="sidebar-title">{html.escape(nav["title"])}</div>
    <div class="progress-track"><div class="progress-fill" style="width:{nav['pct']}%"></div></div>
    <div class="progress-label">{nav['done']} / {nav['total']} completed ({nav['pct']}%)</div>
  </div>
  <nav class="sidebar-nav">
    {''.join(groups_html)}
  </nav>
  <div class="sidebar-foot">
    <button id="save-progress-btn" onclick="saveProgress()" type="button">\U0001f4be Save progress to this file</button>
    <p class="meta" id="save-status"></p>
  </div>
</div>
"""


SCRIPT_JS = """
let __fileHandle = null;
function toggleTick(e){
  e.preventDefault(); e.stopPropagation();
  e.currentTarget.classList.toggle('done');
}
async function saveProgress(){
  const status = document.getElementById('save-status');
  if (!window.showSaveFilePicker) {
    try {
      const state = {};
      document.querySelectorAll('.tick[data-lesson]').forEach(b => { state[b.dataset.lesson] = b.classList.contains('done'); });
      localStorage.setItem(location.pathname, JSON.stringify(state));
      status.textContent = "Saved to this browser's local storage (it doesn't support saving into the file itself) -- will restore next time you open this exact file in this browser.";
    } catch (err) { status.textContent = 'Save failed: ' + err.message; }
    return;
  }
  try {
    if (!__fileHandle) {
      __fileHandle = await window.showSaveFilePicker({
        suggestedName: decodeURIComponent(location.pathname.split('/').pop()),
        types: [{ description: 'HTML file', accept: { 'text/html': ['.html'] } }],
      });
    }
    const writable = await __fileHandle.createWritable();
    await writable.write('<!doctype html>\\n' + document.documentElement.outerHTML);
    await writable.close();
    status.textContent = 'Saved — progress is now stored directly in this HTML file.';
  } catch (err) {
    if (err.name !== 'AbortError') status.textContent = 'Save failed: ' + err.message;
  }
}
(function restoreFallback(){
  try {
    const raw = localStorage.getItem(location.pathname);
    if (!raw) return;
    const state = JSON.parse(raw);
    document.querySelectorAll('.tick[data-lesson]').forEach(b => {
      if (state[b.dataset.lesson] !== undefined) b.classList.toggle('done', !!state[b.dataset.lesson]);
    });
  } catch (e) {}
})();
"""


def render_lesson_page(title, breadcrumb, body_html, video, resources_html, post_html, source_line,
                        sidebar_html="", current_md="", current_done=False):
    thumb, caption = video_html(video)
    resources_block = "\n  ".join(resources_html) if resources_html else '<p class="meta">No gated downloadable files on this lesson.</p>'
    done_class = "done" if current_done else ""
    title_tick = (
        f'<button class="tick title-tick {done_class}" data-lesson="{html.escape(current_md)}" '
        f'onclick="toggleTick(event)" title="Mark complete" type="button"></button>'
    ) if current_md else ""
    return f"""<!doctype html>
<meta charset="utf-8">
<title>{html.escape(title)} — Resource Library</title>
<style>{STYLE}</style>
{sidebar_html}
<div class="main-content">
<p class="meta">{html.escape(breadcrumb)}</p>

<div class="card plain">
  <div class="title-row">
    <h1 class="p-title">{html.escape(title)}</h1>
    {title_tick}
  </div>

  {thumb}
  {caption}

  <div class="p-body">
    {body_html}
  </div>

  {resources_block}
</div>
{post_html}
<hr>
<p class="meta">Source: {html.escape(source_line)}</p>
</div>
<script>{SCRIPT_JS}</script>
"""


# ---------------------------------------------------------------- output

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_month_year(iso_date):
    if not iso_date or len(iso_date) < 7:
        return ""
    year, month = iso_date[:4], iso_date[5:7]
    try:
        return f"{MONTH_ABBR[int(month) - 1]} '{year[2:]}"
    except (ValueError, IndexError):
        return iso_date[:7]


MAX_FOLDER_NAME_LEN = 60  # see sanitize_folder_name()


def sanitize_folder_name(name):
    """Caps the name at MAX_FOLDER_NAME_LEN -- lesson_folder_name()'s Day-prefixed
    scheme (e.g. "About & how to use - 1. This is an exercise program") can push
    a folder+file path past Windows' 260-char MAX_PATH, which Python's long_path()
    \\\\?\\ trick lets this script write around, but Chrome/Explorer/most other
    apps then can't OPEN the file at all (verified live 2026-07-11: a 264-char
    path wrote fine but Chrome returned ERR_FILE_NOT_FOUND). Truncating here
    keeps the worst case (folder name appears twice in the full path, once as
    the directory and once as the .html filename) comfortably under the limit
    regardless of how deep the Drive destination folder is nested."""
    name = "".join(c for c in name if c not in '\\/:*?"<>|')
    name = name.strip().rstrip(". ")
    if len(name) > MAX_FOLDER_NAME_LEN:
        name = name[:MAX_FOLDER_NAME_LEN].rstrip(". ")
    return name


def long_path(p):
    """Windows MAX_PATH is 260 chars by default -- long lesson titles nested under a
    Drive destination can exceed it. The \\\\?\\ prefix opts into the extended-length
    path API and bypasses the limit."""
    if os.name == "nt":
        resolved = str(p.resolve())
        if not resolved.startswith("\\\\?\\"):
            return Path("\\\\?\\" + resolved)
    return p


def detect_drive_root():
    import string
    for letter in string.ascii_uppercase:
        for folder in ("My Drive", "Mon Drive"):
            p = Path(f"{letter}:/{folder}")
            if p.exists():
                return p
    return None


# ---------------------------------------------------------------- one lesson

def extract_lesson(session, group, course, md, out_root, build_id=None,
                   browser_data=None, page_props=None, nav=None,
                   folder_map=None, index=None, verbose=True):
    """Extract ONE lesson into out_root/<lesson folder>/ and return a stats dict.

    This is the whole of the original main() body, lifted verbatim so the bulk
    driver (skool_bulk.py) can call it per lesson instead of duplicating the
    render logic. The CLI main() below is now just argument parsing around it.

    The optional arguments are what bulk needs and single-lesson runs don't;
    every one of them defaults to the previous behaviour:
      page_props  -- reuse an already-fetched lesson JSON (bulk caches these, so
                     a whole-course run fetches each lesson exactly once)
      nav/folder_map -- course-wide navigation + {md: folder} map computed once
                     up front (see render_sidebar for why the map matters)
      index       -- the bulk manifest; enables cross-lesson dedup. None => no
                     dedup and a byte-identical result to before this refactor.
    """
    if build_id is None:
        build_id = fetch_build_id(session, group, course)
    if page_props is None:
        page_props, build_id = fetch_lesson_json_retrying(session, build_id, group, course, md)

    course_tree = page_props.get("course")
    module = find_module_node(course_tree, md)
    if not module:
        raise ValueError(f"Module {md} not found in course tree")
    meta = module.get("metadata", {})
    posts = find_pinned_posts(page_props)
    if nav is None:
        nav = build_nav(course_tree, md)
    sidebar_html = render_sidebar(nav, folder_map)

    browser_data = browser_data or {}
    signed_urls = browser_data.get("signed_urls", {})
    posts_data = browser_data.get("posts", {})  # keyed by post id -- see fetch_browser_data.js

    title = meta.get("title") or (posts[0].get("metadata", {}).get("title") if posts else None) or "Untitled"
    # Must match render_sidebar()'s per-lesson folder naming exactly (Day/section-
    # prefixed, see lesson_folder_name()) or sibling links from OTHER already-
    # extracted lessons pointing at this one will 404 despite this lesson existing.
    if folder_map and md in folder_map:
        lesson_dir_name = folder_map[md]
    elif nav and nav.get("current_folder"):
        lesson_dir_name = nav["current_folder"]
    else:
        lesson_dir_name = sanitize_folder_name(title)

    lesson_dir = Path(out_root) / lesson_dir_name
    long_path(lesson_dir).mkdir(parents=True, exist_ok=True)
    if verbose:
        print(f"Output folder: {lesson_dir}")

    resources = parse_resources(meta.get("resources"))

    # The module's own lesson body/video and a pinned discussion post are two
    # DISTINCT pieces of content that can both be present (verified live
    # 2026-07-11) -- always render the module first, never substitute the post
    # for it. Module video takes priority as the primary lesson video; only
    # fall back to the post's video if the module itself has none.
    lesson_url = f"https://www.skool.com/{group}/classroom/{course}?md={md}"
    video = resolve_video(meta.get("videoLink"))
    if not video:
        # Skool-hosted video (no videoLink, just a videoId) -- the common case:
        # 91 of 107 lessons in the reference classroom.
        video = resolve_skool_video(page_props, meta, lesson_url)
    if not video:
        for p in posts:
            video = resolve_video_from_data(p.get("metadata", {}).get("videoLinksData"))
            if video:
                break
    video = localize_video_thumb(video, lesson_dir)

    desc_warnings = set()
    module_body_html = render_prosemirror_desc(meta.get("desc"), lesson_dir, desc_warnings)
    if desc_warnings and verbose:
        print(f"  unhandled desc node types (rendered best-effort): {sorted(desc_warnings)}")
    module_image_count = module_body_html.count('class="attach-img"')

    resources_html = [make_res_html(r, lesson_dir, signed_urls) for r in resources]

    # Each pinned post is rendered as its own self-contained card with its own
    # comments/attachments nested directly inside it -- never pooled together,
    # so a lesson with several posts doesn't turn into one flat wall of comments.
    total_attachments, total_comments, reused_posts = 0, 0, 0
    post_html_parts = []
    multi = len(posts) > 1
    for idx, post in enumerate(posts, start=1):
        pm = post.get("metadata", {})
        post_id = post.get("id")
        pdata = posts_data.get(post_id, {})
        label = f"Pinned discussion post ({idx} of {len(posts)})" if multi else "Pinned discussion post"

        user = post.get("user", {})
        author = f'{user.get("firstName","")} {user.get("lastName","")}'.strip() or user.get("name", "Unknown")

        # Cross-lesson dedup: the first lesson to claim a post owns it and renders
        # it in full; any other lesson pinning the SAME post links to that local
        # copy instead of duplicating the entire comment thread. Only active when
        # a bulk manifest is supplied.
        if index is not None and post_id:
            owner = index.setdefault("posts", {}).setdefault(
                post_id, {"owner_md": md})["owner_md"]
            if owner != md:
                owner_folder = (folder_map or {}).get(owner)
                if owner_folder:
                    owner_title = (index.get("lessons", {}).get(owner) or {}).get("title", "that lesson")
                    post_html_parts.append(render_post_stub(
                        post_title=pm.get("title") or "", author_name=author,
                        owner_folder=owner_folder, owner_title=owner_title,
                        comment_count=pm.get("comments", 0), label=label,
                    ))
                    reused_posts += 1
                    if verbose:
                        print(f"  post {post_id[:8]} already archived under '{owner_folder}' — linked, not duplicated")
                    continue

        attachment_files = []
        attachment_ids = parse_attachment_ids(pm.get("attachments"))
        if attachment_ids:
            for fname, content in resolve_attachments(attachment_ids, pdata.get("attachments_info")):
                long_path(lesson_dir / fname).write_bytes(content)
                attachment_files.append(fname)
                print(f"  downloaded attachment: {fname}")
        total_attachments += len(attachment_files)

        comments_html_str, comment_rows, gap_note = None, None, None
        if pdata.get("comments_pinned") is not None:
            merged, gap_note = merge_comment_windows(
                pdata.get("comments_pinned"), pdata.get("comments_tail"),
                pm.get("comments", 0),
            )
            comment_rows = merged
            comments_html_str = render_comments_html(merged)
            total_comments += len(merged)

        post_body_html = "\n    ".join(f"<p>{p}</p>" for p in render_content(pm.get("content", "")).split("<br><br>"))
        post_html_parts.append(render_post_block(
            post_title=pm.get("title") or "",
            author_name=author,
            date_sub=format_month_year(post.get("createdAt")),
            body_html=post_body_html,
            attachment_files=attachment_files,
            upvotes=pm.get("upvotes", 0),
            comment_count=pm.get("comments", 0),
            comment_rows=comment_rows,
            comments_html_str=comments_html_str,
            gap_note=gap_note,
            label=label,
        ))
    post_html = "\n".join(post_html_parts)

    html_out = render_lesson_page(
        title=title,
        breadcrumb="Classroom > Resource Library",
        body_html=module_body_html,
        video=video,
        resources_html=resources_html,
        post_html=post_html,
        source_line=f"skool.com/{group}/classroom/{course}?md={md} · captured via skool_scrape.py",
        sidebar_html=sidebar_html,
        current_md=md,
        current_done=bool(meta.get("completed")),
    )

    out_path = lesson_dir / f"{lesson_dir_name}.html"
    long_path(out_path).write_text(html_out, encoding="utf-8")
    if verbose:
        print(f"Wrote {out_path}")
        print(f"Module images: {module_image_count} | Resources: {len(resources)} | "
              f"Posts: {len(posts)} | Post attachments: {total_attachments} | Comments: {total_comments}")

    return {
        "md": md, "title": title, "folder": lesson_dir_name, "path": str(out_path),
        "module_images": module_image_count, "resources": len(resources),
        "posts": len(posts), "attachments": total_attachments,
        "comments": total_comments, "reused_posts": reused_posts,
        "build_id": build_id,
    }


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Extract a Skool lesson without AI/browser automation.")
    ap.add_argument("url", help="Lesson URL, e.g. https://www.skool.com/<group>/classroom/<course>?md=<id>")
    ap.add_argument("--dest", default="skills for Juanca", help="Top-level Drive destination folder")
    ap.add_argument("--browser-data", help="Path to JSON produced by the one-time browser fetch (comments + gated-file signed URLs)")
    ap.add_argument("--out", help="Write directly to this local folder instead of auto-detecting Drive")
    args = ap.parse_args()

    session = get_session()
    group, course, md = parse_lesson_url(args.url)
    print(f"Fetching {group}/{course} md={md} ...")

    if args.out:
        out_root = Path(args.out)
    else:
        root = detect_drive_root()
        if not root:
            sys.exit("Couldn't auto-detect Google Drive root. Pass --out <folder> instead.")
        out_root = root / args.dest

    browser_data = {}
    if args.browser_data:
        browser_data = json.loads(Path(args.browser_data).read_text(encoding="utf-8"))

    try:
        extract_lesson(session, group, course, md, out_root, browser_data=browser_data)
    except ValueError as e:
        sys.exit(f"{e} — check the URL.")


if __name__ == "__main__":
    main()
