#!/usr/bin/env node
/**
 * merge_refresh_into_html.js — merges manual_refreshed.json,
 * sponsored_refreshed.json, scraped_refreshed.json, trends_refreshed.json
 * into index.html's MANUAL_CREATORS / SPONSORED / SCRAPED / TRENDS arrays.
 *
 * This is a targeted, line-level merge, not a full re-serialization: for
 * each creator, it finds their existing single-line object literal inside
 * the right array block (matched by name), and only overwrites the
 * subs/views/eng/cmt/vids/lastUpload fields on that line via regex
 * substitution. Everything else on the line (url, niche/category, notes,
 * brand associations, promoEvidence, formatting, key order) is left
 * byte-for-byte untouched. That keeps every git diff small and readable —
 * you can actually see what changed each day, not a full-file rewrite.
 *
 * Records present in the refreshed JSON but not found in index.html (e.g.
 * a creator added by hand since the last export) are skipped with a
 * warning, not silently dropped or duplicated — this script only updates
 * existing entries, it doesn't add new ones.
 *
 * HOW TO RUN:
 *   node merge_refresh_into_html.js
 * (expects index.html and all 4 *_refreshed.json files in the same folder;
 * used both locally and by the GitHub Actions daily workflow)
 */

const fs = require('fs');
const path = require('path');

const HTML_PATH = path.join(__dirname, 'index.html');

const ARRAY_FILES = [
  { arrayName: 'MANUAL_CREATORS', jsonFile: 'manual_refreshed.json' },
  { arrayName: 'SPONSORED', jsonFile: 'sponsored_refreshed.json' },
  { arrayName: 'SCRAPED', jsonFile: 'scraped_refreshed.json' },
  { arrayName: 'TRENDS', jsonFile: 'trends_refreshed.json' },
];

// The fields we're allowed to touch on each line. Everything else on the
// object literal is preserved exactly as-is.
const REFRESH_FIELDS = ['subs', 'views', 'eng', 'cmt', 'vids', 'lastUpload'];

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// Replaces a single `key:value` occurrence within one object-literal line.
// Handles both quoted string values ("...") and bare numeric values.
function setFieldOnLine(line, key, value) {
  const isString = typeof value === 'string';
  const serialized = isString ? JSON.stringify(value) : String(value);

  // Matches `key:"..."` or `key:123.45` — key optionally preceded by
  // `{ ` or `, ` (start-of-object or mid-object), not inside another word.
  const pattern = new RegExp(`(\\b${escapeRegExp(key)}\\s*:\\s*)(?:"[^"]*"|-?\\d+(?:\\.\\d+)?)`);

  if (!pattern.test(line)) {
    // Field doesn't exist on this record yet (e.g. an old entry with no
    // `vids` field) — insert it right after `name:"..."` instead of
    // skipping it, so refreshed data isn't silently lost.
    return line.replace(/(name\s*:\s*"[^"]*")/, `$1, ${key}:${serialized}`);
  }
  return line.replace(pattern, `$1${serialized}`);
}

function mergeArrayBlock(html, arrayName, records) {
  const startMarker = `const ${arrayName} = [`;
  const startIdx = html.indexOf(startMarker);
  if (startIdx === -1) {
    console.warn(`⚠ Could not find "${startMarker}" in index.html — skipping ${arrayName}`);
    return { html, updated: 0, notFound: [] };
  }
  const blockStart = startIdx + startMarker.length;
  const endIdx = html.indexOf('\n];', blockStart);
  if (endIdx === -1) {
    console.warn(`⚠ Could not find closing "];" for ${arrayName} — skipping`);
    return { html, updated: 0, notFound: [] };
  }

  let block = html.slice(blockStart, endIdx);
  const byName = new Map(records.map(r => [r.name, r]));
  const seen = new Set();

  const lines = block.split('\n');
  const newLines = lines.map(line => {
    const m = line.match(/name\s*:\s*"([^"]*)"/);
    if (!m) return line; // blank line, comment, etc.
    const name = m[1];
    const fresh = byName.get(name);
    if (!fresh) return line; // creator not in this refresh batch — leave untouched
    seen.add(name);

    let updatedLine = line;
    for (const field of REFRESH_FIELDS) {
      if (fresh[field] === undefined || fresh[field] === null) continue;
      updatedLine = setFieldOnLine(updatedLine, field, fresh[field]);
    }
    return updatedLine;
  });

  const notFound = records.filter(r => !seen.has(r.name)).map(r => r.name);
  const newBlock = newLines.join('\n');
  const newHtml = html.slice(0, blockStart) + newBlock + html.slice(endIdx);
  return { html: newHtml, updated: seen.size, notFound };
}

function main() {
  let html = fs.readFileSync(HTML_PATH, 'utf8');
  let totalUpdated = 0;

  for (const { arrayName, jsonFile } of ARRAY_FILES) {
    const jsonPath = path.join(__dirname, jsonFile);
    if (!fs.existsSync(jsonPath)) {
      console.warn(`⚠ ${jsonFile} not found — skipping ${arrayName}`);
      continue;
    }
    const records = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
    const result = mergeArrayBlock(html, arrayName, records);
    html = result.html;
    totalUpdated += result.updated;
    console.log(`${arrayName}: updated ${result.updated}/${records.length} creators`);
    if (result.notFound.length) {
      console.log(`  (${result.notFound.length} in ${jsonFile} not found in index.html, left as-is: ${result.notFound.slice(0, 5).join(', ')}${result.notFound.length > 5 ? ', ...' : ''})`);
    }
  }

  fs.writeFileSync(HTML_PATH, html, 'utf8');
  console.log(`\nDone. ${totalUpdated} creator records updated in index.html.`);
}

main();
