// merge_rival_ads_into_html.js
//
// Reads rival_ads.json (committed by the Worker's addRivalAd action whenever
// someone saves a sponsored ad link in the admin panel) and merges it into
// index.html:
//   1. Any brand in rival_ads.json that isn't already a key in
//      BRAND_CATEGORY_MAP gets added, using the category captured at save time.
//   2. RIVAL_AD_EVIDENCE is fully rebuilt from rival_ads.json, grouped by
//      lowercased brand name, so Rival Radar always reflects the latest data.
//
// Run from the repo root: node merge_rival_ads_into_html.js
// Exits 0 with no changes if rival_ads.json is missing or empty (safe to
// run on every daily refresh regardless of whether new ads were added).

const fs = require('fs');
const path = require('path');

const REPO_ROOT = process.cwd();
const ADS_PATH = path.join(REPO_ROOT, 'rival_ads.json');
const HTML_PATH = path.join(REPO_ROOT, 'index.html');

function log(msg) {
  console.log(`[merge_rival_ads] ${msg}`);
}

function main() {
  if (!fs.existsSync(ADS_PATH)) {
    log('rival_ads.json not found — nothing to merge, exiting.');
    return;
  }

  let ads;
  try {
    ads = JSON.parse(fs.readFileSync(ADS_PATH, 'utf8'));
  } catch (e) {
    console.error(`[merge_rival_ads] Failed to parse rival_ads.json: ${e.message}`);
    process.exit(1);
  }

  if (!Array.isArray(ads) || ads.length === 0) {
    log('rival_ads.json is empty — nothing to merge, exiting.');
    return;
  }

  // De-dupe by URL (normalized) in case the same link got saved more than
  // once — keep the first occurrence, drop the rest so nothing double-counts.
  function normalizeUrl(u) {
    if (!u) return '';
    return u.trim().toLowerCase().replace(/\/+$/, '');
  }
  const seenUrls = new Set();
  const dedupedAds = [];
  let dupeCount = 0;
  for (const ad of ads) {
    const norm = normalizeUrl(ad.url);
    if (!norm) continue;
    if (seenUrls.has(norm)) {
      dupeCount++;
      continue;
    }
    seenUrls.add(norm);
    dedupedAds.push(ad);
  }
  if (dupeCount) {
    log(`Skipped ${dupeCount} duplicate ad link(s) (same URL already present).`);
  }
  ads = dedupedAds;

  if (ads.length === 0) {
    log('No valid ads after de-dupe — nothing to merge, exiting.');
    return;
  }

  let html = fs.readFileSync(HTML_PATH, 'utf8');

  // --- Step 1: ensure every ad's brand exists in BRAND_CATEGORY_MAP ---
  const mapMatch = html.match(/const BRAND_CATEGORY_MAP = \{[\s\S]*?\n\};/);
  if (!mapMatch) {
    console.error('[merge_rival_ads] Could not find BRAND_CATEGORY_MAP in index.html — aborting.');
    process.exit(1);
  }
  let mapBlock = mapMatch[0];

  const missingBrands = [];
  for (const ad of ads) {
    if (!ad.brand) continue;
    const key = ad.brand.toLowerCase();
    const keyPattern = new RegExp(`["']${key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}["']\\s*:`, 'i');
    if (!keyPattern.test(mapBlock)) {
      missingBrands.push({ key, category: ad.category || 'Uncategorized' });
    }
  }

  if (missingBrands.length) {
    // De-dupe (an ad batch could repeat the same new brand)
    const seen = new Set();
    const uniqueMissing = missingBrands.filter(m => {
      if (seen.has(m.key)) return false;
      seen.add(m.key);
      return true;
    });

    const insertLines = uniqueMissing
      .map(m => `  "${m.key}": "${m.category.replace(/"/g, '\\"')}",`)
      .join('\n');

    // Insert right before the closing `};` of the map
    mapBlock = mapBlock.replace(/\n\};$/, `\n${insertLines}\n};`);
    html = html.replace(mapMatch[0], mapBlock);
    log(`Added ${uniqueMissing.length} new brand(s) to BRAND_CATEGORY_MAP: ${uniqueMissing.map(m => m.key).join(', ')}`);
  } else {
    log('No new brands to add to BRAND_CATEGORY_MAP.');
  }

  // --- Step 2: rebuild RIVAL_AD_EVIDENCE from rival_ads.json ---
  const evidence = {};
  for (const ad of ads) {
    if (!ad.brand || !ad.url) continue;
    const key = ad.brand.toLowerCase();
    if (!evidence[key]) evidence[key] = [];
    evidence[key].push({
      url: ad.url,
      title: ad.title || ad.videoTitle || ''
    });
  }

  const evidenceJson = JSON.stringify(evidence, null, 2);
  const evidenceMatch = html.match(/const RIVAL_AD_EVIDENCE = \{[\s\S]*?\};/);
  if (!evidenceMatch) {
    console.error('[merge_rival_ads] Could not find RIVAL_AD_EVIDENCE in index.html — aborting.');
    process.exit(1);
  }
  html = html.replace(evidenceMatch[0], `const RIVAL_AD_EVIDENCE = ${evidenceJson};`);
  log(`Rebuilt RIVAL_AD_EVIDENCE with ${Object.keys(evidence).length} brand(s), ${ads.length} total ad(s).`);

  fs.writeFileSync(HTML_PATH, html, 'utf8');
  log('index.html updated.');
}

main();
