#!/usr/bin/env node
/**
 * merge_content_into_html.js — replaces the CONTENT_IDEAS array block in
 * index.html with the freshly-pulled entries from content_refreshed.json
 * (written by top_content_refresh.py).
 */

const fs = require('fs');
const path = require('path');

const HTML_PATH = path.join(__dirname, 'index.html');
const JSON_PATH = path.join(__dirname, 'content_refreshed.json');
const ARRAY_NAME = 'CONTENT_IDEAS';

function serializeEntry(e) {
  const esc = (s) => JSON.stringify(s == null ? '' : String(s));
  return `{ name:${esc(e.name)}, source:${esc(e.source)}, title:${esc(e.title)}, url:${esc(e.url)}, thumb:${esc(e.thumb)}, views:${e.views}, likes:${e.likes}, comments:${e.comments}, eng:${e.eng}, published:${esc(e.published)} },`;
}

function main() {
  if (!fs.existsSync(JSON_PATH)) {
    console.warn(`⚠ ${JSON_PATH} not found — nothing to merge, leaving index.html untouched.`);
    return;
  }
  const entries = JSON.parse(fs.readFileSync(JSON_PATH, 'utf8'));
  if (!Array.isArray(entries) || entries.length === 0) {
    console.warn('⚠ content_refreshed.json is empty — leaving index.html untouched.');
    return;
  }

  let html = fs.readFileSync(HTML_PATH, 'utf8');
  const startMarker = `const ${ARRAY_NAME} = [`;
  const startIdx = html.indexOf(startMarker);
  if (startIdx === -1) {
    console.warn(`⚠ Could not find "${startMarker}" in index.html — aborting.`);
    return;
  }
  const blockStart = startIdx + startMarker.length;
  const endIdx = html.indexOf('\n];', blockStart);
  if (endIdx === -1) {
    console.warn(`⚠ Could not find closing "];" for ${ARRAY_NAME} — aborting.`);
    return;
  }

  const newBlock = '\n' + entries.map(serializeEntry).join('\n');
  const newHtml = html.slice(0, blockStart) + newBlock + html.slice(endIdx);
  fs.writeFileSync(HTML_PATH, newHtml, 'utf8');
  console.log(`Done. ${ARRAY_NAME} regenerated with ${entries.length} entries.`);
}

main();
