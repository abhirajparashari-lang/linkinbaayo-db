#!/usr/bin/env node
/**
 * append_sponsored_to_content.js
 *
 * Reads sponsored_video_resolutions.json (brand + category already identified
 * by resolve_sponsored_videos.py) and appends each resolved video to
 * content_refreshed.json with type:"sponsored" so Content Radar can surface
 * them alongside organic top content, filterable by category.
 *
 * Runs AFTER resolve_sponsored_videos.py and BEFORE merge_content_into_html.js
 * in the weekly-discover.yml workflow.
 *
 * Does NOT clear sponsored_video_resolutions.json — that's merge_sponsored_videos_into_html.js's job.
 */

const fs = require('fs');
const path = require('path');

const RESOLUTIONS_PATH = path.join(__dirname, 'sponsored_video_resolutions.json');
const CONTENT_PATH     = path.join(__dirname, 'content_refreshed.json');

function main() {
  if (!fs.existsSync(RESOLUTIONS_PATH)) {
    console.log('No sponsored_video_resolutions.json — nothing to append to content.');
    return;
  }

  const resolutions = JSON.parse(fs.readFileSync(RESOLUTIONS_PATH, 'utf8'));
  if (!resolutions.length) {
    console.log('sponsored_video_resolutions.json is empty — nothing to append.');
    return;
  }

  let content = fs.existsSync(CONTENT_PATH)
    ? JSON.parse(fs.readFileSync(CONTENT_PATH, 'utf8'))
    : [];

  const existingUrls = new Set(content.map(v => v.url));

  let added = 0;
  for (const res of resolutions) {
    if (!res.video_url || existingUrls.has(res.video_url)) continue;
    if (!res.brand || !res.creator_name) continue;

    const eng = typeof res.video_eng === 'number' ? res.video_eng : 0;

    content.push({
      name:      res.creator_name || '',
      source:    'sponsored',
      type:      'sponsored',          // Content Radar can filter on this
      brand:     res.brand || '',
      category:  res.category || '',
      title:     res.video_title || '',
      url:       res.video_url || '',
      thumb:     '',                   // not fetched at this stage
      views:     Number(res.video_views) || 0,
      likes:     0,
      comments:  0,
      eng:       parseFloat(eng.toFixed(2)),
      published: res.video_published || '',
    });

    existingUrls.add(res.video_url);
    added++;
    console.log(`  added [${res.brand}] ${(res.video_title || '').slice(0, 65)}`);
  }

  if (added) {
    fs.writeFileSync(CONTENT_PATH, JSON.stringify(content, null, 2), 'utf8');
    console.log(`\nDone. ${added} sponsored video(s) appended to content_refreshed.json.`);
  } else {
    console.log('No new entries to add (all already present in content_refreshed.json).');
  }
}

main();
