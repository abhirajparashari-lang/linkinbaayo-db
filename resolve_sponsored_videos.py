"""
resolve_sponsored_videos.py — reads pending_sponsored_videos.json (queued via
the admin "Add Sponsored Videos" panel), and for each video:
  1. Pulls its title/description/channel/stats from the YouTube Data API.
  2. Asks the Cloudflare Worker (same one Brand Match uses) to identify which
     brand is being promoted, via Gemini.
  3. Asks the same Worker to classify the video's category (reusing the
     existing Brand Match classify endpoint).
  4. Resolves the creator's channel (subs, uploads playlist, etc.) exactly
     like creator_full_refresh.py does.

Writes everything to sponsored_video_resolutions.json for
merge_sponsored_videos_into_html.js to fold into index.html's SPONSORED
array (either as new promoEvidence on an existing creator, or as a
brand-new Sponsored Influencer entry if the channel isn't tracked yet).

Successfully-resolved videos are removed from pending_sponsored_videos.json;
videos where Gemini couldn't identify a brand are left in the pending file
so they don't silently vanish — you can review why later if it keeps
happening for the same kind of video.

API KEYS: same YOUTUBE_API_KEY_1/2 convention as creator_full_refresh.py.
WORKER_URL: the same public Cloudflare Worker URL baked into index.html —
not a secret (the real Gemini/GitHub keys live server-side on the Worker).
"""

import os
import sys
import json
import re
import ssl
import time
import functools
import urllib.request
import urllib.error
import urllib.parse

print = functools.partial(print, flush=True)

WORKER_URL = "https://tight-cherry-1103.abhiraj-parashari.workers.dev/"

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
        sys.exit(1)
    return keys

API_KEYS = _load_api_keys()
_key_idx = 0

PENDING_FILE = "pending_sponsored_videos.json"
OUTPUT_FILE = "sponsored_video_resolutions.json"

SOURCE_FILES_FOR_LOOKUP = [
    "manual_refreshed.json",
    "sponsored_refreshed_seed.json",
    "scraped_refreshed.json",
    "trends_refreshed.json",
]

_UNVERIFIED_CTX = ssl._create_unverified_context()

def _fetch(url, insecure=False, method="GET", body=None, headers=None):
    ctx = _UNVERIFIED_CTX if insecure else None
    req = urllib.request.Request(url, method=method, headers=headers or {})
    if body is not None:
        req.data = body
    with urllib.request.urlopen(req, timeout=25, context=ctx) as resp:
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
            body_txt = e.read().decode("utf-8", "ignore")
            last_err = f"{e.code}: {body_txt[:200]}"
            if e.code in (403, 400):
                _key_idx = (_key_idx + 1) % len(API_KEYS)
                continue
            raise
        except Exception as e:
            last_err = str(e)
            time.sleep(1)
    raise RuntimeError(f"All API keys failed. Last error: {last_err}")

def worker_post(payload):
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; LinkInBaayoBot/1.0)",
    }
    try:
        return _fetch(WORKER_URL, insecure=False, method="POST", body=body, headers=headers)
    except Exception:
        return _fetch(WORKER_URL, insecure=True, method="POST", body=body, headers=headers)
      
def extract_video_id(url):
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", url or "")
    return m.group(1) if m else None

def get_video_details(video_id):
    data = api_get("videos", {"part": "snippet,statistics", "id": video_id})
    items = data.get("items", [])
    if not items:
        return None
    it = items[0]
    sn = it.get("snippet", {})
    st = it.get("statistics", {})
    return {
        "title": sn.get("title", "").strip(),
        "description": sn.get("description", "").strip(),
        "channel_id": sn.get("channelId"),
        "channel_title": sn.get("channelTitle", "").strip(),
        "published": (sn.get("publishedAt") or "").split("T")[0],
        "views": int(st.get("viewCount", 0)),
        "likes": int(st.get("likeCount", 0)),
        "comments": int(st.get("commentCount", 0)),
    }

def get_channel_details(channel_id):
    data = api_get("channels", {"part": "snippet,statistics", "id": channel_id})
    items = data.get("items", [])
    if not items:
        return None
    it = items[0]
    sn = it.get("snippet", {})
    st = it.get("statistics", {})
    handle = sn.get("customUrl", "")  # usually like "@somehandle"
    if handle and not handle.startswith("@"):
        handle = "@" + handle
    return {
        "subs": int(st.get("subscriberCount", 0)),
        "video_count": int(st.get("videoCount", 0)),
        "url": f"https://youtube.com/{handle}" if handle else f"https://youtube.com/channel/{channel_id}",
    }

def load_known_urls():
    """Loads every creator URL already tracked, from all 4 source files, so
    we know whether a video's channel is a brand-new creator or an existing
    one (merge_sponsored_videos_into_html.js does the actual matching against
    index.html itself — this is just used to label the resolution)."""
    known = {}
    for f in SOURCE_FILES_FOR_LOOKUP:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                records = json.load(fh)
        except FileNotFoundError:
            continue
        for rec in records:
            if rec.get("url"):
                known[rec["url"]] = rec.get("name")
    return known

def save(records, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

def main():
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            pending = json.load(f)
    except FileNotFoundError:
        print(f"{PENDING_FILE} not found — nothing queued, nothing to do.")
        save([], OUTPUT_FILE)
        return

    if not pending:
        print("Pending queue is empty — nothing to do.")
        save([], OUTPUT_FILE)
        return

    known_urls = load_known_urls()
    resolutions = []
    still_pending = []

    for i, item in enumerate(pending, 1):
        url = item.get("url", "")
        print(f"[{i}/{len(pending)}] {url} ...", end=" ")
        video_id = extract_video_id(url)
        if not video_id:
            print("⚠ couldn't parse video ID — dropping from queue")
            continue

        try:
            vid = get_video_details(video_id)
        except Exception as e:
            print(f"⚠ YouTube lookup failed: {e} — leaving in queue")
            still_pending.append(item)
            continue

        if not vid:
            print("⚠ video not found/private — dropping from queue")
            continue

        combined_text = f"{vid['title']}\n\n{vid['description'][:1500]}"

        try:
            brand_resp = worker_post({"action": "identifyBrand", "text": combined_text})
        except Exception as e:
            print(f"⚠ brand identification failed: {e} — leaving in queue")
            still_pending.append(item)
            continue

        brand = (brand_resp or {}).get("brand")
        if not brand:
            print("no confident brand identified — leaving in queue for review")
            still_pending.append(item)
            continue

        try:
            cat_resp = worker_post({"text": combined_text})
            weights = (cat_resp or {}).get("weights") or {}
            top_category = max(weights, key=weights.get) if weights else None
        except Exception as e:
            print(f"(category classify failed, continuing without it: {e})", end=" ")
            top_category = None

        try:
            channel = get_channel_details(vid["channel_id"])
        except Exception as e:
            print(f"⚠ channel lookup failed: {e} — leaving in queue")
            still_pending.append(item)
            continue

        if not channel:
            print("⚠ couldn't resolve channel — dropping from queue")
            continue

        is_new_creator = channel["url"] not in known_urls
        views = vid["views"] or 0
        eng = round((vid["likes"] + vid["comments"]) / views * 100, 2) if views else 0.0

        resolutions.append({
            "brand": brand,
            "category": top_category,
            "creator_name": vid["channel_title"],
            "creator_url": channel["url"],
            "creator_subs": channel["subs"],
            "creator_video_count": channel["video_count"],
            "is_new_creator": is_new_creator,
            "video_title": vid["title"],
            "video_url": f"https://youtube.com/watch?v={video_id}",
            "video_views": views,
            "video_eng": eng,
            "video_published": vid["published"],
        })
        print(f"brand={brand} creator={vid['channel_title']} {'(new)' if is_new_creator else '(existing)'}")
        save(resolutions, OUTPUT_FILE)  # save after every video — crash-safe
        time.sleep(0.3)

    save(still_pending, PENDING_FILE)
    print(f"\nDone. {len(resolutions)} resolved, {len(still_pending)} left in queue (unidentified or errored).")

if __name__ == "__main__":
    main()
