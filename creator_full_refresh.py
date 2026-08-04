"""
creator_full_refresh.py — pulls fresh subs/views/engagement/comment-rate for
every creator already in the database (manual_refreshed.json,
sponsored_refreshed_seed.json, scraped_refreshed.json, trends_refreshed.json,
pending_creators.json), via the YouTube Data API v3. Run this daily
(daily-refresh.yml already calls it) so the quality floor and everything else
in Brand Match is filtering on current numbers, not stale ones.

WHAT IT UPDATES per creator: subs, views (avg across recent videos), eng
(%), cmt (%), vids (channel's total video count), lastUpload (most recent
upload date). Everything else on each record (name, url, category/niche,
brand associations, promoEvidence, etc.) is left untouched.

pending_creators.json is a special case: those records start with
subs:0/views:0 placeholders (added via the admin panel's "Add My
Influencers" flow, committed by the Cloudflare Worker). This script resolves
real stats for them exactly like everything else — promote_pending_creators.js
(run right after this script in the GitHub Actions workflow) then looks at
which ones ended up with subs > 0 (successfully resolved) and moves those
into MANUAL_CREATORS in index.html, leaving only genuinely-unresolvable
handles (bad @handle, private/deleted channel, etc.) behind in the pending
file for manual review.

HOW ENGAGEMENT/COMMENT % ARE COMPUTED: over each creator's most recent
NUM_RECENT_VIDEOS videos —
    eng (%) = avg(likes + comments) / avg(views) * 100
    cmt (%) = avg(comments) / avg(views) * 100
Same definition used throughout this project's other scripts.

API KEYS — READ FROM THE ENVIRONMENT, NOT HARDCODED: this script used to
have YouTube Data API keys hardcoded directly in the source. That's a real
problem now that this file lives in a public GitHub repo and runs
automatically — anyone browsing the repo could grab the keys and burn your
quota. Set them as environment variables instead:
    YOUTUBE_API_KEY_1=...
    YOUTUBE_API_KEY_2=...   (optional — add more as YOUTUBE_API_KEY_3, etc.)
Locally: export them in your shell before running, or prefix the command:
    YOUTUBE_API_KEY_1=xxx YOUTUBE_API_KEY_2=yyy python3 -u creator_full_refresh.py
In GitHub Actions: stored as encrypted repo Secrets, passed in via the
workflow's `env:` block (see .github/workflows/daily-refresh.yml).

SSL NOTE: macOS's python.org installer often doesn't wire into the system
certificate store, causing CERTIFICATE_VERIFY_FAILED on every request even
though the network connection itself is fine. Every request here tries a
normal verified HTTPS connection first, and only on ANY failure (not just
SSL-specific errors, since the exact failure mode varies by machine) falls
back once to an unverified SSL context.

HOW TO RUN:
    export YOUTUBE_API_KEY_1=your_key_here
    python3 -u creator_full_refresh.py
  (refreshes every creator across all 5 source files; can take a while —
  one channel lookup + one video-list lookup per creator, ~500 creators)

Writes updates back into each source file in place (same filenames, same
schema) after EVERY creator, so nothing is lost if it's interrupted.
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

WORKER_URL = "https://tight-cherry-1103.abhiraj-parashari.workers.dev/"

print = functools.partial(print, flush=True)

# Read from environment — see the module docstring above for why. Picks up
# YOUTUBE_API_KEY_1, YOUTUBE_API_KEY_2, YOUTUBE_API_KEY_3, ... in order,
# stopping at the first unset one. At least one is required.
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
        print("Set at least YOUTUBE_API_KEY_1 before running this script — see the")
        print("module docstring at the top of this file for exact instructions.")
        sys.exit(1)
    return keys

API_KEYS = _load_api_keys()
_key_idx = 0

NUM_RECENT_VIDEOS = 10  # how many recent uploads to average views/eng/cmt over

SOURCE_FILES = [
    "manual_refreshed.json",
    "sponsored_refreshed_seed.json",
    "scraped_refreshed.json",
    "trends_refreshed.json",
    "pending_creators.json",
]

WORKER_URL = "https://tight-cherry-1103.abhiraj-parashari.workers.dev/"
MIN_TAG_WEIGHT = 4
MAX_TAGS = 8


def worker_classify(text):
    if not text or len(text.strip()) < 20:
        return {}
    body = json.dumps({"action": "classifyCreator", "text": text[:4000]}).encode("utf-8")
    req = urllib.request.Request(
        WORKER_URL, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; LinkInBaayoBot/1.0)",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read())
    return data.get("weights") or {}


def weights_to_tag_string(weights):
    strong = sorted(
        [(cat, w) for cat, w in weights.items() if w >= MIN_TAG_WEIGHT],
        key=lambda x: -x[1],
    )[:MAX_TAGS]
    return " | ".join(cat for cat, _ in strong)


_UNVERIFIED_CTX = ssl._create_unverified_context()


def _fetch(url, insecure=False):
    ctx = _UNVERIFIED_CTX if insecure else None
    with urllib.request.urlopen(url, timeout=20, context=ctx) as resp:
        return json.loads(resp.read())


def api_get(path, params):
    """GET against the YouTube Data API, rotating keys on 403/400 quota errors.
    Tries a normal verified HTTPS request first; falls back to an unverified
    SSL context on ANY exception from that first attempt (macOS cert-store
    issue), not just SSL-specific ones — the failure mode varies by machine."""
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
    """Returns ('id', UC...) or ('handle', '@name') or ('user', 'name') from a channel URL."""
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
    """Resolve any channel URL shape to (channel_id, uploads_playlist_id, video_count) or None."""
    ref = extract_channel_ref(url)
    if not ref:
        return None
    kind, value = ref

    params = {"part": "contentDetails,statistics,snippet", "maxResults": 1}
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
    item = items[0]
    channel_id = item["id"]
    uploads_playlist = item["contentDetails"]["relatedPlaylists"]["uploads"]
    subs = int(item["statistics"].get("subscriberCount", 0))
    video_count = int(item["statistics"].get("videoCount", 0))
    description = item.get("snippet", {}).get("description", "")
    return {"channel_id": channel_id, "uploads_playlist": uploads_playlist, "subs": subs, "vids": video_count, "description": description}


def recent_video_ids(uploads_playlist, limit):
    data = api_get("playlistItems", {
        "part": "contentDetails", "playlistId": uploads_playlist, "maxResults": limit,
    })
    return [it["contentDetails"]["videoId"] for it in data.get("items", [])]


def video_stats(video_ids):
    if not video_ids:
        return [], None, []
    data = api_get("videos", {
        "part": "statistics,snippet", "id": ",".join(video_ids),
    })
    items = data.get("items", [])
    stats = []
    titles = []
    last_upload = None
    for it in items:
        st = it.get("statistics", {})
        views = int(st.get("viewCount", 0))
        likes = int(st.get("likeCount", 0))
        comments = int(st.get("commentCount", 0))
        stats.append((views, likes, comments))
        title = it.get("snippet", {}).get("title")
        if title:
            titles.append(title)
        published = it.get("snippet", {}).get("publishedAt")
        if published and (last_upload is None or published > last_upload):
            last_upload = published
    return stats, (last_upload.split("T")[0] if last_upload else None), titles

def refresh_one(url, need_niche=False):
    """Returns dict of updated fields, or None if the channel couldn't be resolved.
    need_niche is only ever True for creators that don't already have one
    (see refresh_file) — when set, auto-classifies a niche from the
    channel's own description + recent video titles instead of requiring a
    manual pick in the admin panel."""
    ch = resolve_channel(url)
    if not ch:
        return None

    vids_ids = recent_video_ids(ch["uploads_playlist"], NUM_RECENT_VIDEOS)
    stats, last_upload, titles = video_stats(vids_ids)

    if not stats:
        out = {"subs": ch["subs"], "vids": ch["vids"]}  # channel resolved but no recent videos found
        if need_niche:
            weights = worker_classify(ch.get("description", ""))
            tag_string = weights_to_tag_string(weights) if weights else ""
            if tag_string:
                out["niche"] = tag_string
        return out

    avg_views = sum(s[0] for s in stats) / len(stats)
    avg_likes = sum(s[1] for s in stats) / len(stats)
    avg_comments = sum(s[2] for s in stats) / len(stats)

    eng = round((avg_likes + avg_comments) / avg_views * 100, 3) if avg_views else 0.0
    cmt = round(avg_comments / avg_views * 100, 3) if avg_views else 0.0

    out = {
        "subs": ch["subs"],
        "vids": ch["vids"],
        "views": round(avg_views),
        "eng": eng,
        "cmt": cmt,
    }
    if last_upload:
        out["lastUpload"] = last_upload

    if need_niche:
        combined_text = (ch.get("description", "") + "\n" + "\n".join(titles)).strip()
        weights = worker_classify(combined_text)
        tag_string = weights_to_tag_string(weights) if weights else ""
        if tag_string:
            out["niche"] = tag_string
            print(f"(auto-classified niche: {tag_string})", end=" ")
    return out

def save(records, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)


def refresh_file(path):
    print(f"\n=== {path} ===")
    try:
        with open(path, "r", encoding="utf-8") as f:
            records = json.load(f)
    except FileNotFoundError:
        print(f"  ⚠ {path} not found — skipped")
        return

    if not records:
        print("  (empty — nothing to refresh)")
        return

    updated = 0
    skipped = 0
    for i, rec in enumerate(records, 1):
        name = rec.get("name", "?")
        url = rec.get("url")
        print(f"[{i}/{len(records)}] {name} ...", end=" ")
        if not url:
            print("no url on file — skipped")
            skipped += 1
            continue
        need_niche = path in ("pending_creators.json", "manual_refreshed.json") and not rec.get("niche")
        try:
            fresh = refresh_one(url, need_niche=need_niche)
        except Exception as e:
            print(f"⚠ error: {e}")
            skipped += 1
            continue
        if not fresh:
            print("⚠ couldn't resolve channel — skipped")
            skipped += 1
            continue
        rec.update(fresh)
        updated += 1
        print(f"subs={fresh.get('subs')} views={fresh.get('views','—')} eng={fresh.get('eng','—')}%")
        save(records, path)  # save after every creator — crash-safe
        time.sleep(0.3)

    print(f"  Done: {updated} updated, {skipped} skipped.")


def main():
    files = sys.argv[1:] if len(sys.argv) > 1 else SOURCE_FILES
    print(f"Refreshing {len(files)} file(s): {', '.join(files)}")
    for path in files:
        refresh_file(path)
    print("\nAll done. Refreshed *.json files are ready to merge back into index.html.")


if __name__ == "__main__":
    main()
