"""
top_content_refresh.py — pulls each creator's TOP 1-3 best-performing recent
videos (by view count) via the YouTube Data API v3, and writes them out as
content_refreshed.json in the exact schema Content Radar expects (see
CONTENT_IDEAS in index.html).

VIEW FLOOR IS DYNAMIC: reads config.json (the same file the site's admin
"Quality Floor" panel writes to) for the view-count cutoffs, instead of a
hardcoded number. One place to change the benchmark, both the live site's
filtering and this script pick it up automatically. If config.json doesn't
exist yet, sensible defaults are used (5,000 views for videos published in
the last 3 days, 10,000 views for anything older).
"""

import os
import sys
import json
import re
import ssl
import time
import datetime
import functools
import urllib.request
import urllib.error
import urllib.parse

print = functools.partial(print, flush=True)

def _load_api_keys():
    keys = []
    i = 1
    while True:
        key = os.environ.get(f"YOUTUBE_API_KEY_{i}")
        if not key:
            break
        keys.append(key)
        i += 1
    if not keys:
        print("ERROR: no YouTube API keys found in the environment.")
        print("Set at least YOUTUBE_API_KEY_1 before running this script.")
        sys.exit(1)
    return keys

API_KEYS = _load_api_keys()
_key_idx = 0

SAMPLE_SIZE = 50
TOP_N = 3
CONTENT_RECENT_DAYS = 3

CONFIG_FILE = "config.json"

def load_view_floor():
    defaults = {"contentRecent": 5000, "contentOlder": 10000}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return {
            "contentRecent": cfg.get("contentRecent", defaults["contentRecent"]),
            "contentOlder": cfg.get("contentOlder", defaults["contentOlder"]),
        }
    except (FileNotFoundError, json.JSONDecodeError):
        return defaults

VIEW_FLOOR = load_view_floor()

def floor_for_video(published):
    if not published:
        return VIEW_FLOOR["contentOlder"]
    try:
        age_days = (datetime.date.today() - datetime.date.fromisoformat(published)).days
    except ValueError:
        return VIEW_FLOOR["contentOlder"]
    return VIEW_FLOOR["contentRecent"] if age_days <= CONTENT_RECENT_DAYS else VIEW_FLOOR["contentOlder"]

SOURCE_FILES = [
    "manual_refreshed.json",
    "sponsored_refreshed_seed.json",
    "scraped_refreshed.json",
    "trends_refreshed.json",
]
SOURCE_LABEL_BY_FILE = {
    "manual_refreshed.json": "manual",
    "sponsored_refreshed_seed.json": "sponsored",
    "scraped_refreshed.json": "scraped",
    "trends_refreshed.json": "trends",
}

OUTPUT_FILE = "content_refreshed.json"

_UNVERIFIED_CTX = ssl._create_unverified_context()

def _fetch(url, insecure=False):
    ctx = _UNVERIFIED_CTX if insecure else None
    with urllib.request.urlopen(url, timeout=20, context=ctx) as resp:
        return json.loads(resp.read())

def api_get(path, params):
    global _key_idx
    last_err = None
    for _ in range(len(API_KEYS)):
        key = API_KEYS[_key_idx]
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url = f"https://www.googleapis.com/youtube/v3/{path}?{qs}&key={key}"
        try:
            try:
                return _fetch(url, insecure=False)
            except Exception:
                return _fetch(url, insecure=True)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            last_err = f"{e.code}: {body[:200]}"
            if e.code in (403, 400):
                _key_idx = (_key_idx + 1) % len(API_KEYS)
                continue
            raise
        except Exception as e:
            last_err = str(e)
            time.sleep(1)
    raise RuntimeError(f"All API keys failed. Last error: {last_err}")

def extract_channel_ref(url):
    if not url:
        return None
    m = re.search(r"youtube\.com/channel/([\w-]+)", url)
    if m:
        return ("id", m.group(1))
    m = re.search(r"youtube\.com/(@[\w.-]+)", url)
    if m:
        return ("handle", m.group(1))
    m = re.search(r"youtube\.com/(?:c/|user/)([\w-]+)", url)
    if m:
        return ("user", m.group(1))
    return None

def resolve_channel(url):
    ref = extract_channel_ref(url)
    if not ref:
        return None
    kind, value = ref
    params = {"part": "contentDetails", "maxResults": 1}
    if kind == "id":
        params["id"] = value
    elif kind == "handle":
        params["forHandle"] = value
    elif kind == "user":
        params["forUsername"] = value
    data = api_get("channels", params)
    items = data.get("items", [])
    if not items:
        return None
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

def recent_video_ids(uploads_playlist, limit):
    data = api_get("playlistItems", {
        "part": "contentDetails", "playlistId": uploads_playlist, "maxResults": limit,
    })
    return [it["contentDetails"]["videoId"] for it in data.get("items", [])]

def video_details(video_ids):
    if not video_ids:
        return []
    data = api_get("videos", {
        "part": "statistics,snippet", "id": ",".join(video_ids),
    })
    out = []
    for it in data.get("items", []):
        st = it.get("statistics", {})
        sn = it.get("snippet", {})
        views = int(st.get("viewCount", 0))
        likes = int(st.get("likeCount", 0))
        comments = int(st.get("commentCount", 0))
        published = sn.get("publishedAt", "")
        out.append({
            "video_id": it["id"],
            "title": sn.get("title", "").strip(),
            "views": views,
            "likes": likes,
            "comments": comments,
            "published": published.split("T")[0] if published else None,
        })
    return out

def top_videos_for_creator(url):
    uploads_playlist = resolve_channel(url)
    if not uploads_playlist:
        return []
    vid_ids = recent_video_ids(uploads_playlist, SAMPLE_SIZE)
    if not vid_ids:
        return []
    details = video_details(vid_ids)
    details = [d for d in details if d["views"] >= floor_for_video(d["published"])]
    details.sort(key=lambda d: d["views"], reverse=True)
    return details[:TOP_N]

def save(records, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

def main():
    print(f"Using view floor from {CONFIG_FILE if os.path.exists(CONFIG_FILE) else 'defaults'}: "
          f"recent(<= {CONTENT_RECENT_DAYS}d)={VIEW_FLOOR['contentRecent']}, older={VIEW_FLOOR['contentOlder']}")

    all_entries = []
    seen_urls = set()

    for source_file in SOURCE_FILES:
        source_label = SOURCE_LABEL_BY_FILE[source_file]
        print(f"\n=== {source_file} (source: {source_label}) ===")
        try:
            with open(source_file, "r", encoding="utf-8") as f:
                records = json.load(f)
        except FileNotFoundError:
            print(f"  ⚠ {source_file} not found — skipped")
            continue

        for i, rec in enumerate(records, 1):
            name = rec.get("name", "?")
            url = rec.get("url")
            print(f"[{i}/{len(records)}] {name} ...", end=" ")
            if not url:
                print("no url on file — skipped")
                continue
            try:
                top = top_videos_for_creator(url)
            except Exception as e:
                print(f"⚠ error: {e}")
                continue
            if not top:
                print("no qualifying videos")
                continue

            added = 0
            for v in top:
                video_url = f"https://youtube.com/watch?v={v['video_id']}"
                if video_url in seen_urls:
                    continue
                seen_urls.add(video_url)
                views = v["views"] or 0
                eng = round((v["likes"] + v["comments"]) / views * 100, 3) if views else 0.0
                all_entries.append({
                    "name": name,
                    "source": source_label,
                    "title": v["title"],
                    "url": video_url,
                    "thumb": f"https://i.ytimg.com/vi/{v['video_id']}/mqdefault.jpg",
                    "views": views,
                    "likes": v["likes"],
                    "comments": v["comments"],
                    "eng": eng,
                    "published": v["published"],
                })
                added += 1
            print(f"kept {added} video(s), top view count {top[0]['views']}")
            save(all_entries, OUTPUT_FILE)
            time.sleep(0.3)

    print(f"\nAll done. {len(all_entries)} content entries written to {OUTPUT_FILE}.")

if __name__ == "__main__":
    main()
