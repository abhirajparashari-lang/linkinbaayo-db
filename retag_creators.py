#!/usr/bin/env python3
"""
retag_creators.py — one-time (resumable) backfill that reclassifies every
tracked creator's niche/category as a real, nuanced, multi-tag profile based
on their own actual recent video titles (already refreshed daily by
top_content_refresh.py into content_refreshed.json), instead of whatever
single category they were seeded/discovered with.

WHY: most of this database's "category" field was set once during initial
scraping and is a single guess; discovered/admin-added creators either
inherited whichever niche's search query happened to surface them (pure
coincidence — confirmed on a real creator: a lipstick/makeup-swatch channel
that got mislabeled "Wellness / Ayurveda" purely because that niche's
keyword pool happened to contain "nykaa review") or got a single manually-
picked dropdown value. Meanwhile top_content_refresh.py is ALREADY pulling
real, fresh video titles for every single creator every night — this script
just puts that existing data to use: classify off real titles, keep only
genuinely strong category matches (weight >= MIN_TAG_WEIGHT), cap at
MAX_TAGS so it stays nuanced (a real vlogger/traveler/fitness/fashion
creator keeps every real signal) without turning into "picks up anything."

RESUMABLE BY DESIGN: processes at most RETAG_BATCH_SIZE creators per run
(tracked via retag_progress.json) so a database of hundreds of creators gets
covered gradually over several nightly runs instead of one giant batch that
blows through Gemini's rate limits. Safe to re-run any time — already-
processed creators are skipped. Once retag_progress.json covers everyone,
this becomes a no-op (new creators are handled by creator_full_refresh.py /
discover_lookalike_creators.py directly, not this script).

Run right after "Merge refreshed content into index.html" (needs fresh
content_refreshed.json AND the current index.html) and before the final git
commit step, so any edits land in the same commit as everything else.
"""

import json
import re
import time
import functools
import urllib.request

print = functools.partial(print, flush=True)

WORKER_URL = "https://tight-cherry-1103.abhiraj-parashari.workers.dev/"

RETAG_BATCH_SIZE = 80   # creators processed per run — tune based on how many
                        # nights you're comfortable spreading the backfill over
MIN_TAG_WEIGHT = 4      # only keep categories Gemini scores >= this out of 10 —
                        # drops the "loose fit, weight 1-3" tags the classify
                        # prompt allows for brands, which would otherwise make
                        # creator tagging noisy ("picks up anything")
MAX_TAGS = 8            # soft ceiling only, not a real design constraint —
                        # MIN_TAG_WEIGHT is the actual filter now that the
                        # Worker's classifyCreator prompt never forces a tag
                        # without real, repeated evidence

SOURCE_FILES = [
    "manual_refreshed.json",
    "sponsored_refreshed_seed.json",
    "scraped_refreshed.json",
    "trends_refreshed.json",
]
CONTENT_FILE = "content_refreshed.json"
CHECKPOINT_FILE = "retag_progress.json"
HTML_PATH = "index.html"


def worker_classify(text):
    """Same Brand Match / classify endpoint on the Cloudflare Worker used
    elsewhere in this project (resolve_sponsored_videos.py,
    creator_full_refresh.py, discover_lookalike_creators.py)."""
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
    """Keeps only genuinely strong matches and caps how many, so this stays
    nuanced instead of noisy."""
    strong = sorted(
        [(cat, w) for cat, w in weights.items() if w >= MIN_TAG_WEIGHT],
        key=lambda x: -x[1],
    )[:MAX_TAGS]
    return " | ".join(cat for cat, _ in strong)


def load_checkpoint():
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def save_checkpoint(done):
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(done), f, indent=2)


def load_titles_by_name():
    """Groups content_refreshed.json's real video titles by creator name
    (case-insensitive) — this is the actual signal we classify off."""
    try:
        with open(CONTENT_FILE, "r", encoding="utf-8") as f:
            content = json.load(f)
    except FileNotFoundError:
        return {}
    by_name = {}
    for item in content:
        name = (item.get("name") or "").strip().lower()
        if name and item.get("title"):
            by_name.setdefault(name, []).append(item["title"])
    return by_name


def load_manual_creators_block(html):
    start_marker = "const MANUAL_CREATORS = ["
    start = html.find(start_marker)
    if start == -1:
        return None, None, None
    block_start = start + len(start_marker)
    end = html.find("\n];", block_start)
    if end == -1:
        return None, None, None
    return start, block_start, end


def main():
    done = load_checkpoint()
    titles_by_name = load_titles_by_name()
    if not titles_by_name:
        print("No content_refreshed.json data yet — run top_content_refresh.py first. Skipping.")
        return

    processed_this_run = 0
    changed_files = set()

    # ── 4 flat JSON source files (category field) ──────────────────────
    for path in SOURCE_FILES:
        if processed_this_run >= RETAG_BATCH_SIZE:
            break
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except FileNotFoundError:
            continue
        file_changed = False
        for rec in records:
            if processed_this_run >= RETAG_BATCH_SIZE:
                break
            key = f"{path}|{(rec.get('name') or '').lower()}"
            if key in done:
                continue
            titles = titles_by_name.get((rec.get("name") or "").lower())
            if not titles:
                continue  # no fresh content yet for this creator — retry next run
            try:
                weights = worker_classify("\n".join(titles))
            except Exception as e:
                print(f"  ⚠ classify failed for {rec.get('name')} ({path}): {e}")
                continue
            tag_string = weights_to_tag_string(weights)
            if tag_string and tag_string != rec.get("category"):
                print(f"  {rec.get('name')} ({path}): \"{rec.get('category')}\" -> \"{tag_string}\"")
                rec["category"] = tag_string
                file_changed = True
            done.add(key)
            processed_this_run += 1
            time.sleep(4.5)
        if file_changed:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2, ensure_ascii=False)
            changed_files.add(path)

    # ── MANUAL_CREATORS embedded in index.html (niche field) ───────────
    if processed_this_run < RETAG_BATCH_SIZE:
        try:
            with open(HTML_PATH, "r", encoding="utf-8") as f:
                html = f.read()
        except FileNotFoundError:
            html = None

        if html:
            start, block_start, end = load_manual_creators_block(html)
            if block_start is not None:
                block = html[block_start:end]
                lines = block.split("\n")
                html_changed = False
                for li, line in enumerate(lines):
                    if processed_this_run >= RETAG_BATCH_SIZE:
                        break
                    m = re.search(
                        r'name\s*:\s*"((?:[^"\\]|\\.)*)".*?niche\s*:\s*"((?:[^"\\]|\\.)*)"',
                        line,
                    )
                    if not m:
                        continue
                    name, niche = m.groups()
                    key = f"MANUAL_CREATORS|{name.lower()}"
                    if key in done:
                        continue
                    titles = titles_by_name.get(name.lower())
                    if not titles:
                        continue
                    try:
                        weights = worker_classify("\n".join(titles))
                    except Exception as e:
                        print(f"  ⚠ classify failed for {name} (MANUAL_CREATORS): {e}")
                        continue
                    tag_string = weights_to_tag_string(weights)
                    if tag_string and tag_string != niche:
                        print(f"  {name} (MANUAL_CREATORS): \"{niche}\" -> \"{tag_string}\"")
                        lines[li] = line.replace(f'niche:"{niche}"', f'niche:"{tag_string}"') \
                                         .replace(f'niche: "{niche}"', f'niche: "{tag_string}"')
                        html_changed = True
                    done.add(key)
                    processed_this_run += 1
                    time.sleep(0.2)

                if html_changed:
                    block = "\n".join(lines)
                    html = html[:block_start] + block + html[end:]
                    with open(HTML_PATH, "w", encoding="utf-8") as f:
                        f.write(html)
                    changed_files.add(HTML_PATH)

    save_checkpoint(done)
    print(f"\nProcessed {processed_this_run} creator(s) this run "
          f"({len(done)} total done across all runs so far).")
    if changed_files:
        print(f"Changed: {', '.join(sorted(changed_files))}")
    else:
        print("No tag changes this run.")


if __name__ == "__main__":
    main()
