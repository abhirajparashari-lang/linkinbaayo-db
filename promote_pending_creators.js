#!/usr/bin/env node
/**
 * promote_pending_creators.js — the second half of the admin "Add My
 * Influencers" auto-pipeline. Run this AFTER creator_full_refresh.py has
 * processed pending_creators.json (which fills in real subs/views/eng for
 * anything it could resolve, leaving unresolvable entries at subs:0).
 *
 * What it does:
 * 1. Reads pending_creators.json.
 * 2. Splits it into "resolved" (subs > 0 — creator_full_refresh.py found a
 *    real channel and pulled real stats) and "unresolved" (subs still 0 —
 *    bad handle, private/deleted channel, or some other lookup failure).
 * 3. Appends every resolved record as a new line inside MANUAL_CREATORS in
 *    index.html (matching the exact object-literal style already used
 *    there, so future merges/edits keep working the same way).
 * 4. Rewrites pending_creators.json to contain ONLY the unresolved leftovers
 *    — so nothing silently vanishes, and the admin can see (via the Add My
 *    Influencers panel, or by opening the file) which handles need a manual
 *    look, while resolved ones are already live.
 *
 * Skips anything whose name already exists in MANUAL_CREATORS (case-
 * insensitive) so re-running this after a partial failure never duplicates
 * an entry.
 *
 * HOW TO RUN (also wired into daily-refresh.yml, right after the refresh
 * script and before the HTML merge step):
 *   node promote_pending_creators.js
 */

const fs = require('fs');
const path = require('path');

const HTML_PATH = path.join(__dirname, 'index.html');
const PENDING_PATH = path.join(__dirname, 'pending_creators.json');

function esc(s) {
  return String(s == null ? '' : s).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

function recordToLine(r) {
  const fields = [
    `name:"${esc(r.name)}"`,
    `url:"${esc(r.url)}"`,
    `subs:${Number(r.subs) || 0}`,
    `niche:"${esc(r.niche || '')}"`,
    `eng:${Number(r.eng) || 0}`,
    `cmt:${Number(r.cmt) || 0}`,
    `views:${Number(r.views) || 0}`,
    `vids:${Number(r.vids) || 0}`,
    `notes:"${esc(r.notes || 'Added via admin panel — resolved by creator_full_refresh.py.')}"`,
    `lastUpload:"${esc(r.lastUpload || '')}"`,
  ];
  return `  { ${fields.join(', ')} },`;
}

function main() {
  if (!fs.existsSync(PENDING_PATH)) {
    console.log('No pending_creators.json found — nothing to promote.');
    return;
  }
  const pending = JSON.parse(fs.readFileSync(PENDING_PATH, 'utf8'));
  if (!pending.length) {
    console.log('pending_creators.json is empty — nothing to promote.');
    return;
  }

  let html = fs.readFileSync(HTML_PATH, 'utf8');
  const startMarker = 'const MANUAL_CREATORS = [';
  const startIdx = html.indexOf(startMarker);
  if (startIdx === -1) {
    console.error('⚠ Could not find "const MANUAL_CREATORS = [" in index.html — aborting, nothing changed.');
    process.exitCode = 1;
    return;
  }
  const blockStart = startIdx + startMarker.length;
  const endIdx = html.indexOf('\n];', blockStart);
  if (endIdx === -1) {
    console.error('⚠ Could not find closing "];" for MANUAL_CREATORS — aborting, nothing changed.');
    process.exitCode = 1;
    return;
  }

  const existingBlock = html.slice(blockStart, endIdx);
  const existingNames = new Set(
    [...existingBlock.matchAll(/name\s*:\s*"([^"]*)"/g)].map(m => m[1].toLowerCase())
  );

  const resolved = [];
  const unresolved = [];
  for (const r of pending) {
    if (Number(r.subs) > 0) {
      if (existingNames.has((r.name || '').toLowerCase())) {
        console.log(`  skip (already in MANUAL_CREATORS): ${r.name}`);
        continue;
      }
      resolved.push(r);
    } else {
      unresolved.push(r);
    }
  }

  if (resolved.length) {
    const newLines = resolved.map(recordToLine).join('\n');
    html = html.slice(0, endIdx) + '\n' + newLines + html.slice(endIdx);
    fs.writeFileSync(HTML_PATH, html, 'utf8');
  }

  fs.writeFileSync(PENDING_PATH, JSON.stringify(unresolved, null, 2), 'utf8');

  console.log(`Promoted ${resolved.length} creator(s) into MANUAL_CREATORS in index.html.`);
  if (resolved.length) console.log('  ' + resolved.map(r => r.name).join(', '));
  console.log(`${unresolved.length} still unresolved, left in pending_creators.json for review.`);
  if (unresolved.length) console.log('  ' + unresolved.map(r => r.name).join(', '));
}

main();
