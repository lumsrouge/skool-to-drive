// Run via chrome-devtools MCP evaluate_script on an authenticated Skool lesson tab.
// Fetches the pieces that sit behind api2.skool.com's AWS WAF JS challenge --
// comments (both pagination windows), gated-file signed download URLs, and
// post attachment (GIF/image) metadata -- which a plain `requests` session
// cannot solve on its own. Everything else (lesson text/links/video) is
// fetched directly by skool_scrape.py.
//
// A module can have MORE THAN ONE pinned post underneath it (not just one) --
// verified 2026-07-11 the data has to stay grouped per-post, never flattened
// into one shared comments blob, or a multi-post lesson becomes unreadable.
// So `posts` is now an ARRAY, one entry per pinned post: { postId, groupId,
// attachmentIds }. Get these from the module's `pinnedPosts` list (each
// entry's `.post.id` / `.post.groupId` / `.post.metadata.attachments` --
// the latter is a COMMA-SEPARATED STRING of file IDs, split it, not a JSON
// array -- verified live 2026-07-11 on a lesson with 4 screenshot PNGs),
// obtained via a quick pure-Python _next/data fetch first. `fileIds` (gated
// module resources) stays a flat list -- those aren't per-post.
//
// IMPORTANT: base64-encode the result before writing it out via evaluate_script's
// filePath option -- the file-write path markdown-escapes characters like `(` `)`
// (verified live 2026-07-11: turned literal "(" into "\(" in comment text),
// corrupting raw JSON/text written directly. Base64 has no such characters.
//
// Usage:
//   1. navigate_page to the lesson URL
//   2. evaluate_script with this function body (fill in posts[] and fileIds)
//   3. evaluate_script `() => window.__browserDataB64` with filePath set
//   4. in Python: base64.b64decode(...) -> json.loads(...) -> save as browser_data.json
//   5. python3 skool_scrape.py "<url>" --browser-data browser_data.json

async function fetchBrowserData({ posts = [], fileIds = [] }) {
  const signed_urls = {};
  const posts_data = {};

  for (const { postId, groupId, attachmentIds = [] } of posts) {
    if (!postId || !groupId) continue;

    const [pinnedRes, tailRes] = await Promise.all([
      fetch(`https://api2.skool.com/posts/${postId}/comments?group-id=${groupId}&limit=25&pinned=true`, { credentials: 'include' }),
      fetch(`https://api2.skool.com/posts/${postId}/comments?group-id=${groupId}&limit=25&tail=true`, { credentials: 'include' }),
    ]);
    const comments_pinned = pinnedRes.ok ? await pinnedRes.json() : null;
    const comments_tail = tailRes.ok ? await tailRes.json() : null;

    // Post-level attachments (GIFs, screenshot PNGs, etc.) -- any content_type,
    // any count. api2.skool.com/files?ids=<id> only accepts one ID at a time
    // reliably (a comma-joined multi-id query returned "invalid file IDs" live).
    const attachments_info = [];
    for (const aid of attachmentIds) {
      const r = await fetch(`https://api2.skool.com/files?ids=${aid}`, { credentials: 'include' });
      if (r.ok) {
        const data = await r.json();
        const f = (data.files || [])[0];
        if (f && f.metadata) {
          attachments_info.push({ id: aid, file_name: f.metadata.file_name, content_type: f.metadata.content_type, read_url: f.metadata.read_url });
        }
      }
    }

    posts_data[postId] = { comments_pinned, comments_tail, attachments_info };
  }

  for (const fid of fileIds) {
    const r = await fetch(`https://api2.skool.com/files/${fid}/download-url?expire=28800`, { method: 'POST', credentials: 'include' });
    if (r.ok) signed_urls[fid] = await r.text();
  }

  const jsonStr = JSON.stringify({ posts: posts_data, signed_urls });
  const utf8Bytes = new TextEncoder().encode(jsonStr);
  let binary = '';
  for (let i = 0; i < utf8Bytes.length; i++) binary += String.fromCharCode(utf8Bytes[i]);
  window.__browserDataB64 = btoa(binary);
  return {
    len: window.__browserDataB64.length,
    posts_fetched: Object.keys(posts_data).length,
    gated_files: Object.keys(signed_urls).length,
  };
}
