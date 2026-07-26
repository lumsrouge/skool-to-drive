---
name: skool-to-drive
description: >-
  Extract a Skool classroom lesson's resources and content and save them into the
  user's Google Drive (which is synced locally via Google Drive for Desktop). Use
  whenever the user shares a Skool lesson URL (skool.com/.../classroom/...) and
  wants it saved, archived, or "put in Drive" — phrasings like "extract this Skool
  page to Drive", "save this lesson's resources", "download this lesson into my
  drive folder", "put this in skills for Juanca". Works for any Skool classroom
  lesson and any destination folder the user names.
---

# Skool → Google Drive

Pull one Skool classroom lesson (video link + written description + downloadable
resource files) and drop a tidy copy into the user's Google Drive. Because Google
Drive for Desktop is installed, "uploading" is just **copying files into a synced
folder on disk** — fast, no browser upload, no base64 encoding.

## 🚀 Fast path: `skool_scrape.py` (added 2026-07-11 — use this for bulk/repeat extraction)

The full browser-driven workflow below costs ~50k–150k Claude tokens per
comment-heavy lesson, because the AI drives every fetch through the browser MCP
and each tool round-trip re-sends the growing conversation. For extracting many
lessons, use **`skool_scrape.py`** in this skill's folder instead — it hits
Skool's JSON endpoints directly via `requests` and a **Firefox session cookie**
(Chrome/Edge cookies are DPAPI-encrypted on Windows and unreadable this way; see
[[yt-dlp-cookies-firefox]]), and renders the same Skool-dark HTML with **no AI
in the loop** for that part. Net cost per lesson drops to ~2k–6k tokens (just
the browser-fetch step below), not 50k–150k.

**Why it isn't 100% scriptable:** `api2.skool.com` (comments + gated-file
signed-URL minting + GIF metadata) sits behind an **AWS WAF JS-challenge SDK**
(`*.edge.sdk.awswaf.com/.../mp_verify`) — verified live 2026-07-11, a plain
`requests` call with a valid Firefox cookie gets a CloudFront 403 even though
the same cookie works fine against `www.skool.com`. This overturns the older
"Why not a pure offline script" section below for the *lesson content* itself
(that part **is** now solvable via `requests` + Firefox cookie), but comments
and gated files still need one authenticated browser call per lesson.

**Two-step run per lesson:**

1. **Browser step (me, via Chrome DevTools MCP)** — `navigate_page` to the
   lesson, then `evaluate_script` the `fetchBrowserData()` function from
   `fetch_browser_data.js` in this folder, filled in with `posts` (an **array**,
   one entry per pinned post — a module can have more than one, not just
   `pinnedPosts[0]`; each entry is `{postId, groupId, attachmentIds}` from that
   post's `.id`/`.groupId`/`.metadata.attachments`, via a quick pure-Python
   `_next/data` fetch) and `fileIds` (from the lesson's `resources[].file_id`,
   module-level, not per-post). `attachmentIds` per post is a
   **comma-separated string** of file IDs, split it; can be zero, one, or
   several, and any mix of GIF/PNG/JPG, not just a single GIF.
   Then a second `evaluate_script(() => window.__browserDataB64)` call **with
   `filePath` set** to save it. **Must base64-encode before writing to file** —
   verified live 2026-07-11 that the file-write path markdown-escapes
   characters like `(`/`)` (turned a literal `(` into `\(` in real comment
   text), silently corrupting raw JSON. Base64 has no such characters.
2. **Script step (fully scripted, $0 AI cost)** — decode the base64 blob to
   JSON (`base64.b64decode(...)` → `json.loads(...)`), save as
   `browser_data.json`, then:
   ```
   python3 skool_scrape.py "<lesson-url>" --browser-data browser_data.json --out "<folder>"
   ```
   (omit `--out` to auto-detect the Google Drive root and use `--dest` for the
   subfolder name, same convention as the manual workflow's step 2).

**Verified working (2026-07-11) on three real lessons:**
- A comment-heavy pinned-post lesson (55/73 comments, Loom video, main-post
  GIF) — byte-for-byte content parity with the hand-built reference HTML.
- A module-only lesson with 4 gated `n8n`/Make JSON resource files — all 4
  downloaded and validated as real JSON, correctly split between base64
  data-URI (small files) and relative-link (the one file over 200KB) per the
  existing resource-link policy below.
- A pinned-post lesson with **4 screenshot PNGs** attached to the main post
  (not a GIF) — all 4 downloaded and rendered inline; this is what surfaced
  the fact that `metadata.attachments` isn't limited to a single GIF (see
  gotcha below).
- A lesson with a **full separate module lesson body** (2000+ words, 17
  inline images, headings/blockquotes/lists) *plus* a distinct pinned
  discussion post underneath — this surfaced a real architectural bug (see
  gotcha below) where the script discarded the module body whenever a post
  existed. Fixed and re-verified visually against the live page, side by
  side, section by section.

**⚠️ The dominant failure mode: SILENT DROPS (two found live 2026-07-26).**
The renderer walks JSON trees and skips anything it doesn't recognise — and a
dropped node looks *exactly* like a node that was never there. Counting what the
renderer produced therefore **cannot** detect the loss; it only confirms the
renderer agrees with itself. Both bugs below shipped past a "verified" run and
were caught by the user opening a page, not by any check:

- **`bulletList` is a FOURTH list-type spelling** alongside `unorderedList` /
  `bullet_list` / `orderedList`. 22 blocks rendered as nothing. Nested lists,
  `codeBlock` (incl. one inside a blockquote), and `hardBreak` were dropped too.
  `render_prosemirror_desc()` is now properly **recursive** and records unknown
  node types in a `warn` set instead of silently skipping them.
- **Most lessons have NO `videoLink` at all.** 91 of 107 use a *Skool-hosted*
  (Mux-backed) video: empty `metadata.videoLink`, populated `metadata.videoId`,
  with the real data at **`pageProps.video`** (`id` matches `metadata.videoId`).
  A `videoLink`-only implementation showed **no video on 89 of 107 lessons**.
  See `resolve_skool_video()`. The playable stream is signed/short-lived, but
  `pageProps.video.thumbnailUrl` is a plain public `assets.skool.com` link.
  Prefer `https://image.mux.com/<playbackId>/thumbnail.jpg?token=<thumbnailToken>`
  — the identical frame as a ~107 KB JPEG vs a ~1.25 MB PNG (12x smaller; over
  91 lessons that's ~10 MB instead of ~114 MB). Poster is saved locally as
  `video-thumbnail.jpg`; the video itself is still never downloaded, and the
  watch link points at the live (paid-gated) lesson URL.

Three more of the same family, found 2026-07-26 by auditing surfaces the first
checker never touched:

- **A post's own video is NOT an alternative to the module's video.** A pinned
  post can carry `metadata.videoLinksData`; the code used it only as a *fallback*
  when the module had none, so 7 real post videos across 3 lessons vanished.
  `render_post_block()` now takes an optional `video=`. Note `videoLinksData` is
  often the string `"[]"` — truthy but empty — so test the *parsed* list.
- **Skool encodes lists in post/comment text with bracket tokens and no closing
  tags**: a line beginning `[ol:N]` or `[ul]`, items delimited by `[li]`, ending
  at end-of-line. `render_content()` passed them through, so readers saw literal
  `[ol:1][li]First, head over to…` — in **14 of 31 posts and 30 comments**. Use
  `render_rich_content()` (block-level) for post bodies and comment bodies; the
  comment bubble is a `<div class="c-body">`, not a `<p>`, so a list isn't nested
  inside a paragraph.
- **A missing thumbnail used to swallow the whole video.** `video_html()` bailed
  out when `thumb_primary` was empty, dropping the link too. It now degrades to a
  text link. Relatedly, when every poster candidate fails to download (one Loom
  poster now 403s), `localize_video_thumb()` clears the poster rather than
  keeping a remote URL that would render a broken image.

**Posters are saved locally for every provider** so the archive is genuinely
offline — verified 0 remote image references across 107 lessons. For Loom, swap
the oEmbed `.gif` for `.jpg`: same frame, **~64 KB vs ~1.0 MB**.

**Always run `verify_extraction.py <course folder>` after a bulk run.** It walks
the cached *source* JSON and asserts every text run, image and video actually
reached the HTML — the only check that can catch this bug class. When comparing
text, strip **all** whitespace from both sides: an inline mark splits a run
across tags (`download <strong>Loom</strong>`) and tag-stripping injects spaces,
which otherwise produces false "missing text" on the richest-formatted sentences.

**Known gotchas fixed during the build (useful if extending the script):**
- `_next/data` uses `firstName`/`lastName`/`createdAt` (camelCase); the
  `api2.skool.com` comments endpoint uses `first_name`/`last_name`/`created_at`
  (snake_case) — same person, two different field-naming conventions.
  `pinnedPosts[0].post.metadata.title` is the **original post title before it
  was cross-posted into the module** — prefer the **module's own**
  `metadata.title` for the displayed lesson title, not the post's.
- `videoLinksData` (on the post, not the module) is a **JSON string**, not a
  parsed array, and already carries `url`/`thumbnail`/`title`/`len_ms` — no
  need to call Loom's oEmbed API separately if it's present.
- `api2.skool.com/files?ids=` (attachment metadata) returns
  `{files:[{metadata:{...}}]}` — nested one level deeper than it first looks.
  It only reliably accepts **one ID per call** — a comma-joined multi-ID query
  returned `"invalid file IDs"` live — so loop it per attachment.
- `post.metadata.attachments` is a **comma-separated string of file IDs**
  (e.g. `"id1,id2,id3,id4"`), not a single ID or a JSON array — a lesson can
  have any number of attached images, and they aren't all GIFs. Every
  attachment's `metadata.read_url` (same field name regardless of file type)
  is a standalone public `assets.skool.com` link — downloads via plain
  `requests`, no cookie.
- Long lesson titles nested under a Drive path can exceed Windows' 260-char
  `MAX_PATH`; the script uses the `\\?\` extended-length path prefix on every
  filesystem write to avoid it.
- **A module's own lesson body (`metadata.desc`) and a pinned discussion post
  are two separate things that can BOTH exist on one lesson** — not
  alternatives. The script always renders the module body first (its own
  video takes priority), then the pinned post as its own labeled card, then
  comments. Never render one instead of the other.
- Real `desc` JSON uses **camelCase** list node names (`unorderedList`, not
  `bullet_list`) plus a `blockquote` node type — both must be handled or that
  content silently disappears.
- Inline `image` nodes inside `desc` are **public** (`assets.skool.com`,
  direct `attrs.src`, no cookie/WAF) — downloadable via plain `requests`
  entirely inside the script, no browser-fetch step needed for these.
- **A module can have more than one pinned post underneath it, not just one.**
  `find_pinned_posts()` returns the full list (`page_props["pinnedPosts"]`),
  and the script loops over every entry, rendering each as its **own**
  self-contained card with its **own** comments/attachments nested directly
  inside it (labeled "Pinned discussion post (N of M)" when there's more than
  one). Never pool multiple posts' comments into one shared section — the
  `browser_data.json` shape reflects this too: `posts` is keyed by post id
  (`{comments_pinned, comments_tail, attachments_info}` per post), not one
  flat top-level blob.

For a **one-off** lesson, or if Skool changes its markup and the script needs
re-diagnosing, fall back to the full manual workflow below.

## 📚 Whole-course path: `skool_bulk.py` (added 2026-07-26)

Extracts an **entire classroom** by calling `skool_scrape.extract_lesson()` — the
exact same code path a single lesson uses, so bulk and one-off output are
identical by construction. `skool_bulk.py` contains no rendering logic.

**The key economy:** `fetch_browser_data.js` already takes **arrays** and returns
blobs keyed by *global* post/file id, and `extract_lesson()` reads them as
`posts_data.get(post_id)` / `signed_urls.get(file_id)`. So **one browser call can
serve many lessons** — a superset blob works with the existing JS and Python
unchanged. That turns ~107 browser round-trips into ~10. A post shared by two
lessons is also fetched only once, since the blob is keyed by post id.

**Still semi-attended, not fire-and-forget.** Re-verified 2026-07-26: sending
Firefox's own `aws-waf-token` cookie from `requests` still gets a CloudFront 403
on `api2.skool.com`, while the identical call from an authenticated browser tab
returns 200. The browser step cannot be scripted away — don't re-investigate
without new evidence.

```bash
# 1. Map the course: caches every lesson's JSON, builds the folder map,
#    plans chunks, and prints a duplicate-content census. Pure Python, $0.
python3 skool_bulk.py enumerate "<any lesson URL from the course>" --out "<folder>"

# 2. Chunk 0 is every browser-free lesson — render it right away, no browser.
python3 skool_bulk.py render --out "<folder>" --chunk 0

# 3. For each remaining chunk: get the payload, run it in the browser, render.
python3 skool_bulk.py next-chunk --out "<folder>"
#    -> evaluate_script fetchBrowserData(<payload>) on an authenticated Skool tab
#    -> evaluate_script `() => window.__browserDataB64` with filePath set
python3 skool_bulk.py render --out "<folder>" --chunk N --browser-data blob.b64

python3 skool_bulk.py status --out "<folder>"
```

**Chunks are packed by in-page fetch count, not lesson count** (default budget
70). Each post costs 2 (both comment windows), each attachment and gated file 1.
One real Maker School lesson has **8 pinned posts = 19 fetches on its own**, so
packing by lesson count would produce wildly uneven chunks and risk a chunk
running past the tool timeout.

**The manifest (`<folder>/_bulk/manifest.json`) is the index**, and it does three
things:
- **Resume/idempotence** — a lesson marked `done` is skipped on re-run
  (`--force` overrides). It's saved after *every* lesson, so a crash mid-run
  resumes cleanly.
- **Post dedup** — the first lesson to claim a post owns it and renders it in
  full; any other lesson pinning the same post gets a compact card linking to
  `../<owner folder>/<owner folder>.html` instead of duplicating a 100-comment
  thread. See `render_post_stub()`.
- **Course-wide folder map** — fixes a latent collision: `sanitize_folder_name()`
  truncates at 60 chars, so two lessons agreeing in their first 60 characters
  would collapse into one folder, the second silently overwriting the first.
  Measured on Month 1: 107 lessons, longest name **59 chars**, zero collisions —
  one character of headroom. A single-lesson run structurally cannot see this;
  only a course-wide pass can.

`enumerate` prints a **duplicate-content census** (posts/gated files/inline
images appearing on more than one lesson) so dedup effort is sized to what's
actually there rather than assumed.

`_bulk/pages/` caches every lesson's raw JSON (~50 MB for 107 lessons) so
rendering never re-fetches. It's a working artifact — safe to delete once the
course is fully extracted; the lesson folders are self-contained without it.

## ⚠️ Mechanism update (verified 2026-07-10 — READ FIRST)

Skool changed since this skill was written. Corrections that override the steps below:

- **`__NEXT_DATA__` no longer holds the lesson.** Fetch it from the Next.js data endpoint instead:
  `/_next/data/<buildId>/<group>/classroom/<course>.json?md=<moduleId>&group=<group>&course=<course>`
  via `fetch(url,{headers:{'x-nextjs-data':'1'},credentials:'include'})`. Find the module node by
  `id===<moduleId>`; its `metadata` has `title/desc/videoLink/videoLenMs/resources`, where
  `resources` and `desc` are **stringified JSON** (parse them). Field names are otherwise unchanged.
- **A programmatic `.click()` on the Download button now works** and mints a CloudFront **signed URL**
  on `files.skool.com` (standalone — valid with no cookie for a short time). The "trusted click only"
  note below is stale for the file step.
- **Tool choice for bulk (corrected):** the built-in browser is **NOT sandboxed** — a real *trusted* click (computer
  tool by ref; not a JS `.click()`) on Download opens a native **Save As** dialog defaulting to the real `Downloads`
  folder and saves to local disk. The real variable is the **"ask where to save each file" setting**: if ON, every
  download pops a native dialog that NOTHING automatable can click — browser MCP tools only touch page content, and
  computer-use can never control the Claude app's own window (where the built-in browser + dialog live). The built-in
  browser also blocks `chrome://settings`, so the prompt can't be disabled. → built-in browser Download button is a
  dead end for unattended bulk. Use **real Chrome with auto-download ON** (Chrome DevTools MCP / Claude-in-Chrome):
  files land silently in `Downloads` → read from disk → copy to Drive at zero token cost. Videos → `yt-dlp`.
  (Alternative for a few small files without Chrome: built-in browser in-page `fetch` of the signed URL → return
  bytes → Write to disk; no dialog, but token-heavy at scale.)

## Prerequisites (check once)

- **FIRST, before anything else:** check `list_connected_browsers`. If it returns
  `[]`, immediately tell the user **"Please open Google Chrome (with the Claude
  extension) so I can reach your logged-in Skool tab"** and wait — don't start the
  rest of the workflow until a browser is connected.
- **Chrome MCP** connected, and the browser already **logged into Skool** with the
  user's paid access. (Load Chrome MCP tools via ToolSearch `query:"computer-use"`
  / `select:` if they're deferred.)
- **Google Drive for Desktop** running and synced (see step 2 for detection).
- The work is browser + filesystem; no Drive *API/MCP* upload is needed anymore.

## Workflow

### 1. Retrieve the lesson (Chrome MCP)

Why the browser at all? See **"Why not a pure offline script"** below — the short
version is that the page is behind the user's paid login and the files download
from short-lived *signed* URLs, so retrieval must ride the already-logged-in tab.
The goal here is speed: do the reading in **one shot**, not by exploring the page.

**a) Read everything in ONE `javascript_tool` call.** Don't click around to
discover structure — it's all in the embedded Next.js blob. Run this once and get
back clean JSON:

```js
// one call on the lesson tab — returns title/desc/video + resource list
(() => {
  const j = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
  let hit = null;
  (function walk(o){
    if (o && typeof o === 'object') {
      if (o.metadata && o.metadata.title && Array.isArray(o.metadata.resources)) hit = o;
      for (const k in o) walk(o[k]);
    }
  })(j.props.pageProps);
  const m = hit.metadata;
  return JSON.stringify({
    title: m.title, desc: m.desc, videoLink: m.videoLink,
    videoLenMs: m.videoLenMs,
    resources: (m.resources||[]).map(r => ({
      title: r.title, file_id: r.file_id, file_name: r.file_name,
      type: r.file_content_type
    }))
  });
})()
```

That's the entire text + links + resource manifest, instantly. (If output comes
back blank, it tripped the token filter — stash it in `window.__x` and read it
back in a second fetch-free call. See Gotchas.)

**b) Download the resource files — the one irreducible manual step.** Resources
render as React `ResourceWrapper` elements with **no href**, and the file bytes
only exist behind a signed S3 URL minted on demand. A synthetic JS `.click()` does
nothing. You must do a **real (trusted) click** with the Chrome `computer` tool on
the resource row → a "No preview available" modal opens with a **DOWNLOAD** button
→ click DOWNLOAD → the file lands in `~/Downloads` (`C:\Users\<user>\Downloads`).
The modal does **not** auto-close — close it (X, top-right) before the next one.

Speed tips for this step:
- Do it with **`computer_batch`**, one batch per file: `[click row → wait → click
  DOWNLOAD → click close-X]`. Batching avoids a model round-trip between each
  sub-click, which was most of the old slowness.
- The download itself is fast; only the click choreography costs time. There are
  usually only a handful of resources, so this is seconds, not minutes, once batched.

After downloading, validate each file (e.g. it parses / has expected size) and
note the exact `file_name`s — you'll copy those specific files next.

### 2. Decide the destination (two flexible inputs)

**a) Synced Drive root — auto-detect, don't hardcode a drive letter.**
If the user already gave a folder path, use it. Otherwise detect the Google Drive
for Desktop mount:

```powershell
# confirm it's running
Get-Process GoogleDriveFS -ErrorAction SilentlyContinue
# find the mount: scan filesystem drives for a My Drive (or localized) folder
Get-PSDrive -PSProvider FileSystem | ForEach-Object {
  foreach($n in 'My Drive','Mon Drive'){ $p = "$($_.Name):\$n"; if(Test-Path $p){ $p } }
}
# also possible: a "<root>\Shared drives\..." for shared drives
```

The drive letter varies per machine and the folder name may be localized
(`Mon Drive` in French). On this machine today it resolves to **`G:\My Drive`**.
If detection fails, ask the user for the path.

**b) Destination folder name — flexible, not fixed.**
Take the top-level folder name from the user's request. If they didn't specify,
ask, offering the last-used name (**"skills for Juanca"**) as the default. Don't
assume it.

### 3. Copy into the synced folder (the upload)

Create `<DriveRoot>\<Destination>\<Lesson Title>\` and copy the downloaded files
in. Sanitize the lesson title for a folder name (strip `\ / : * ? " < > |`).

```powershell
$dest = "G:\My Drive\skills for Juanca\AI Graphic Designer System"  # example
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item "$env:USERPROFILE\Downloads\<file_name>" -Destination $dest
```

Google Drive for Desktop uploads to the cloud automatically. **No base64, no
browser upload.** (base64 via a Drive MCP `create_file` is retired — only worth it
if Drive for Desktop is ever unavailable.)

### 3.5 Extract every link, every comment, and main-post GIFs (added 2026-07-11)

Three gaps found by the user comparing the snapshot to the real page — fold all
three in before writing the HTML, not after:

**a) Every hyperlink in the body text must survive, not just the plain text.**
Skool stores rich text two different ways depending on whether you're reading a
**module** (`metadata.desc`) or a **community post** (`metadata.content`) — you
need both parsers, because a lesson backed by a pinned post (see step 1a) mixes
them:

- **Module `desc`** is `"[v2]" + JSON.stringify(prosemirrorNodes)`. A link is
  **not a separate field** — it's a `marks` entry on a text run:
  `{"type":"text","marks":[{"type":"link","attrs":{"href":"https://..."}}],"text":"full length course here"}`.
  Walking the node tree and reading only `.text` (as earlier versions of this
  skill did) **silently drops every hyperlink** — verified 2026-07-11, a link on
  "full length course here" → a YouTube URL was missing from the HTML until
  caught. Render any text run carrying a `link` mark as `<a href="...">`.
- **Post `content`** (pinned-post pattern, step 1a) is a **plain string** using
  inline markdown-style links and mentions, not ProseMirror JSON:
  `"...PS—[link to copy](https://docs.google.com/...)."` and
  `"Thx to [@Mikael Nylander](obj://user/<id>) for..."`. Regex for both:
  - `\[([^\]]+)\]\((https?://[^)]+)\)` → `<a href="$2">$1</a>` (a real link)
  - `\[@([^\]]+)\]\(obj://user/[^)]+\)` → plain `@Name` text, **not** a link
    (it's an internal user reference, not a URL — linking it to nothing/a
    broken `obj://` URI is wrong)
  - Any bare `https?://...` left over after that (comments especially — see
    below — often contain a raw pasted URL with no markdown brackets) → linkify
    it too.

**b) Extract every comment on the post, not just the post body.** If the lesson
is backed by a pinned community post (step 1a), it has a comment thread the
earlier version of this skill never touched. Fetch it directly:

```
GET https://api2.skool.com/posts/<post_id>/comments?group-id=<group_id>&limit=25&pinned=true   (oldest batch)
GET https://api2.skool.com/posts/<post_id>/comments?group-id=<group_id>&limit=25&tail=true      (a second, later batch)
```
(`post_id` = `pinnedPosts[0].post.id`; `group_id` = the post's `groupId`, both
from the step-1a fetch.) Each returns `{post_tree:{children:[{post, children}]}}`
— `post.metadata.content` is the comment text (plain-string format, same
mention/link handling as above), `post.user.first_name/last_name` the author,
`post.created_at` the date, `post.metadata.upvotes` the like count. A comment's
own `children` array holds its replies — recurse and indent them.

**Known limitation — verified 2026-07-10/11, don't re-attempt without new
evidence:** these two calls are the *only* two working windows. `limit` only
accepts `25` (any other value 400s). Extensively tried and all failed to reach
the middle of a long thread: `offset=N`, `last=<cursor from response>`,
`after=<ISO timestamp>`, `start_after=`, `cursor=<comment id>`, `after_id=`,
`page=2` — every one of them silently re-returns page 1. Real UI scrolling
(`window.scrollBy` in a loop) triggers **no further network request** either —
the live site itself appears to only ever load these same two windows for a
long thread. **Practical result:** for threads ≤ ~50 comments you'll get all of
them (the two windows don't overlap and don't leave a gap). For longer threads
there will be a middle gap you cannot reach — merge+dedupe what the two calls
return by comment `id`, and **disclose the gap explicitly** in the HTML: "Showing
N of \<total from the `73 comments` UI count\> comments — Skool's API has no
reachable pagination past the first/last 25." Don't silently under-report.

**c) GIFs: main post = copy it; comments = never copy it, note it.** A GIF is
technically a public Giphy/Tenor URL, which would normally fall under the
public-link-only policy below — but the user carved out an explicit exception
2026-07-11: a GIF attached directly to **the lesson's main post** (check
`pinnedPosts[0].post.metadata.attachments` → `GET api2.skool.com/files?ids=<id>`
→ `content_type: image/gif`) should be **downloaded and copied into the lesson
folder** like any gated resource (`curl` the `read_url` — it's a plain public
URL, no auth needed — save as `<file_name>.gif`, embed with
`<img src="./<file_name>.gif">`). A GIF attached to an individual **comment**
(`post.metadata.attachments_data` inside a comment node, same shape) is
decorative reaction noise on someone else's reply — **do not download or embed
it**; just note inline that the comment had one, e.g. a small
`<span class="gif-note">[GIF reaction — not copied, per policy]</span>` next to
that comment's text so a blank-looking comment doesn't look like a bug.

### 4. Add an HTML snapshot of the page

Write a clean, self-contained `<Lesson Title>.html` into the same sub-folder
containing: the title + duration, a **video thumbnail**, the description, and a
list of the resource files.

**Style it as a Skool-dark reskin, not generic light-mode HTML (added
2026-07-11).** The snapshot should visually read like the live Skool page, not
like raw markup — the user compared the two directly and asked for it. Verified
design tokens (pulled straight from the live DOM via chrome-devtools) and the
full template are below. Scope is **post pane only**: recreate the post/module
card and comment thread on Skool's dark canvas — skip the top nav, left module
sidebar, and right community rail, since those are dead app chrome for an
archived single lesson.

**Tokens:**

| Token | Value |
|---|---|
| Page canvas | `#222222` |
| Post-card fill | `#262626` (module-only page, no post: `#333333`) |
| Card border | `1px solid rgba(255,255,255,.09)`, radius `16px` (module card: `10px`) |
| Primary text | `#e4e4e4` |
| Names / titles | `#ffffff` (module title: `#e4e4e4` is fine), weight **700** |
| Meta gray (date, category, "N of M comments") | `#909090`, 13px |
| Accent (links, `@mentions`) | `#93b3f3`, no underline, underline on hover |
| Post/module title | 23px / 700 |
| Comment bubble | fill `#222`, border `1px solid rgba(255,255,255,.09)`, radius **15px**, padding `8px 13px` |
| Font | keep `-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif` — no external font loads, must render offline from `file://` |

**Post-card layout** (when the lesson is backed by a community post — step 1a):
avatar (initials, see below) + author name (white 700) + badges, a muted
`date · category` sub-line, the 23px title, the body paragraphs (mentions/links
per step 3.5a), the video thumbnail, the main-post GIF if any, a resources note,
then a static muted `👍 <count> · 💬 <count> comments` line echoing Skool's
action row (non-interactive — it's a snapshot, not a live page). Comments render
**below** the card (not inside it) as individual bordered bubbles, matching the
live site.

**Module-only layout** (no backing post — plain `metadata.desc` content, e.g. an
"About & how to use" page): a single `#333` card, no author header, 23px title,
20px/500 subheadings, body paragraphs — no comments/actions row, since a plain
module page has none on the live site.

**Avatars are deterministic initials, not real photos** (added 2026-07-11): a
36–44px circle, background color hashed from the person's name against a small
fixed palette (e.g. `#4d7ea8 #3f9e6a #c9822f #8a5fc4 #c4507a #4f9b9b #b0574c
#5a7fd6`), initials in white. Pulling real avatar images would mean extra
scraping and breaks offline `file://` rendering for no real gain — this is a
close-enough visual stand-in. Skip the numeric level badge (that data isn't
captured by step 1a/1c).

**Comment rows:** flex row of avatar + bubble, indent the **whole row** (not
just the bubble) by `44px` per reply depth, matching Skool's reply nesting. A
muted `👍 <upvotes> · Reply` line sits under the bubble, outside its border
(static, matching the live "👍 1  Reply" affordance).

**Use a clickable static thumbnail, NOT an `<iframe>` embed** (verified
2026-07-10). A `youtube-nocookie.com` iframe throws **"Error 153 — Video player
configuration error"** when opened from a `file://` origin — which is exactly
how these snapshots get viewed (double-clicked locally, or synced into Drive
and opened from the local Drive folder — still `file://`). A static thumbnail
image has no such restriction and always renders:

```html
<a class="thumb-link" href="<original videoLink>" target="_blank">
  <img src="https://i.ytimg.com/vi/<VIDEO_ID>/maxresdefault.jpg"
       onerror="this.onerror=null;this.src='https://i.ytimg.com/vi/<VIDEO_ID>/hqdefault.jpg'"
       alt="Video thumbnail">
  <div class="play"></div>
</a>
<p><a href="<original videoLink>">Open on YouTube</a> — "<video title>"</p>
```

```css
.thumb-link{display:block;position:relative;max-width:100%;margin:16px 0;border-radius:12px;overflow:hidden}
.thumb-link img{display:block;width:100%;height:auto}
.play{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:68px;height:48px;background:rgba(0,0,0,.75);border-radius:14px;display:flex;align-items:center;justify-content:center}
.play::after{content:"";border-style:solid;border-width:12px 0 12px 20px;border-color:transparent transparent transparent #fff;margin-left:4px}
```

`maxresdefault.jpg` doesn't exist for every video (older/small uploads) — the
`onerror` fallback to `hqdefault.jpg` (always exists) handles that.

**Downloaded resource files must be real clickable links, with a RELATIVE
href** (verified 2026-07-10 — user caught a plain `<div>` with no link):

```html
<a class="res" href="./<file_name>" download><file_name> (<size>, <description>)</a>
```

```css
.res{display:block;padding:10px 13px;background:#2e2e2e;border:1px solid rgba(255,255,255,.09);border-radius:10px;margin:6px 0;color:#e4e4e4;text-decoration:none;font-size:14px}
.res:hover{border-color:#555}
```

Use `./<file_name>` (relative to the HTML file, same folder) — **not** an
absolute path. The whole point of the sub-folder is that HTML + resource files
travel together; a relative link keeps working after the folder is copied into
Drive or moved anywhere else, while an absolute path breaks immediately.

**For TEXT resources (json/py/txt/md/csv/js/yaml/etc.), make the link actually
trigger a download — don't rely on `download` + a relative `file://` href**
(verified 2026-07-10). Chrome **ignores the `download` attribute entirely on
`file://`-origin links** — clicking just navigates the tab to the raw file
instead of downloading it, and there is no dialog, no error, nothing to
indicate it silently didn't work. The fix: embed the file as a **base64
`data:` URI** — Chrome honors `download` on `data:` URIs regardless of origin,
so the click reliably triggers a real, silent download (byte-identical copy,
correct filename), confirmed via md5 diff:

```html
<a class="res" href="data:<mime-type>;base64,<BASE64_CONTENT>"
   download="<file_name>"><file_name> (<size>, <description>)</a>
```

Do the base64 encoding and the string substitution into the HTML **with a
script (Python/PowerShell), not by pasting the encoded blob through a chat/edit
tool** — a data URI shows up in full in DOM snapshots and inflates context for
no reason. Common mime types: `.json`→`application/json`, `.py`→`text/x-python`,
`.txt`/`.md`→`text/plain`, `.csv`→`text/csv`, `.js`→`text/javascript`;
default to `text/plain` if unsure.

Still **keep the real standalone file** in the sub-folder alongside the HTML
too (per step 1b/3) — the data URI makes the *HTML's* link work correctly, it
doesn't replace having an actual file on disk for direct use / Drive sync.

This data-URI approach doesn't scale to large/binary files (base64 adds ~33%
size and bloats the HTML) — it's for the small text-based resources these
lessons typically attach (workflow JSON, scripts, prompts). For anything
bigger, the relative `./<file_name>` link is the fallback (it still opens/saves
correctly via right-click → Save As, just not via a bare click).

**Before reporting done, preview the generated HTML yourself** — the built-in
browser pane refuses `file://` navigation, so use the connected Chrome
DevTools MCP (`navigate_page` to the `file:///...` path, then
`take_screenshot`) to actually look at the rendered page and catch broken
embeds like the iframe issue above, rather than assuming the markup is correct.

**Video policy:** the lesson video is normally a **YouTube embed, not a
Skool-hosted file** — show the thumbnail + link; do **not** download the MP4
(YouTube ToS, size). Just `Set-Content`/write the HTML file directly into the
synced folder.

**General public-link policy (added 2026-07-10 — applies beyond video):** if a
resource is hosted somewhere **publicly accessible to anyone who has the link**
(YouTube, Vimeo, Loom, a public Google Drive/Docs link, etc.), do **not**
download it. Just record the link (title + URL, embedded where sensible) in the
HTML snapshot — same treatment as the YouTube video above. Only download and
copy into Drive the resources that actually required the authenticated Skool
session to obtain (e.g. the JSON/file attachments minted via the signed-URL
flow in step 1b) — those aren't reachable without the paid membership, so they
need a real local/Drive copy. Rule of thumb: **if it's a link, link it; only
download what's actually gated.** (One narrow exception: a GIF attached
directly to the lesson's main post — see step 3.5c — is copied locally even
though it's technically a public Giphy/Tenor URL, per explicit user instruction
2026-07-11. Comment GIFs are the opposite: never copied, even though they're
the same kind of URL — the distinction is main-post vs. comment, not
public-vs-gated.)

### 5. Report

List what landed in the sub-folder and confirm the Drive app shows it syncing /
synced (`Get-ChildItem` the folder). Give the user the local path and, if handy,
the Drive web link.

## Why not a pure offline script (investigated — don't redo this)

Tempting idea: "just have a Python script fetch the page and the files instantly."
It was investigated properly and **it does not work**. Two independent walls, both
about **authentication**:

1. **The files come from short-lived *signed* URLs.** Probed directly:
   - `api.skool.com/files/{file_id}` → `OPTIONS` reports `Allow: DELETE, OPTIONS`.
     `GET`/`POST`/`HEAD`/`PUT`/`PATCH` all return **405**. It is *not* a download
     endpoint.
   - `assets.skool.com/files/{file_id}` → **403** `<Code>AccessDenied</Code>` from
     **AmazonS3**; `files.skool.com/{file_id}` → **403** from **CloudFront**.
   So the bytes sit in S3 and are only reachable with a signature that Skool mints
   per-request for an authenticated session. There is no guessable static URL.
   (`/files/{fid}/download`, `/signed-url`, `/url`, `/files/download/{fid}` → 404.)

2. **A script can't borrow the login.** The lesson page itself is paid-gated, so
   even reading `__NEXT_DATA__` needs the session cookie. Chrome on Windows now
   protects cookies with **App-Bound Encryption**, so `browser_cookie3` &co
   generally **cannot** decrypt them. (Neither `requests` nor `browser_cookie3` is
   installed here anyway.) Firefox cookies *are* readable — so a Python path is
   only conceivable if the user logs into Skool in **Firefox**, and even then you
   would still have to reverse-engineer the signed-URL endpoint from wall #1.

**Conclusion:** ride the already-authenticated Chrome tab. The win is not
"avoid the browser" — it's **one JS call for all text/links/metadata** (step 1a)
and **batched clicks** for the files (step 1b). That removes the exploration and
the per-click round-trips, which is where the time actually went.

## Gotchas (hard-won)

- **Don't explore the page — go straight to `__NEXT_DATA__`.** Reading the DOM,
  hunting for links, or clicking to "see what happens" is what made this slow the
  first time. One JS call (step 1a) yields the whole manifest.
- **Auto-detect the Drive root every time** — never assume `G:` or `My Drive`;
  letters vary and names localize.
- **Trusted clicks only** for Skool resources (Chrome `computer` tool). Synthetic
  `.click()` won't trigger the download. The download modal doesn't auto-close.
- **Batch the click choreography** (`computer_batch`) instead of one tool call per
  sub-click — the round-trips, not the downloads, were the bottleneck.
- **Close the modal before clicking the next resource.** If you don't, the next
  click lands on the backdrop and that file is silently skipped. Verify the file
  count in `~/Downloads` matches the manifest before copying.
- **`javascript_tool` output is silently blanked (`{}`)** when the returned value
  contains cookie/token-like data (fetch responses that set cookies, signed URLs).
  Strip URLs/hex tokens from what you return, or stash in `window.__x` and read it
  back in a separate fetch-free call.
- The lesson **video is YouTube**, embedded — not a downloadable Skool file.
- Reuse an existing destination sub-folder rather than duplicating; name it after
  the page title.
