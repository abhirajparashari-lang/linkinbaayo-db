#!/usr/bin/env node
/**
 * backfill_manual_refreshed.js — ONE-TIME repair script.
 *
 * Problem: promote_pending_creators.js has always written newly-promoted
 * creators straight into index.html's MANUAL_CREATORS block, but never
 * also registered them in manual_refreshed.json. Since creator_full_refresh.py
 * only refreshes stats (subs/views/eng/thumb) for creators already present
 * in its 5 source JSON files, every creator that fell through this gap has
 * been silently frozen at whatever stats it had on the day it was added —
 * no thumbnail, no fresh subs/views/eng, forever.
 *
 * This script does a one-time catch-up: it reads every creator currently
 * baked into index.html's MANUAL_CREATORS array, and for any name not
 * already present in manual_refreshed.json, adds it there (using its
 * current index.html stats as the starting point). After running this once
 * and committing the result, the next creator_full_refresh.py run will
 * finally pick up all of them and start refreshing them properly —
 * including capturing their channel thumbnail.
 *
 * Run once, from the repo root:
 *   node backfill_manual_refreshed.js
 *
 * Then commit the updated manual_refreshed.json. Safe to re-run — it only
 * ever adds names that are missing, never duplicates or overwrites existing
 * entries.
 */

const fs = require('fs');
const path = require('path');

const HTML_PATH = path.join(__dirname, 'index.html');
const MANUAL_REFRESHED_PATH = path.join(__dirname, 'manual_refreshed.json');

function main() {
  const html = fs.readFileSync(HTML_PATH, 'utf8');

  const startMarker = 'const MANUAL_CREATORS = [';
  const startIdx = html.indexOf(startMarker);
  if (startIdx === -1) {
    console.error('⚠ Could not find "const MANUAL_CREATORS = [" in index.html — aborting.');
    process.exitCode = 1;
    return;
  }
  const blockStart = startIdx + startMarker.length;
  const endIdx = html.indexOf('\n];', blockStart);
  if (endIdx === -1) {
    console.error('⚠ Could not find closing "];" for MANUAL_CREATORS — aborting.');
    process.exitCode = 1;
    return;
  }
  const block = html.slice(blockStart, endIdx);

  // Parse each `{ name:"...", url:"...", subs:N, niche:"...", eng:N, cmt:N,
  // views:N, vids:N, notes:"...", lastUpload:"..." }` line into an object.
  // Matches the exact field set recordToLine() in promote_pending_creators.js
  // writes, since that's the schema already used throughout MANUAL_CREATORS.
  const lineRe = /\{\s*name:"((?:[^"\\]|\\.)*)",\s*url:"((?:[^"\\]|\\.)*)",\s*subs:([\d.]+),\s*niche:"((?:[^"\\]|\\.)*)",\s*eng:([\d.]+),\s*cmt:([\d.]+),\s*views:([\d.]+),\s*vids:([\d.]+),\s*notes:"((?:[^"\\]|\\.)*)",\s*lastUpload:"((?:[^"\\]|\\.)*)"\s*\}/g;

  const unesc = (s) => s.replace(/\\"/g, '"').replace(/\\\\/g, '\\');

  const parsed = [];
  let m;
  while ((m = lineRe.exec(block)) !== null) {
    parsed.push({
      name: unesc(m[1]),
      url: unesc(m[2]),
      subs: Number(m[3]),
      niche: unesc(m[4]),
      eng: Number(m[5]),
      cmt: Number(m[6]),
      views: Number(m[7]),
      vids: Number(m[8]),
      notes: unesc(m[9]),
      lastUpload: unesc(m[10]),
    });
  }

  console.log(`Parsed ${parsed.length} creator record(s) out of MANUAL_CREATORS in index.html.`);

  let manualRefreshed = [];
  if (fs.existsSync(MANUAL_REFRESHED_PATH)) {
    manualRefreshed = JSON.parse(fs.readFileSync(MANUAL_REFRESHED_PATH, 'utf8'));
  }
  const alreadyTracked = new Set(manualRefreshed.map(r => (r.name || '').toLowerCase()));

  let added = 0;
  for (const r of parsed) {
    if (!alreadyTracked.has(r.name.toLowerCase())) {
      manualRefreshed.push(r);
      alreadyTracked.add(r.name.toLowerCase());
      added++;
    }
  }

  fs.writeFileSync(MANUAL_REFRESHED_PATH, JSON.stringify(manualRefreshed, null, 2), 'utf8');
  console.log(`Added ${added} previously-orphaned creator(s) to manual_refreshed.json.`);
  console.log(`manual_refreshed.json now has ${manualRefreshed.length} total record(s).`);
  if (parsed.length - added !== manualRefreshed.length - added) {
    // sanity note only, not an error
  }
}

main();
