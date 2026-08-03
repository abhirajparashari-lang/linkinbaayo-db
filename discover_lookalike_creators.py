#!/usr/bin/env python3
"""
discover_lookalike_creators.py — nightly creator-discovery engine, pure
YouTube Data API (no browser rendering, no Gemini calls) so it stays cheap
and fast within your existing API-key quota.

HOW IT WORKS
1. Reads all currently-tracked creators (manual_refreshed.json,
   sponsored_refreshed_seed.json, scraped_refreshed.json, trends_refreshed.json,
   pending_creators.json, AND MANUAL_CREATORS embedded in index.html — the
   last of which is where every admin-added and every previously-discovered
   creator ends up once promote_pending_creators.js runs) to know who we
   already have, and their niche.
2. Reads content_refreshed.json (built nightly by top_content_refresh.py) to
   pull real, currently-working video titles per creator, grouped by niche —
   this is the actual language your successful creators' best videos use,
   not a guess.
3. For each niche, extracts the most common meaningful keywords from those
   titles, and runs ONE YouTube search.list(type=video, order=viewCount,
   publishedAfter=<45 days>) per niche using those keywords — surfaces
   videos that are ranking well on the exact same topics right now.
4. Collects the unique channels behind those videos, drops any already in
   the database, and pulls real stats (channels.list + recent video stats)
   for the rest.
5. Scores each new candidate on subscriber range, engagement rate, views-
   per-subscriber (a proxy for "punching above their weight" / momentum),
   and upload recency. Only candidates clearing every threshold pass.
6. Passing candidates are written straight into pending_creators.json with
   real, already-resolved stats (not placeholders) — so the very next step
   in the pipeline, promote_pending_creators.js, folds them into
   MANUAL_CREATORS automatically. No manual review step, no scraping, no
   extra Gemini calls.

QUOTA COST: ~1 search.list call per niche (100 units each) + ~2 cheap
calls (1 unit each) per surfaced candidate channel. For ~13 niches and a
few hundred candidate videos total, that's roughly 1,300-1,800 units —
well inside a single key's 10,000/day free quota, and this project
already rotates multiple keys (YOUTUBE_API_KEY_1, _2, ...).

CAPS (tune below): MAX_NEW_PER_RUN limits how many creators get added in
one night so growth stays gradual and reviewable via git history.

HOW TO RUN:
  YOUTUBE_API_KEY_1=xxx python3 -u discover_lookalike_creators.py
(also wired into daily-refresh.yml, right after creator_full_refresh.py
and before promote_pending_creators.js)
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
from collections import Counter, defaultdict

print = functools.partial(print, flush=True)

WORKER_URL = "https://tight-cherry-1103.abhiraj-parashari.workers.dev/"


def worker_classify(text):
    """Reuses the same Brand Match / classify endpoint on the Cloudflare
    Worker that resolve_sponsored_videos.py and creator_full_refresh.py
    already call, so a discovered candidate's real content (description +
    recent video titles) gets a real category instead of just inheriting
    whichever niche's search query happened to surface it."""
    if not text or len(text.strip()) < 20:
        return {}
    body = json.dumps({"text": text[:4000]}).encode("utf-8")
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


# ── config ──────────────────────────────────────────────────────────────
SOURCE_FILES = [
    "manual_refreshed.json",
    "sponsored_refreshed_seed.json",
    "scraped_refreshed.json",
    "trends_refreshed.json",
]
PENDING_FILE = "pending_creators.json"
CONTENT_FILE = "content_refreshed.json"

MIN_SUBS = 3000
MAX_SUBS = 500000
MIN_ENG_PCT = 1.2   # confirmed via a full 200-candidate test run: at 1.5% most
                    # near-misses sat around 1.0-1.4% engagement, not
                    # meaningfully worse creators, just short of the line.
                    # 1.2% let 8 more real candidates through without letting
                    # in anything junky.
MAX_DAYS_SINCE_UPLOAD = 30
MIN_VIEWS_PER_SUB = 0.05          # avg recent-video views >= 5% of sub count
KEYWORDS_PER_NICHE = 5            # keyword pool size; only the top QUERY_KEYWORDS
QUERY_KEYWORDS = 2                # of these are actually joined into the search
                                   # query — joining more (tried 4) over-narrows
                                   # the query and returns zero results, confirmed
                                   # empirically in testing
VIDEOS_PER_NICHE_SEARCH = 25
MAX_NEW_PER_RUN = 15
RECENT_DAYS_WINDOW = 45

# Channels that look like meme/reaction/compilation accounts (numbered clip
# series, reaction pages, reposted content) clear the numeric filters fine
# but aren't real creators worth pitching to brands. Confirmed in testing:
# a "calisthenics" search surfaced several "Power of Calisthenics Part N"
# clip-compilation channels that otherwise looked like strong candidates.
BLOCKLIST_TERMS = [
    "compilation", "reaction", "reacts to", "edits page", "edit page",
    "fan page", "fanpage", "clips page", "repost",
    "unboxing", "unbox ",  # generic gadget-unboxing content confirmed in
                            # testing to surface under "Tech / Lifestyle" —
                            # off-brand for this database even though the
                            # channel technically clears every numeric bar.
]
# A channel whose recent uploads are mostly numbered ("Part 12", "Ep 4") is
# almost always a clip/compilation series rather than an original creator.
PART_NUMBER_RE = re.compile(r"\bpart\s*\d+\b|\bep(isode)?\s*\d+\b", re.I)

STOPWORDS = set("""
the a an and or but for with without your you i my our we is are was were
this that these those how to of in on at from best top new full video
episode part honest days challenge shorts short viral youtubeshorts
shortsfeed watch guess want love what does take back some didn subscribe
more others house life
""".split())

_UNVERIFIED_CTX = ssl._create_unverified_context()


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


def _fetch(url, insecure=False):
    ctx = _UNVERIFIED_CTX if insecure else None
    with urllib.request.urlopen(url, timeout=25, context=ctx) as resp:
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


HTML_PATH = "index.html"


def load_manual_creators_from_html(html_path=HTML_PATH):
    """MANUAL_CREATORS lives embedded directly in index.html, not a JSON
    file — every creator added via the admin panel AND every creator this
    discovery engine adds ends up here once promote_pending_creators.js
    runs. Confirmed as a real bug in testing: without reading this block,
    load_known()'s dedup set loses track of a creator the moment they're
    promoted out of pending_creators.json, and this script could
    re-discover and re-add the exact same channel every single night."""
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        return []
    start_marker = "const MANUAL_CREATORS = ["
    start = html.find(start_marker)
    if start == -1:
        return []
    block_start = start + len(start_marker)
    end = html.find("\n];", block_start)
    if end == -1:
        return []
    block = html[block_start:end]
    creators = []
    # Each record is one line, e.g.:
    #   { name:"X", url:"Y", subs:1, niche:"Z", eng:1, cmt:1, views:1, vids:1, notes:"...", lastUpload:"..." },
    for m in re.finditer(
        r'name\s*:\s*"((?:[^"\\]|\\.)*)".*?url\s*:\s*"((?:[^"\\]|\\.)*)".*?niche\s*:\s*"((?:[^"\\]|\\.)*)"',
        block,
    ):
        name, url, niche = m.groups()
        creators.append({"name": name, "url": url, "niche": niche})
    return creators


# ── load existing data ─────────────────────────────────────────────────
def load_known():
    creators = []
    for f in SOURCE_FILES + [PENDING_FILE]:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                creators.extend(json.load(fh))
        except FileNotFoundError:
            continue
    creators.extend(load_manual_creators_from_html())
    return creators


def normalize_handle(url):
    """Extract a comparable handle/id fragment from a youtube.com URL."""
    if not url:
        return None
    m = re.search(r"youtube\.com/(@[\w.-]+|channel/[\w-]+|c/[\w.-]+|user/[\w.-]+)", url, re.I)
    return m.group(1).lower() if m else url.lower()


def primary_niche(raw):
    """Many creator records store a pipe-joined multi-tag niche string
    (e.g. "Fitness | Vlogging / Daily Life | Travel"). Using the whole
    string as a bucket fragments the keyword pool into dozens of tiny,
    noisy groups — confirmed in testing, where most buckets ended up with
    only 3-6 titles each. Using just the first/primary tag gives fewer,
    cleaner, better-populated buckets."""
    raw = (raw or "").strip()
    return raw.split("|")[0].strip() if raw else ""


def load_content_by_niche(known_creators):
    # IMPORTANT: most of the database (manual_refreshed.json,
    # sponsored_refreshed_seed.json, scraped_refreshed.json,
    # trends_refreshed.json — 497 of 497 creators checked) tags creators
    # with a "category" field, e.g. "Skincare | Haircare | Cosmetics /
    # Makeup". Only creators added via the admin panel and folded into
    # MANUAL_CREATORS by promote_pending_creators.js get a "niche" field
    # instead (that script writes niche: explicitly). Reading "niche" only
    # was a real bug found in testing — it silently produced a niche
    # distribution of 86% "blank," and completely missed every Skincare,
    # Cosmetics, Haircare, Nutrition/Supplements, and Wellness/Ayurveda
    # creator, i.e. most of this project's actual core categories. Reading
    # "category" first (falling back to "niche") fixes this.
    name_to_niche = {
        (c.get("name") or "").lower(): primary_niche(c.get("category") or c.get("niche"))
        for c in known_creators if c.get("name")
    }
    by_niche = defaultdict(list)
    try:
        with open(CONTENT_FILE, "r", encoding="utf-8") as f:
            content = json.load(f)
    except FileNotFoundError:
        return by_niche
    for item in content:
        niche = name_to_niche.get((item.get("name") or "").lower())
        if niche and item.get("title"):
            by_niche[niche].append(item["title"])
    return by_niche


def top_keywords(titles, n):
    words = Counter()
    for t in titles:
        for w in re.findall(r"[a-zA-Z]{4,}", t.lower()):
            if w not in STOPWORDS:
                words[w] += 1
    return [w for w, _ in words.most_common(n)]


# ── YouTube calls ──────────────────────────────────────────────────────
def search_videos(query, published_after):
    data = api_get("search", {
        "part": "snippet", "q": query, "type": "video", "order": "viewCount",
        "publishedAfter": published_after, "maxResults": VIDEOS_PER_NICHE_SEARCH,
        "regionCode": "IN", "relevanceLanguage": "en",
    })
    return data.get("items", [])


def get_channel(channel_id):
    data = api_get("channels", {"part": "snippet,statistics,contentDetails", "id": channel_id})
    items = data.get("items", [])
    return items[0] if items else None


def get_recent_video_stats(uploads_playlist_id, max_n=5):
    pl = api_get("playlistItems", {
        "part": "contentDetails", "playlistId": uploads_playlist_id, "maxResults": max_n,
    })
    video_ids = [i["contentDetails"]["videoId"] for i in pl.get("items", [])]
    if not video_ids:
        return []
    data = api_get("videos", {"part": "snippet,statistics", "id": ",".join(video_ids)})
    out = []
    for it in data.get("items", []):
        st = it.get("statistics", {})
        # A creator can hide their like count — when that happens the
        # "likeCount" key is simply absent from the API response, not zero.
        # Treating a missing key as 0 (the old behavior) silently tanked
        # engagement to 0% for otherwise-strong channels — confirmed in
        # testing on a channel with 3.8M avg views. Track "likes_known"
        # so the engagement calc below can skip videos with hidden likes
        # instead of falsely zeroing them out.
        out.append({
            "views": int(st.get("viewCount", 0)),
            "likes": int(st["likeCount"]) if "likeCount" in st else None,
            "comments": int(st.get("commentCount", 0)),
            "published": it["snippet"]["publishedAt"][:10],
            "title": it["snippet"].get("title", ""),
        })
    return out


# ── main ────────────────────────────────────────────────────────────────
def main():
    known = load_known()
    known_handles = {normalize_handle(c.get("url")) for c in known if c.get("url")}
    by_niche = load_content_by_niche(known)

    if not by_niche:
        print("No content_refreshed.json data to build keywords from yet — "
              "run top_content_refresh.py at least once first. Skipping this run.")
        return

    published_after = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime(time.time() - RECENT_DAYS_WINDOW * 86400))

    niche_candidates = {}  # niche -> ordered list of channel ids
    seen_cids = set()
    for niche, titles in by_niche.items():
        kws = top_keywords(titles, KEYWORDS_PER_NICHE)
        if not kws:
            continue
        query = " ".join(kws[:QUERY_KEYWORDS])
        print(f"[{niche}] searching: {query}")
        try:
            videos = search_videos(query, published_after)
        except Exception as e:
            print(f"  ⚠ search failed for {niche}: {e}")
            continue
        ids = []
        for v in videos:
            cid = v["snippet"]["channelId"]
            if cid not in seen_cids:
                seen_cids.add(cid)
                ids.append(cid)
        if ids:
            niche_candidates[niche] = ids

    # Round-robin across niches instead of exhausting one niche's candidates
    # before moving to the next. Confirmed in testing that without this, a
    # MAX_NEW_PER_RUN=15 cap stopped evaluation after only 4 niches
    # (Fitness/Lifestyle/Food/Nutrition) — Skincare, this project's single
    # largest category by creator count, never got evaluated in the same
    # run simply because of dict iteration order. Interleaving guarantees
    # every niche gets a fair shot each run.
    ordered_candidates = []
    niche_queues = {n: list(ids) for n, ids in niche_candidates.items()}
    while any(niche_queues.values()):
        for n in list(niche_queues.keys()):
            q = niche_queues[n]
            if q:
                ordered_candidates.append((q.pop(0), n))

    print(f"\n{len(ordered_candidates)} unique candidate channels found across all niches.")

    new_creators = []
    checked = 0
    for cid, niche in ordered_candidates:
        if len(new_creators) >= MAX_NEW_PER_RUN:
            print(f"Hit MAX_NEW_PER_RUN ({MAX_NEW_PER_RUN}) — stopping evaluation early.")
            break
        checked += 1
        try:
            ch = get_channel(cid)
        except Exception as e:
            print(f"  ⚠ channel lookup failed for {cid}: {e}")
            continue
        if not ch:
            continue

        sn, st, cd = ch.get("snippet", {}), ch.get("statistics", {}), ch.get("contentDetails", {})
        subs = int(st.get("subscriberCount", 0))
        handle = sn.get("customUrl", "")
        if handle and not handle.startswith("@"):
            handle = "@" + handle
        url = f"https://youtube.com/{handle}" if handle else f"https://youtube.com/channel/{cid}"
        norm = normalize_handle(url)

        if norm in known_handles:
            continue
        if not (MIN_SUBS <= subs <= MAX_SUBS):
            continue

        title_desc = f"{sn.get('title','')} {sn.get('description','')}".lower()
        if any(term in title_desc for term in BLOCKLIST_TERMS):
            print(f"  [skip] {sn.get('title')} — blocklisted term in title/description (looks like a compilation/reaction page)")
            continue

        # Country is self-reported and a LOT of creators never set it — hard-
        # requiring country == "IN" would exclude plenty of genuinely Indian
        # creators who just left the field blank. So this only excludes a
        # candidate when the channel has explicitly declared a different
        # country; an unset/blank country is left in (search.list's
        # regionCode="IN" already biases results toward India upstream).
        declared_country = sn.get("country")
        if declared_country and declared_country != "IN":
            print(f"  [skip] {sn.get('title')} — declared country is {declared_country}, not India")
            continue

        uploads_id = cd.get("relatedPlaylists", {}).get("uploads")
        if not uploads_id:
            continue
        try:
            recent = get_recent_video_stats(uploads_id)
        except Exception as e:
            print(f"  ⚠ recent-video lookup failed for {sn.get('title')}: {e}")
            continue
        if not recent:
            continue

        numbered = sum(1 for v in recent if PART_NUMBER_RE.search(v["title"]))
        if numbered >= max(2, len(recent) // 2):
            print(f"  [skip] {sn.get('title')} — most recent uploads are numbered clips ('Part N'/'Ep N'), looks like a compilation series")
            continue

        # Blocklist terms (e.g. "unboxing") often show up in video titles
        # rather than the channel's own bio — checking recent video titles
        # too catches generic gadget-unboxing/off-brand content that passes
        # a channel-description-only check clean.
        recent_titles_blob = " ".join(v["title"].lower() for v in recent)
        if any(term in recent_titles_blob for term in BLOCKLIST_TERMS):
            print(f"  [skip] {sn.get('title')} — blocklisted term in recent video titles (e.g. unboxing/generic gadget content)")
            continue

        avg_views = sum(v["views"] for v in recent) / len(recent)
        # Only average engagement over videos that actually report a like
        # count — a channel that hides likes on some/all videos should not
        # have those treated as 0 likes, which would unfairly tank its score.
        with_likes = [v for v in recent if v["likes"] is not None]
        if with_likes:
            avg_eng = sum(v["likes"] + v["comments"] for v in with_likes) / len(with_likes)
            eng_pct = round((avg_eng / avg_views * 100), 2) if avg_views else 0.0
            eng_known = True
        else:
            eng_pct = None
            eng_known = False
        cmt_pct = round((sum(v["comments"] for v in recent) / len(recent) / avg_views * 100), 2) if avg_views else 0.0
        most_recent_date = max(v["published"] for v in recent)
        days_since = (time.time() - time.mktime(time.strptime(most_recent_date, "%Y-%m-%d"))) / 86400
        views_per_sub = (avg_views / subs) if subs else 0

        # If likes are hidden on every recent video we can't score engagement
        # directly — fall back to requiring a stronger views-per-sub signal
        # instead of failing the candidate outright over missing data.
        eng_ok = (eng_pct is not None and eng_pct >= MIN_ENG_PCT) if eng_known else (views_per_sub >= MIN_VIEWS_PER_SUB * 3)
        passes = (
            eng_ok and
            days_since <= MAX_DAYS_SINCE_UPLOAD and
            views_per_sub >= MIN_VIEWS_PER_SUB
        )
        eng_label = f"{eng_pct}%" if eng_known else "hidden"
        print(f"  [{'PASS' if passes else 'skip'}] {sn.get('title')} — "
              f"subs={subs}, eng={eng_label}, views/sub={views_per_sub:.2f}, last upload {days_since:.0f}d ago")

        known_handles.add(norm)  # dedupe within this run too
        if not passes:
            continue

        # "niche" here is just whichever niche's search query surfaced this
        # candidate — not a real classification. Confirmed via testing: a
        # contaminated "Wellness / Ayurveda" keyword pool (top query ended
        # up being "nykaa review") surfaced a pure lipstick/makeup swatch
        # channel with zero content verification. Classify off the
        # candidate's own real content instead, falling back to the
        # search-source niche only if classification fails or is empty.
        classify_text = f"{sn.get('description','')}\n" + "\n".join(v["title"] for v in recent)
        final_niche = niche
        try:
            weights = worker_classify(classify_text.strip())
            if weights:
                final_niche = max(weights, key=weights.get)
        except Exception as e:
            print(f"  (classify failed for {sn.get('title')}, keeping search-source niche '{niche}': {e})")

        new_creators.append({
            "name": sn.get("title", ""),
            "url": url,
            "subs": subs,
            "niche": final_niche,
            "eng": eng_pct if eng_known else 0,
            "cmt": cmt_pct,
            "views": round(avg_views),
            "vids": int(st.get("videoCount", 0)),
            "notes": f"Auto-discovered by lookalike engine ({time.strftime('%Y-%m-%d')}) — "
                     f"surfaced via {niche} search"
                     + (f", classified as {final_niche}" if final_niche != niche else "")
                     + ", meets engagement/momentum criteria.",
            "lastUpload": most_recent_date,
        })

    print(f"\nChecked {checked} candidate channels, {len(new_creators)} passed all criteria.")

    if new_creators:
        try:
            with open(PENDING_FILE, "r", encoding="utf-8") as f:
                pending = json.load(f)
        except FileNotFoundError:
            pending = []
        pending.extend(new_creators)
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump(pending, f, indent=2, ensure_ascii=False)
        print(f"Wrote {len(new_creators)} new creator(s) into {PENDING_FILE} — "
              f"promote_pending_creators.js will fold them into MANUAL_CREATORS this run.")
        print("  " + ", ".join(c["name"] for c in new_creators))
    else:
        print("Nothing new cleared the bar this run.")


if __name__ == "__main__":
    main()
