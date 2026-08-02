#!/usr/bin/env node
/**
 * merge_sponsored_videos_into_html.js — the last step of the "Add Sponsored
 * Video" auto-pipeline. Run this AFTER resolve_sponsored_videos.py has
 * turned pending_sponsored_videos.json into sponsored_video_resolutions.json
 * (each entry already has a Gemini-identified brand + real creator stats).
 *
 * What it does, per resolution:
 *   - If the creator's URL already exists in the SPONSORED array: appends a
 *     new promoEvidence entry to that record (and adds the brand to its
 *     `brands` field if not already listed), leaving everything else on
 *     that record untouched — same "targeted line edit, not full rewrite"
 *     philosophy as merge_refresh_into_html.js.
 *   - If the creator's URL doesn't exist in SPONSORED yet (is_new_creator,
 *     or just never tracked as a Sponsored Influencer before): appends a
 *     brand-new single-line record to the end of the SPONSORED array,
 *     matching its existing object-literal style.
 *   - Never touches MANUAL_CREATORS/SCRAPED/TRENDS — a creator can be
 *     tracked as a manual influencer AND have sponsored-video proof
 *     evidence; those are separate, both stay intact.
 *
 * After merging, sponsored_video_resolutions.json is cleared (each entry
 * is now live in index.html, so nothing should be re-merged next run).
 *
 * HOW TO RUN (wired into daily-refresh.yml, after resolve_sponsored_videos.py
 * and before the existing merge_refresh_into_html.js / merge_content_into_html.js
 * steps):
 *   node merge_sponsored_videos_into_html.js
 */

const fs = require('fs');
const path = require('path');

const HTML_PATH = path.join(__dirname, 'index.html');
const RESOLUTIONS_PATH = path.join(__dirname, 'sponsored_video_resolutions.json');

function esc(s) {
  return String(s == null ? '' : s).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

function findArrayBlock(html, arrayName) {
  const startMarker = `const ${arrayName} = [`;
  const startIdx = html.indexOf(startMarker);
  if (startIdx === -1) return null;
  const blockStart = startIdx + startMarker.length;
  const endIdx = html.indexOf('\n];', blockStart);
  if (endIdx === -1) return null;
  return { blockStart, endIdx };
}

// Builds one new promoEvidence object literal from a resolution record.
function promoEvidenceLiteral(res) {
  const eng = typeof res.video_eng === 'number' ? `"${res.video_eng.toFixed(2)}%"` : `"${esc(res.video_eng)}"`;
  return `{brand:"${esc(res.brand)}", videoTitle:"${esc(res.video_title)}", videoUrl:"${esc(res.video_url)}", views:${Number(res.video_views) || 0}, eng:${eng}, published:"${esc(res.video_published)}", paid:"Sponsored", code:null}`;
}

// Brand-new SPONSORED record for a creator that isn't tracked yet.
function newSponsoredLine(res) {
  const fields = [
    `name:"${esc(res.creator_name)}"`,
    `subs:${Number(res.creator_subs) || 0}`,
    `category:"${esc(res.category || '')}"`,
    `brands:"${esc(res.brand)}"`,
    `eng:${typeof res.video_eng === 'number' ? res.video_eng : 0}`,
    `views:${Number(res.video_views) || 0}`,
    `cmt:0`,
    `vids:${Number(res.creator_video_count) || 0}`,
    `paid:"Sponsored"`,
    `promoEvidence:[${promoEvidenceLiteral(res)}]`,
    `lastUpload:"${esc(res.video_published || '')}"`,
    `notes:"Added via Add Sponsored Video panel — brand identified by Gemini, resolved by resolve_sponsored_videos.py."`,
  ];
  return `  { ${fields.join(', ')} },`;
}

// Finds the SPONSORED line for a given creator URL. SPONSORED records don't
// always carry a bare `url` field the same way MANUAL_CREATORS does (some
// derive it from name via sponsoredUrl() in the frontend) — so we match on
// `name` (case-insensitive) as the reliable key, same as promote_pending_creators.js
// does for MANUAL_CREATORS.
function findLineIndexByName(lines, name) {
  const target = name.toLowerCase();
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(/name\s*:\s*"([^"]*)"/);
    if (m && m[1].toLowerCase() === target) return i;
  }
  return -1;
}

function addPromoEvidenceToLine(line, res) {
  const newEntry = promoEvidenceLiteral(res);
  const brandAlreadyOnLine = new RegExp(`brand\\s*:\\s*"${res.brand.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}"`).test(line);

  let updated = line;

  // Append the new evidence entry into the existing promoEvidence:[...] array.
  if (/promoEvidence\s*:\s*\[/.test(updated)) {
    updated = updated.replace(/promoEvidence\s*:\s*\[/, (m) => m); // no-op, just confirms match exists
    updated = updated.replace(/(promoEvidence\s*:\s*\[)(.*?)(\])/, (full, open, inner, close) => {
      const sep = inner.trim().length ? ', ' : '';
      return `${open}${inner}${sep}${newEntry}${close}`;
    });
  } else {
    // No promoEvidence field yet on this record — insert one right after name:"...".
    updated = updated.replace(/(name\s*:\s*"[^"]*")/, `$1, promoEvidence:[${newEntry}]`);
  }

  // Add the brand to the `brands` field if it's a comma-separated list that
  // doesn't already mention it.
  if (!brandAlreadyOnLine) {
    const brandsMatch = updated.match(/brands\s*:\s*"([^"]*)"/);
    if (brandsMatch) {
      const current = brandsMatch[1];
      if (!current.toLowerCase().includes(res.brand.toLowerCase())) {
        const merged = current.trim() ? `${current}, ${res.brand}` : res.brand;
        updated = updated.replace(/(brands\s*:\s*")[^"]*(")/, `$1${esc(merged)}$2`);
      }
    } else {
      updated = updated.replace(/(name\s*:\s*"[^"]*")/, `$1, brands:"${esc(res.brand)}"`);
    }
  }

  return updated;
}

function main() {
  if (!fs.existsSync(RESOLUTIONS_PATH)) {
    console.log('No sponsored_video_resolutions.json found — nothing to merge.');
    return;
  }
  const resolutions = JSON.parse(fs.readFileSync(RESOLUTIONS_PATH, 'utf8'));
  if (!resolutions.length) {
    console.log('sponsored_video_resolutions.json is empty — nothing to merge.');
    return;
  }

  let html = fs.readFileSync(HTML_PATH, 'utf8');
  const block = findArrayBlock(html, 'SPONSORED');
  if (!block) {
    console.error('⚠ Could not find "const SPONSORED = [" / closing "];" in index.html — aborting, nothing changed.');
    process.exitCode = 1;
    return;
  }

  let { blockStart, endIdx } = block;
  let body = html.slice(blockStart, endIdx);
  let lines = body.split('\n');

  let updatedCount = 0;
  let addedCount = 0;
  const newRecordLines = [];

  for (const res of resolutions) {
    if (!res.brand || !res.creator_name) {
      console.log(`  skip (missing brand or creator name): ${res.video_url}`);
      continue;
    }
    const idx = findLineIndexByName(lines, res.creator_name);
    if (idx !== -1) {
      lines[idx] = addPromoEvidenceToLine(lines[idx], res);
      updatedCount++;
      console.log(`  updated (existing Sponsored Influencer): ${res.creator_name} — added "${res.brand}" evidence`);
    } else {
      newRecordLines.push(newSponsoredLine(res));
      addedCount++;
      console.log(`  new Sponsored Influencer: ${res.creator_name} — "${res.brand}"`);
    }
  }

  body = lines.join('\n');
  if (newRecordLines.length) {
    body = body + '\n' + newRecordLines.join('\n');
  }

  html = html.slice(0, blockStart) + body + html.slice(endIdx);
  fs.writeFileSync(HTML_PATH, html, 'utf8');

  // Every resolution in this batch is now live — clear the file so nothing
  // gets double-merged on the next run.
  fs.writeFileSync(RESOLUTIONS_PATH, '[]', 'utf8');

  console.log(`\nDone. ${updatedCount} existing Sponsored Influencer(s) updated, ${addedCount} new one(s) added.`);
}

main();
