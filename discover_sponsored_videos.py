"""
discover_sponsored_videos.py

Weekly scan: checks ALL tracked creators' recent uploads (last 8 days only)
for sponsored content using keyword matching on title + description.
Zero Gemini calls — brand identification runs separately in resolve_sponsored_videos.py.

Quota usage (per weekly run for ~1400 creators):
  - Channel IDs extracted directly from /channel/UCxxx URLs  →  0 API calls
  - 1 playlistItems call per creator (IDs + dates only)      →  ~1,400 units
  - 1 videos batch call per 50 new video IDs                 →  ~56 units (avg 2 new/creator)
  ─────────────────────────────────────────────────────────────────────────────
  Total: ~1,460 YouTube API units  (out of 10,000 daily limit)
  Gemini: 0 calls here. resolve_sponsored_videos.py fires Gemini only on flagged videos.

Run from repo root (same folder as manual_refreshed.json etc.):
  YOUTUBE_API_KEY_1=<key> python3 discover_sponsored_videos.py
"""

import os, sys, json, ssl, re, time, datetime, functools
import urllib.request, urllib.parse, urllib.error

print = functools.partial(print, flush=True)

# ── config ────────────────────────────────────────────────────────────────────
LOOKBACK_DAYS  = 8    # videos published in last 8 days (weekly + 1-day buffer)
BATCH_SIZE     = 50   # YouTube API max video IDs per batch call

PENDING_FILE     = "pending_sponsored_videos.json"
RESOLUTIONS_FILE = "sponsored_video_resolutions.json"
CACHE_FILE       = "discovered_videos_cache.json"   # seen video IDs across runs

SOURCE_FILES = [
    "manual_refreshed.json",
    "sponsored_refreshed_seed.json",
    "scraped_refreshed.json",
    "trends_refreshed.json",
]

# Keyword signals — checked in title + description (case-insensitive)
SPONSORED_KEYWORDS = [
    "includes paid promotion",
    "#ad",
    "#sponsored",
    "#gifted",
    "#collab",
    "#paidpartnership",
    "paid partnership",
    "sponsored by",
    "gifted by",
    "this video is sponsored",
]
# ─────────────────────────────────────────────────────────────────────────────

def _load_api_keys():
    keys = []
    i = 1
    while True:
        k = os.environ.get(f"YOUTUBE_API_KEY_{i}")
        if not k:
            break
        keys.append(k)
        i += 1
    if not keys:
        print("ERROR: no YOUTUBE_API_KEY_* env vars found.")
        sys.exit(1)
    return keys

API_KEYS = _load_api_keys()
_key_idx = 0
_CTX = ssl._create_unverified_context()

def yt(path, **params):
    global _key_idx
    last_err = None
    for _ in range(len(API_KEYS)):
        key = API_KEYS[_key_idx]
        params["key"] = key
        qs = urllib.parse.urlencode(params)
        url = f"https://www.googleapis.com/youtube/v3/{path}?{qs}"
        try:
            try:
                with urllib.request.urlopen(url, timeout=15) as r:
                    return json.loads(r.read())
            except Exception:
                with urllib.request.urlopen(url, timeout=15, context=_CTX) as r:
                    return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            last_err = f"HTTP {e.code}: {body[:200]}"
            if e.code in (403, 429):
                _key_idx = (_key_idx + 1) % len(API_KEYS)
                continue
            raise
        except Exception as e:
            last_err = str(e)
            time.sleep(1)
    raise RuntimeError(f"All API keys failed. Last error: {last_err}")

def save(data, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def channel_id_from_url(url):
    """Extract or resolve YouTube channel ID. Zero API call for /channel/UCxxx URLs."""
    url = (url or "").rstrip("/")
    # /channel/UCxxx — free, no API call
    m = re.search(r"/channel/(UC[\w-]+)", url)
    if m:
        return m.group(1)
    # /@handle
    m = re.search(r"/@([\w.%-]+)", url)
    if m:
        try:
            data = yt("channels", part="id", forHandle=m.group(1))
            items = data.get("items", [])
            return items[0]["id"] if items else None
        except Exception:
            return None
    # /c/name or /user/name
    m = re.search(r"/(?:c|user)/([\w.%-]+)", url)
    if m:
        slug = m.group(1)
        try:
            data = yt("channels", part="id", forUsername=slug)
            items = data.get("items", [])
            if items:
                return items[0]["id"]
            data = yt("channels", part="id", forHandle=slug)
            items = data.get("items", [])
            return items[0]["id"] if items else None
        except Exception:
            return None
    return None

def uploads_playlist_id(channel_id):
    try:
        data = yt("channels", part="contentDetails", id=channel_id)
        items = data.get("items", [])
        if not items:
            return None
        return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except Exception:
        return None

def recent_video_ids_since(playlist_id, cutoff_str):
    """Fetch video IDs from uploads playlist published on or after cutoff_str.
    Stops as soon as it hits a video older than the cutoff — uploads are newest-first."""
    video_ids = []
    page_token = None
    while True:
        params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 10}
        if page_token:
            params["pageToken"] = page_token
        try:
            data = yt("playlistItems", **params)
        except Exception:
            break
        items = data.get("items", [])
        if not items:
            break
        stop = False
        for item in items:
            cd = item.get("contentDetails", {})
            vid_id = cd.get("videoId")
            published = (cd.get("videoPublishedAt") or "")[:10]
            if not vid_id:
                continue
            if published < cutoff_str:
                stop = True
                break
            video_ids.append(vid_id)
        if stop or not data.get("nextPageToken"):
            break
        page_token = data["nextPageToken"]
    return video_ids

def batch_fetch_snippets(video_ids):
    """Batch-fetch title + description for video IDs. 50 per API call."""
    results = {}
    for i in range(0, len(video_ids), BATCH_SIZE):
        batch = video_ids[i:i + BATCH_SIZE]
        try:
            data = yt("videos", part="snippet", id=",".join(batch))
            for item in data.get("items", []):
                results[item["id"]] = item.get("snippet", {})
        except Exception as e:
            print(f"  ⚠ batch snippet fetch failed: {e}")
        time.sleep(0.1)
    return results

def is_sponsored(snippet):
    text = (snippet.get("title", "") + " " + snippet.get("description", "")).lower()
    for kw in SPONSORED_KEYWORDS:
        if kw.lower() in text:
            return True
    return False

def main():
    since = datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)
    cutoff_str = since.isoformat()
    today_str  = datetime.date.today().isoformat()
    print(f"Scanning videos published since {cutoff_str}...\n")

    # Already-processed: skip these to avoid double-queueing
    cache        = set(load_json(CACHE_FILE, []))
    pending      = load_json(PENDING_FILE, [])
    resolutions  = load_json(RESOLUTIONS_FILE, [])
    skip_urls    = {item["url"] for item in pending} | \
                   {item.get("video_url", "") for item in resolutions}

    # Load all creators across source files, deduplicate by URL
    creators = []
    seen_urls = set()
    for fname in SOURCE_FILES:
        for rec in load_json(fname, []):
            url = rec.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                creators.append({"name": rec.get("name", "?"), "url": url})

    print(f"Loaded {len(creators)} unique creators from source files.\n")

    new_pending   = []
    new_cache_ids = set()
    total_checked = 0
    creators_active = 0

    for i, creator in enumerate(creators, 1):
        ch_id = channel_id_from_url(creator["url"])
        if not ch_id:
            continue

        pl_id = uploads_playlist_id(ch_id)
        if not pl_id:
            continue

        recent_ids = recent_video_ids_since(pl_id, cutoff_str)
        if not recent_ids:
            continue

        # Only fetch snippets for videos we haven't seen before
        new_ids = [v for v in recent_ids if v not in cache and v not in new_cache_ids]
        if not new_ids:
            continue

        creators_active += 1
        snippets = batch_fetch_snippets(new_ids)
        total_checked += len(snippets)
        new_cache_ids.update(new_ids)  # mark all as seen whether or not sponsored

        for vid_id, snippet in snippets.items():
            video_url = f"https://youtube.com/watch?v={vid_id}"
            if video_url in skip_urls:
                continue
            if is_sponsored(snippet):
                title = snippet.get("title", "")[:70]
                print(f"  ✅ {creator['name']}: {title}")
                new_pending.append({"url": video_url, "addedDate": today_str})
                skip_urls.add(video_url)

        if i % 200 == 0:
            print(f"  [{i}/{len(creators)}] checked so far...")

        time.sleep(0.15)

    # Persist
    if new_pending:
        pending.extend(new_pending)
        save(pending, PENDING_FILE)

    save(list(cache | new_cache_ids), CACHE_FILE)

    print(f"\n── Summary ──────────────────────────────────────")
    print(f"  Creators scanned:            {len(creators)}")
    print(f"  Creators with new uploads:   {creators_active}")
    print(f"  Videos checked:              {total_checked}")
    print(f"  New sponsored videos queued: {len(new_pending)}")
    if new_pending:
        print(f"  → resolve_sponsored_videos.py will identify brands next.")

if __name__ == "__main__":
    main()
