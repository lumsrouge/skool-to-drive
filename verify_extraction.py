"""
Exhaustiveness check for a bulk-extracted course.

The trap this exists to catch: a ProseMirror node type the renderer doesn't know
about is dropped SILENTLY, and a dropped node looks exactly like a node that was
never there. Counting what the renderer produced (as the manifest stats do)
therefore cannot detect loss -- it only ever confirms the renderer agrees with
itself. This walks the cached SOURCE JSON instead and asserts that every text
run, image, and video actually reached the rendered HTML.

    python3 verify_extraction.py "<course output folder>"
"""
import html as H
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skool_scrape as ss

sys.stdout.reconfigure(encoding="utf-8")

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def norm(s):
    return WS_RE.sub(" ", s).strip()


def squash(s):
    """All whitespace removed. Necessary for containment tests: an inline mark
    splits a text run across tags (`download <strong>Loom</strong>`), and tag
    stripping injects whitespace at those boundaries, so a naive substring test
    reports false losses on exactly the richest-formatted sentences."""
    return WS_RE.sub("", H.unescape(s))


def visible_text(html_str):
    """Rendered HTML -> normalised visible text, with data: URIs stripped first
    (a base64 blob would otherwise 'contain' almost any short string)."""
    s = re.sub(r'href="data:[^"]*"', 'href=""', html_str)
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s, flags=re.S | re.I)
    s = TAG_RE.sub(" ", s)
    return norm(H.unescape(s))


def desc_text_runs(desc):
    """Every text run in a module body, recursively, in document order."""
    if not desc or not isinstance(desc, str) or not desc.startswith("[v2]"):
        return []
    try:
        nodes = json.loads(desc[4:])
    except (ValueError, TypeError):
        return []
    out = []

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
            return
        if not isinstance(n, dict):
            return
        if n.get("type") == "text" and n.get("text"):
            out.append(n["text"])
        walk(n.get("content"))

    walk(nodes)
    return out


def desc_images(desc):
    if not desc or not isinstance(desc, str) or not desc.startswith("[v2]"):
        return 0
    try:
        nodes = json.loads(desc[4:])
    except (ValueError, TypeError):
        return 0
    n = 0

    def walk(x):
        nonlocal n
        if isinstance(x, list):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            if x.get("type") == "image" and (x.get("attrs") or {}).get("src"):
                n += 1
            walk(x.get("content"))

    walk(nodes)
    return n


MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
MD_MENTION = re.compile(r"\[@([^\]]+)\]\(obj://user/[^)]+\)")
MD_ESC = re.compile(r"\\([\\`*_{}\[\]()#+.!-])")


LIST_TOK = re.compile(r"\[(?:ol(?::\d+)?|ul|li)\]")


def plain_visible(content):
    """A post/comment `content` string as the reader should finally see it:
    markdown link syntax reduced to its label, mentions to @Name, backslash
    escapes removed, and Skool's bracket list tokens dropped (they become real
    <ul>/<ol> markup, so they must not be expected in the visible text).
    Mirrors render_rich_content() so a comparison tests delivery, not
    formatting."""
    if not content:
        return ""
    s = MD_MENTION.sub(lambda m: "@" + m.group(1), content)
    s = MD_LINK.sub(lambda m: m.group(1), s)
    s = LIST_TOK.sub(" ", s)
    return MD_ESC.sub(r"\1", s)


def load_blobs(root):
    """Decode every chunk blob into one {post_id: {...}} map (ground truth for
    comments and post attachments)."""
    posts = {}
    for p in sorted((root / "_bulk").glob("chunk_*_blob.json")):
        raw = p.read_text(encoding="utf-8").strip()
        try:
            val = json.loads(raw)
            if isinstance(val, str):
                val = json.loads(__import__("base64").b64decode(val).decode("utf-8"))
        except (ValueError, TypeError):
            continue
        posts.update(val.get("posts") or {})
    return posts


def flatten_comments(children, depth=0, out=None):
    if out is None:
        out = []
    for node in children or []:
        post = node.get("post") or {}
        m = post.get("metadata") or {}
        out.append({
            "id": post.get("id"),
            "content": m.get("content") or "",
            "has_gif": bool(m.get("attachments_data")),
            "depth": depth,
        })
        flatten_comments(node.get("children"), depth + 1, out)
    return out


def main():
    root = Path(sys.argv[1])
    man = json.loads((root / "_bulk" / "manifest.json").read_text(encoding="utf-8"))
    pages = root / "_bulk" / "pages"
    blob_posts = load_blobs(root)

    missing_text, missing_video, missing_img, missing_file = [], [], [], []
    missing_post, missing_comment, missing_res = [], [], []
    missing_postvid, remote_imgs, leftover_markup = [], [], []
    tot_runs = tot_imgs = tot_vid = 0
    tot_posts = tot_comments = tot_res = tot_attach = tot_postvid = 0

    for md, l in man["lessons"].items():
        folder = man["folder_map"][md]
        page = root / folder / f"{folder}.html"
        if not page.exists():
            missing_file.append(folder)
            continue
        raw_html = ss.long_path(page).read_text(encoding="utf-8")
        rendered_vis = visible_text(raw_html)
        rendered = squash(rendered_vis)

        pp = json.loads(ss.long_path(pages / f"{md}.json").read_text(encoding="utf-8"))
        meta = (ss.find_module_node(pp.get("course"), md) or {}).get("metadata", {})

        # --- every text run must survive
        for run in desc_text_runs(meta.get("desc")):
            t = squash(run)
            if len(t) < 4:
                continue
            tot_runs += 1
            if t not in rendered:
                missing_text.append((l["set"], l["title"], norm(run)[:70]))

        # --- every inline image must be referenced AND on disk
        want_imgs = desc_images(meta.get("desc"))
        tot_imgs += want_imgs
        got = len(re.findall(r'<img class="attach-img"', raw_html))
        if got < want_imgs:
            missing_img.append((l["title"], want_imgs, got))

        # --- a lesson that HAS a video must show one
        has_video = bool((meta.get("videoLink") or "").strip() or (meta.get("videoId") or "").strip())
        if has_video:
            tot_vid += 1
            # A poster that 403s legitimately degrades to a link-only card, so
            # accept either a thumbnail or a watch link -- but not silence.
            shown = ('class="thumb-link"' in raw_html
                     or re.search(r">(Watch on Skool|Open on \w+|Open link)<", raw_html))
            if not shown:
                missing_video.append((l["set"], l["title"]))

        # --- no raw Skool markup may survive into the visible text. Skool
        #     encodes lists as [ol:N]/[ul]/[li] with no closing tags; if the
        #     renderer doesn't understand a token it shows up verbatim to the
        #     reader rather than failing loudly.
        for tok in re.findall(r"\[(?:ol(?::\d+)?|ul|li)\]", rendered_vis):
            leftover_markup.append((l["title"], tok))

        # --- no page may depend on a remote image: this is an OFFLINE archive
        for src in re.findall(r'<img[^>]+src="(https?://[^"]+)"', raw_html):
            remote_imgs.append((l["title"], src[:60]))

        # --- pinned post bodies, and the comments hanging off them
        posts = ss.find_pinned_posts(pp)
        stub_owned = set(re.findall(r'also pinned under another lesson', raw_html))

        # --- a post carrying its own video must show it (module video is NOT a
        #     substitute -- they are distinct content)
        post_vid_urls, post_hosted = [], 0
        for post in posts:
            pmeta = post.get("metadata") or {}
            raw = pmeta.get("videoLinksData")
            arr = None
            if raw:
                try:
                    arr = json.loads(raw) if isinstance(raw, str) else raw
                except (ValueError, TypeError):
                    arr = None
            for v in arr or []:  # empty list is normal and means "no video"
                if v.get("url"):
                    post_vid_urls.append(v["url"])
            # A post uses EITHER an external video (videoLinksData) or a
            # Skool-hosted one (videoIds) -- count the hosted case too.
            if not arr and (pmeta.get("videoIds") or "").strip():
                post_hosted += 1
        unesc = H.unescape(raw_html)
        for u in post_vid_urls:
            tot_postvid += 1
            if u not in unesc and not stub_owned:
                missing_postvid.append((l["set"], l["title"], u[:60]))
        if post_hosted and not stub_owned:
            tot_postvid += post_hosted
            shown = len(re.findall(r'src="\./post-\d+-video-thumbnail', raw_html))
            # posters for external post videos use the same naming, so subtract them
            if shown - len(post_vid_urls) < post_hosted:
                missing_postvid.append((l["set"], l["title"],
                                        f"{post_hosted} Skool-hosted post video(s) not rendered"))
        for post in posts:
            pm = post.get("metadata") or {}
            pid = post.get("id")
            tot_posts += 1
            for chunk_txt in plain_visible(pm.get("content") or "").split("\n"):
                t = squash(chunk_txt)
                if len(t) < 8:
                    continue
                if t not in rendered:
                    missing_post.append((l["set"], l["title"], norm(chunk_txt)[:70]))
                    break
            bd = blob_posts.get(pid) or {}
            if not bd or stub_owned:
                continue
            seen = set()
            for window in ("comments_pinned", "comments_tail"):
                data = bd.get(window)
                if not data:
                    continue
                for c in flatten_comments((data.get("post_tree") or {}).get("children")):
                    if c["id"] in seen:
                        continue
                    seen.add(c["id"])
                    body = squash(plain_visible(c["content"]))
                    if len(body) < 8:
                        continue  # trivially short or GIF-only: covered by the gif marker
                    tot_comments += 1
                    if body not in rendered:
                        missing_comment.append((l["title"], norm(c["content"])[:60]))

        # --- resources: external links clickable, gated files present
        for r in ss.parse_resources(meta.get("resources")):
            tot_res += 1
            if r["kind"] == "link":
                # Compare against UNESCAPED markup: a query string's "&" is
                # correctly written as "&amp;" in the href, so a raw-text match
                # reports a false miss on any multi-parameter URL.
                if r["url"] not in H.unescape(raw_html):
                    missing_res.append((l["title"], "external link not linked", r["url"][:60]))
            else:
                fn = r.get("file_name") or ""
                on_disk = (root / folder / fn).exists() if fn else False
                embedded = f'download="{H.escape(fn)}"' in raw_html or f'>{H.escape(fn)}<' in raw_html
                if not (on_disk or embedded):
                    missing_res.append((l["title"], "gated file neither on disk nor embedded", fn))

        # --- post attachments resolved from the blob must be on disk
        for post in posts:
            bd = blob_posts.get(post.get("id")) or {}
            for a in bd.get("attachments_info") or []:
                if not a.get("file_name"):
                    continue
                tot_attach += 1
                if not (root / folder / a["file_name"]).exists() and not stub_owned:
                    missing_file.append(f"{folder}/{a['file_name']} (post attachment)")

        # --- every locally referenced asset must exist
        for src in re.findall(r'<img[^>]+src="\./([^"]+)"', raw_html):
            if not (root / folder / H.unescape(src)).exists():
                missing_file.append(f"{folder}/{src}")
        for href in re.findall(r'<a class="res" href="\./([^"]+)"', raw_html):
            if not (root / folder / H.unescape(href)).exists():
                missing_file.append(f"{folder}/{href}")

    print(f"lessons checked      : {len(man['lessons'])}")
    print(f"body text runs       : {tot_runs}   missing: {len(missing_text)}")
    print(f"inline images        : {tot_imgs}   lessons short: {len(missing_img)}")
    print(f"lessons with a video : {tot_vid}   showing none: {len(missing_video)}")
    print(f"pinned post bodies   : {tot_posts}   missing: {len(missing_post)}")
    print(f"comments             : {tot_comments}   missing: {len(missing_comment)}")
    print(f"resources            : {tot_res}   missing: {len(missing_res)}")
    print(f"post-owned videos    : {tot_postvid}   missing: {len(missing_postvid)}")
    print(f"post attachments     : {tot_attach}")
    print(f"remote image refs    : {len(remote_imgs)}   (must be 0 for a true offline archive)")
    print(f"raw Skool markup left: {len(leftover_markup)}   (must be 0 — e.g. literal [li] on the page)")
    print(f"broken local assets  : {len(missing_file)}")
    for s, t, x in missing_text[:10]:
        print(f"   TEXT  {s} / {t}: {x!r}")
    for t, w, g in missing_img[:10]:
        print(f"   IMG   {t}: expected {w}, rendered {g}")
    for s, t in missing_video[:10]:
        print(f"   VIDEO {s} / {t}")
    for s, t, x in missing_post[:10]:
        print(f"   POST  {s} / {t}: {x!r}")
    for t, x in missing_comment[:10]:
        print(f"   CMT   {t}: {x!r}")
    for t, why, x in missing_res[:10]:
        print(f"   RES   {t}: {why}: {x!r}")
    for s, t, x in missing_postvid[:10]:
        print(f"   PVID  {s} / {t}: {x!r}")
    for t, x in leftover_markup[:10]:
        print(f"   MARKUP {t}: {x!r}")
    for t, x in remote_imgs[:10]:
        print(f"   REMOTE {t}: {x!r}")
    for f in missing_file[:10]:
        print(f"   FILE  {f}")
    bad = (missing_text or missing_img or missing_video or missing_file
           or missing_post or missing_comment or missing_res
           or missing_postvid or remote_imgs or leftover_markup)
    print("\nRESULT:", "FAIL" if bad else "PASS — no content loss detected")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
