"""
Bulk course extractor -- runs skool_scrape.py's single-lesson pipeline across an
entire Skool classroom, resumably, without duplicating shared content.

This file contains NO rendering logic. Every lesson is produced by
skool_scrape.extract_lesson(), the exact same code path a single-lesson run
uses, so bulk output and one-off output stay identical by construction.

Why it is semi-attended rather than one command: comments, post attachments and
gated-file signed URLs live on api2.skool.com, which sits behind an AWS WAF JS
challenge that a plain `requests` session cannot solve (re-verified 2026-07-26 --
even sending Firefox's own `aws-waf-token` cookie still returns CloudFront 403).
Those pieces need an authenticated browser call. But fetch_browser_data.js takes
ARRAYS and returns blobs keyed by global post/file id, so one browser call can
cover many lessons at once -- turning ~107 browser round-trips into ~10.

Workflow:
    python3 skool_bulk.py enumerate <lesson-url> --out <folder>
    python3 skool_bulk.py next-chunk --out <folder>          # prints the JS payload
    ... run fetchBrowserData(payload) in the browser, save the base64 blob ...
    python3 skool_bulk.py render --out <folder> --chunk N --browser-data blob.b64
    python3 skool_bulk.py status --out <folder>

Chunk 0 is always the browser-free lessons (module body + inline images +
external links only) -- render it immediately, no browser needed.
"""
import argparse
import base64
import binascii
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import skool_scrape as ss

MANIFEST_VERSION = 1
# In-page fetches per chunk. Each post costs 2 (both comment windows), each
# attachment 1, each gated file 1. One real lesson in Maker School has 8 pinned
# posts = 19 fetches on its own, so packing by lesson count would produce wildly
# uneven chunks. Deliberately conservative -- a chunk runs as a single
# evaluate_script call and must finish well inside the tool timeout.
DEFAULT_FETCH_BUDGET = 70


# ---------------------------------------------------------------- manifest

def bulk_dir(out_root):
    return Path(out_root) / "_bulk"


def manifest_path(out_root):
    return bulk_dir(out_root) / "manifest.json"


def load_manifest(out_root):
    p = manifest_path(out_root)
    if not p.exists():
        sys.exit(f"No manifest at {p}. Run `enumerate` first.")
    return json.loads(p.read_text(encoding="utf-8"))


def save_manifest(out_root, man):
    p = manifest_path(out_root)
    ss.long_path(p.parent).mkdir(parents=True, exist_ok=True)
    ss.long_path(p).write_text(json.dumps(man, indent=2, ensure_ascii=False), encoding="utf-8")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- folder map

def build_folder_map(nav):
    """Assign every lesson its output folder name ONCE, course-wide, resolving
    collisions that a single-lesson run structurally cannot see.

    sanitize_folder_name() truncates at 60 chars, and lesson titles repeat
    verbatim across days (that's why lesson_folder_name() prefixes the set
    title). Two lessons whose "Day N - Title" agree in their first 60 characters
    therefore collapse to one folder -- the second silently overwriting the
    first, with every sidebar link pointing at whichever won. Measured on Maker
    School's Month 1: 107 lessons, longest name 59 chars, zero collisions today
    -- one character of headroom, so this guard is cheap insurance rather than
    a fix for a currently-broken thing.
    """
    fmap, taken, collisions = {}, {}, []
    for s in nav["sets"]:
        for m in s["modules"]:
            name = m["folder"]
            if name in taken:
                suffix = " ~" + m["id"][:6]
                name = name[:ss.MAX_FOLDER_NAME_LEN - len(suffix)].rstrip(". ") + suffix
                collisions.append({"lesson": m["title"], "clashed_with": taken[m["folder"]],
                                   "resolved_to": name})
            taken[name] = m["title"]
            fmap[m["id"]] = name
    return fmap, collisions


# ---------------------------------------------------------------- enumerate

def lesson_fetch_plan(page_props, meta):
    """What this lesson needs from the browser: its posts (each costing 2 comment
    fetches plus one per attachment) and its gated resource file ids."""
    posts = []
    for p in ss.find_pinned_posts(page_props):
        pm = p.get("metadata", {})
        posts.append({
            "postId": p.get("id"),
            "groupId": p.get("groupId"),
            "attachmentIds": ss.parse_attachment_ids(pm.get("attachments")),
            "comments": pm.get("comments", 0),
        })
    file_ids = [r["file_id"] for r in ss.parse_resources(meta.get("resources"))
                if r["kind"] == "gated" and r.get("file_id")]
    cost = sum(2 + len(p["attachmentIds"]) for p in posts) + len(file_ids)
    return posts, file_ids, cost


def cmd_enumerate(args):
    out_root = Path(args.out)
    session = ss.get_session()
    group, course, md0 = ss.parse_lesson_url(args.url)
    build_id = ss.fetch_build_id(session, group, course)
    print(f"Course {group}/{course}  buildId={build_id}")

    page_props, build_id = ss.fetch_lesson_json_retrying(session, build_id, group, course, md0)
    course_tree = page_props.get("course")
    nav = ss.build_nav(course_tree, md0)
    if not nav:
        sys.exit("Couldn't read the course tree — check the URL.")
    print(f"Course: {nav['title']!r} — {nav['total']} lessons in {len(nav['sets'])} sets")

    folder_map, collisions = build_folder_map(nav)

    pages_dir = bulk_dir(out_root) / "pages"
    ss.long_path(pages_dir).mkdir(parents=True, exist_ok=True)

    lessons, plans = {}, {}
    post_seen, file_seen, image_seen = {}, {}, {}
    all_mods = [(s["title"], m) for s in nav["sets"] for m in s["modules"]]

    for i, (set_title, m) in enumerate(all_mods, start=1):
        md = m["id"]
        cache = pages_dir / f"{md}.json"
        if cache.exists() and not args.refresh:
            pp = json.loads(ss.long_path(cache).read_text(encoding="utf-8"))
        else:
            pp, build_id = ss.fetch_lesson_json_retrying(session, build_id, group, course, md)
            ss.long_path(cache).write_text(json.dumps(pp, ensure_ascii=False), encoding="utf-8")
            time.sleep(args.delay)
        module = ss.find_module_node(pp.get("course"), md)
        meta = (module or {}).get("metadata", {})

        posts, file_ids, cost = lesson_fetch_plan(pp, meta)
        plans[md] = {"posts": posts, "fileIds": file_ids, "cost": cost}

        # Collision census -- measures the user's cross-referencing concern
        # instead of assuming it. Dedup paths are only worth wiring up for
        # vectors that actually fire.
        for p in posts:
            post_seen.setdefault(p["postId"], []).append(md)
        for fid in file_ids:
            file_seen.setdefault(fid, []).append(md)
        for node in _iter_desc_images(meta.get("desc")):
            image_seen.setdefault(node, []).append(md)

        lessons[md] = {
            "title": m["title"], "set": set_title, "folder": folder_map[md],
            "status": "pending", "needs_browser": cost > 0, "fetch_cost": cost,
            "posts": len(posts), "gated_files": len(file_ids),
            "extracted_at": None, "error": None,
        }
        print(f"  [{i}/{len(all_mods)}] {m['title'][:58]:<58} posts={len(posts)} files={len(file_ids)} cost={cost}")

    chunks = build_chunks(lessons, plans, args.budget)

    man = {
        "version": MANIFEST_VERSION, "group": group, "course": course,
        "build_id": build_id, "course_title": nav["title"],
        "generated_at": now_iso(), "out_root": str(out_root),
        "folder_map": folder_map, "collisions": collisions,
        "lessons": lessons, "chunks": chunks,
        "posts": {}, "files": {},
    }
    save_manifest(out_root, man)

    dup_posts = {k: v for k, v in post_seen.items() if len(v) > 1}
    dup_files = {k: v for k, v in file_seen.items() if len(v) > 1}
    dup_imgs = {k: v for k, v in image_seen.items() if len(v) > 1}

    print("\n" + "=" * 62)
    print(f"Lessons              : {len(lessons)}")
    print(f"  browser-free       : {sum(1 for l in lessons.values() if not l['needs_browser'])}")
    print(f"  need browser       : {sum(1 for l in lessons.values() if l['needs_browser'])}")
    print(f"Total in-page fetches: {sum(p['cost'] for p in plans.values())}")
    print(f"Chunks               : {len(chunks)}  (chunk 0 = browser-free, render now)")
    print(f"Folder collisions    : {len(collisions)}")
    for c in collisions:
        print(f"    {c['lesson']!r} clashed with {c['clashed_with']!r} -> {c['resolved_to']!r}")
    print("--- duplicate-content census ---")
    print(f"Posts pinned on >1 lesson : {len(dup_posts)}")
    for pid, mds in list(dup_posts.items())[:10]:
        print(f"    post {pid[:8]} on {len(mds)} lessons: {', '.join(lessons[x]['title'][:30] for x in mds[:3])}")
    print(f"Gated files on >1 lesson  : {len(dup_files)}")
    for fid, mds in list(dup_files.items())[:10]:
        print(f"    file {fid[:8]} on {len(mds)} lessons")
    print(f"Inline images on >1 lesson: {len(dup_imgs)}")
    print("=" * 62)
    print(f"\nManifest: {manifest_path(out_root)}")
    print("Next: python3 skool_bulk.py render --out <folder> --chunk 0")


def _iter_desc_images(desc):
    """fileIDs of inline images in a module body, for the duplication census."""
    if not desc or not isinstance(desc, str) or not desc.startswith("[v2]"):
        return []
    try:
        nodes = json.loads(desc[4:])
    except (ValueError, TypeError):
        return []
    return [n.get("attrs", {}).get("fileID") for n in nodes
            if n.get("type") == "image" and n.get("attrs", {}).get("fileID")]


def build_chunks(lessons, plans, budget):
    """Chunk 0 = every browser-free lesson (no payload, render immediately).
    Chunks 1..N pack browser-needing lessons up to a fetch-count budget."""
    chunks = [{"id": 0, "mds": [md for md, l in lessons.items() if not l["needs_browser"]],
               "fetches": 0, "posts": [], "fileIds": [], "status": "pending"}]
    cur = None
    for md, l in lessons.items():
        if not l["needs_browser"]:
            continue
        plan = plans[md]
        if cur is None or (cur["fetches"] + plan["cost"] > budget and cur["mds"]):
            cur = {"id": len(chunks), "mds": [], "fetches": 0,
                   "posts": [], "fileIds": [], "status": "pending"}
            chunks.append(cur)
        cur["mds"].append(md)
        cur["fetches"] += plan["cost"]
        cur["posts"].extend({"postId": p["postId"], "groupId": p["groupId"],
                             "attachmentIds": p["attachmentIds"]} for p in plan["posts"])
        cur["fileIds"].extend(plan["fileIds"])
    return chunks


# ---------------------------------------------------------------- next-chunk

def cmd_next_chunk(args):
    man = load_manifest(args.out)
    pending = [c for c in man["chunks"] if c["status"] != "done" and c["id"] != 0]
    if args.chunk is not None:
        pending = [c for c in man["chunks"] if c["id"] == args.chunk]
    if not pending:
        print("No pending browser chunks. Everything is rendered or chunk 0 is all that's left.")
        return
    c = pending[0]
    lessons = man["lessons"]
    print(f"# Chunk {c['id']}: {len(c['mds'])} lessons, ~{c['fetches']} in-page fetches")
    for md in c["mds"]:
        print(f"#   - {lessons[md]['title']}")
    payload = {"posts": c["posts"], "fileIds": c["fileIds"]}
    out = bulk_dir(args.out) / f"chunk_{c['id']}_payload.json"
    ss.long_path(out).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"\n# payload written to {out}")
    print(json.dumps(payload, ensure_ascii=False))


# ---------------------------------------------------------------- render

def load_browser_blob(path):
    """Accepts the raw base64 blob produced by fetch_browser_data.js (what
    window.__browserDataB64 holds), a JSON-quoted string of it, or already-decoded
    JSON. Base64 is the safe transport: the browser tool's file-write path
    markdown-escapes characters like `(` and would silently corrupt raw JSON."""
    raw = Path(path).read_text(encoding="utf-8").strip()
    try:
        val = json.loads(raw)
        if isinstance(val, dict):
            return val
        if isinstance(val, str):
            raw = val.strip()
    except ValueError:
        pass
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as e:
        sys.exit(f"Couldn't read browser data from {path}: {e}")


def cmd_render(args):
    out_root = Path(args.out)
    man = load_manifest(out_root)
    chunk = next((c for c in man["chunks"] if c["id"] == args.chunk), None)
    if chunk is None:
        sys.exit(f"No chunk {args.chunk} in manifest (have 0..{len(man['chunks'])-1}).")

    mds = chunk["mds"]
    if args.only:
        # Match an md id outright, or a case-insensitive substring of the folder
        # name / title -- so `--only "Day 8"` re-does one day without touching
        # the rest of its chunk.
        needles = [n.lower() for n in args.only]
        mds = [md for md in mds
               if any(n == md.lower()
                      or n in man["folder_map"][md].lower()
                      or n in man["lessons"][md]["title"].lower()
                      for n in needles)]
        if not mds:
            sys.exit(f"--only {args.only} matched no lesson in chunk {args.chunk}.")
        print(f"--only: {len(mds)} of {len(chunk['mds'])} lesson(s) in this chunk")

    todo = [md for md in mds
            if args.force or man["lessons"][md]["status"] != "done"]
    skipped = len(mds) - len(todo)
    if skipped:
        print(f"Skipping {skipped} already-extracted lesson(s) — re-run with --force to redo them.")
    if not todo:
        # Nothing left to do, so don't demand a browser payload for work that is
        # already finished -- that would make a plain re-run of a completed
        # chunk look like a failure.
        if not args.only:
            chunk["status"] = "done"
            save_manifest(out_root, man)
        print(f"Chunk {args.chunk}: nothing to do — all {len(mds)} lesson(s) already extracted.")
        return

    browser_data = {}
    if args.browser_data:
        browser_data = load_browser_blob(args.browser_data)
        print(f"browser data: {len(browser_data.get('posts', {}))} posts, "
              f"{len(browser_data.get('signed_urls', {}))} signed URLs")
    elif chunk["fetches"] > 0:
        sys.exit(f"Chunk {args.chunk} needs browser data (~{chunk['fetches']} fetches). "
                 f"Run `next-chunk --chunk {args.chunk}` and pass --browser-data.")

    session = ss.get_session()
    group, course = man["group"], man["course"]
    build_id = man["build_id"]
    pages_dir = bulk_dir(out_root) / "pages"

    ok = fail = 0
    for i, md in enumerate(todo, start=1):
        entry = man["lessons"][md]
        print(f"[{i}/{len(todo)}] {entry['title'][:60]}")
        try:
            cache = pages_dir / f"{md}.json"
            if args.refresh or not cache.exists():
                # Re-pull the lesson from Skool so a re-extract picks up edits and
                # newly posted comments rather than replaying this morning's cache.
                pp, build_id = ss.fetch_lesson_json_retrying(session, build_id, group, course, md)
                ss.long_path(cache).write_text(json.dumps(pp, ensure_ascii=False), encoding="utf-8")
                man["build_id"] = build_id
            else:
                pp = json.loads(ss.long_path(cache).read_text(encoding="utf-8"))
            nav = ss.build_nav((pp or {}).get("course"), md) if pp else None
            stats = ss.extract_lesson(
                session, group, course, md, out_root, build_id=build_id,
                browser_data=browser_data, page_props=pp, nav=nav,
                folder_map=man["folder_map"], index=man, verbose=not args.quiet,
            )
            entry.update(status="done", extracted_at=now_iso(), error=None,
                         stats={k: stats[k] for k in
                                ("module_images", "resources", "posts", "attachments",
                                 "comments", "reused_posts", "post_videos")})
            ok += 1
        except Exception as e:  # one bad lesson must not abort a 107-lesson run
            entry.update(status="error", error=f"{type(e).__name__}: {e}")
            print(f"    ERROR: {type(e).__name__}: {e}")
            fail += 1
        save_manifest(out_root, man)  # after every lesson, so a crash resumes cleanly

    if all(man["lessons"][md]["status"] == "done" for md in chunk["mds"]):
        chunk["status"] = "done"
    save_manifest(out_root, man)
    print(f"\nChunk {args.chunk}: {ok} rendered, {fail} failed, {skipped} skipped.")


# ---------------------------------------------------------------- status

def cmd_status(args):
    man = load_manifest(args.out)
    lessons = man["lessons"]
    by = {}
    for l in lessons.values():
        by[l["status"]] = by.get(l["status"], 0) + 1
    print(f"Course: {man['course_title']!r} ({man['group']}/{man['course']})")
    print(f"Output: {man['out_root']}")
    print(f"Lessons: {len(lessons)} — " + ", ".join(f"{k}: {v}" for k, v in sorted(by.items())))
    tot = {"comments": 0, "attachments": 0, "resources": 0, "module_images": 0, "reused_posts": 0}
    for l in lessons.values():
        for k in tot:
            tot[k] += (l.get("stats") or {}).get(k, 0)
    print("Totals: " + ", ".join(f"{k}={v}" for k, v in tot.items()))
    print(f"Posts indexed: {len(man.get('posts', {}))}")
    pend = [c for c in man["chunks"] if c["status"] != "done"]
    print(f"Chunks: {len(man['chunks'])} total, {len(pend)} pending "
          + (f"(next: {pend[0]['id']})" if pend else "— all done"))
    errs = [(md, l) for md, l in lessons.items() if l["status"] == "error"]
    if errs:
        print(f"\n{len(errs)} error(s):")
        for md, l in errs[:20]:
            print(f"  {l['title'][:50]}: {l['error']}")


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description="Bulk-extract a whole Skool classroom.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enumerate", help="Map the course, cache every lesson, plan the chunks")
    e.add_argument("url")
    e.add_argument("--out", required=True)
    e.add_argument("--budget", type=int, default=DEFAULT_FETCH_BUDGET,
                   help="Max in-page fetches per browser chunk")
    e.add_argument("--delay", type=float, default=0.4, help="Politeness delay between lesson fetches")
    e.add_argument("--refresh", action="store_true", help="Re-fetch lessons already cached")
    e.set_defaults(func=cmd_enumerate)

    n = sub.add_parser("next-chunk", help="Print the fetchBrowserData payload for the next chunk")
    n.add_argument("--out", required=True)
    n.add_argument("--chunk", type=int, default=None)
    n.set_defaults(func=cmd_next_chunk)

    r = sub.add_parser("render", help="Render one chunk's lessons")
    r.add_argument("--out", required=True)
    r.add_argument("--chunk", type=int, required=True)
    r.add_argument("--browser-data")
    r.add_argument("--force", action="store_true", help="Re-render lessons already marked done")
    r.add_argument("--only", nargs="+", help="Limit to lessons matching these md ids / folder or title substrings (e.g. --only \"Day 8\")")
    r.add_argument("--refresh", action="store_true", help="Re-fetch each lesson from Skool instead of using the cached page JSON")
    r.add_argument("--quiet", action="store_true")
    r.set_defaults(func=cmd_render)

    s = sub.add_parser("status", help="Progress report")
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
