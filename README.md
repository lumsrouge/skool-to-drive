# skool-to-drive

Turns a [Skool](https://www.skool.com) classroom you pay for into a **self-contained
offline archive** — one folder per lesson, each with a Skool-styled HTML page, the
lesson body, its discussion posts and comments, downloadable resources, images and
video posters, all cross-linked so the whole course browses offline from `file://`.

Built for personal archival of a community the account owner is a paying member of.

## Layout

| File | Role |
|---|---|
| `skool_scrape.py` | The extractor. `extract_lesson()` does one lesson end to end; `main()` is a thin CLI around it. All rendering lives here. |
| `skool_bulk.py` | Whole-course driver. Enumerates, plans browser batches, renders, resumes. **No rendering logic of its own** — it calls `extract_lesson()`, so bulk and one-off output are identical by construction. |
| `verify_extraction.py` | Correctness check. Walks the cached *source* and asserts every text run, image, video, post, comment and resource reached the HTML. |
| `fetch_browser_data.js` | The one piece that must run in an authenticated browser tab. |
| `SKILL.md` | The real reference: mechanism, every gotcha, both workflows. Start here. |

## Quick start

```bash
python3 skool_bulk.py enumerate "<any lesson URL from the course>" --out "<folder>"
python3 skool_bulk.py render     --out "<folder>" --chunk 0        # browser-free lessons
python3 skool_bulk.py next-chunk --out "<folder>"                  # prints a payload
#   run fetchBrowserData(payload) in a logged-in Chrome tab, save the base64 blob
python3 skool_bulk.py render     --out "<folder>" --chunk N --browser-data blob.json
python3 verify_extraction.py "<folder>"                            # must print PASS
```

Requires `requests` and `browser_cookie3`, plus a Skool session in **Firefox**
(auth) and **Chrome** (the browser step). Chrome/Edge cookies are DPAPI-locked on
Windows and can't be read directly.

## Why it isn't one unattended command

Lesson text, resources, inline images and Skool-hosted video metadata all come
from `www.skool.com` and work fine from plain Python. But comments, post
attachments and gated-file URLs live on `api2.skool.com`, behind an AWS WAF
JS challenge. A `requests` call gets a CloudFront 403 **even when sending
Firefox's own `aws-waf-token` cookie** (re-verified 2026-07-26), while the
identical call from a logged-in browser tab returns 200.

The mitigation is batching, not defeat: `fetch_browser_data.js` takes arrays and
returns blobs keyed by global post/file id, so one browser call serves many
lessons. On the reference course that meant **2 browser round-trips for 107
lessons** — 98 of which needed no browser at all.

## The failure mode this codebase is shaped around

The renderer walks JSON trees, and anything it doesn't recognise it skips. **A
dropped node is indistinguishable from a node that was never there.** So any check
that counts what the renderer produced can only ever confirm the renderer agrees
with itself — it will pass happily while a fifth of the content is missing.

Four content-loss bugs shipped past exactly that kind of check:

1. `bulletList` — a fourth spelling of the list node type, alongside
   `unorderedList` / `bullet_list` / `orderedList`. Rendered as nothing.
2. Most lessons have an **empty `videoLink`** and a Skool-hosted video under
   `videoId` + `pageProps.video`. A `videoLink`-only reader showed no video on
   **89 of 107 lessons**.
3. Skool encodes lists in post/comment text as `[ol:N]` / `[ul]` / `[li]` with no
   closing tags. Passed through, they printed literally to the reader.
4. Posts carry **two** video fields — `videoLinksData` (external) and `videoIds`
   (Skool-hosted). Reading only the first dropped every hosted post video.

Every one was found by a human opening a page, not by a check.

`verify_extraction.py` is the response: it reads the **source**, not the output,
and asserts arrival. Two traps when extending it, both of which produced false
failures here — strip *all* whitespace before comparing text (inline marks split
words across tags, and tag-stripping injects spaces), and compare URLs against
**unescaped** HTML (`&` is correctly written `&amp;` in an href).

## Conventions

- **Videos are never downloaded** — poster frame plus a link. Posters are saved
  locally, so a finished archive has zero remote image dependencies.
- **Only gated content is downloaded.** If it's a public link, it stays a link.
- Lesson folders are self-contained and relative-linked, so the tree can be moved
  or synced anywhere without breaking.
- The manifest at `<folder>/_bulk/manifest.json` is the index: resume state, the
  course-wide folder map, and post ownership for cross-lesson dedup.
