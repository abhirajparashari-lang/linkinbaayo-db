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
genuinely strong category matches (weight >= MIN_TAG_WEIGHT).

BATCHED, NOT ONE-AT-A-TIME: an earlier per-creator version (one Gemini call
per creator) blew through Gemini's rate limit almost immediately — a full
run's worth of classify calls failed with 502s from sheer request-count
volume. This version bundles CREATORS_PER_BATCH_CALL creators into a single
Gemini call via the Worker's "classifyCreatorsBatch" action, cutting actual
API request count by roughly that factor while covering the same or more
creators per run. This is the real lever for running fast across the whole
database without tripping the limit.

RESUMABLE BY DESIGN: processes at most RETAG_BATCH_SIZE creators per run
(tracked via retag_progress.json, which the workflow now commits back to the
repo) so already-classified creators are never redone. Safe to re-run any
time. Once retag_progress.json covers everyone, this becomes a no-op — new
creators are handled by creator_full_refresh.py / discover_lookalike_creators.py
directly, not this script.

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

CREATORS_PER_BATCH_CALL = 10   # creators bundled into ONE Gemini call — the
                                # real lever for speed + staying under the
                                # rate limit at the same time
RETAG_BATCH_SIZE = 200         # total creators processed per run — safe to
                                # set high now that actual API call count is
                                # roughly RETAG_BATCH_SIZE / CREATORS_PER_BATCH_CALL
SLEEP_BETWEEN_BATCH_CALLS = 3  # seconds between each batch Gemini call
MIN_TAG_WEIGHT = 4             # only keep categories Gemini scores >= this out
                                # of 10 — the actual noise filter
MAX_TAGS = 8                   # soft ceiling only, not a real constraint —
                                # MIN_TAG_WEIGHT does the real filtering

SOURCE_FILES = [
    "manual_refreshed.json",
    "sponsored_refreshed_seed.json",
    "scraped_refreshed.json",
    "trends_refreshed.json",
]
CONTENT_FILE = "content_refreshed.json"
CHECKPOINT_FILE = "retag_progress.json"
HTML_PATH = "index.html"


def worker_classify_batch(items, retries=1, backoff=20):
    """items: list of {"id": str, "text": str}. Returns {id: weights_dict}.
    Retries once after a real pause on failure — a transient blip shouldn't
    waste a whole batch of creators."""
    if not items:
        return {}
    body = json.dumps({"action": "classifyCreatorsBatch", "items": items}).encode("utf-8")
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                WORKER_URL, data=body, method="POST",
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (compatible; LinkInBaayoBot/1.0)",
                },
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            return data.get("results") or {}
        except Exception:
            if attempt < retries:
                time.sleep(backoff)
                continue
            raise


def weights_to_tag_string(weights):
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


def run_batches(candidates, apply_fn):
    """candidates: list of dicts with at least {"key", "id_for_batch", "text"}.
    Chunks into CREATORS_PER_BATCH_CALL-sized groups, classifies each chunk
    in one Gemini call, and calls apply_fn(candidate, tag_string) for every
    candidate that got a non-empty result. Returns how many were processed
    (attempted) this run, regardless of whether the tag actually changed."""
    processed = 0
    for i in range(0, len(candidates), CREATORS_PER_BATCH_CALL):
        chunk = candidates[i:i + CREATORS_PER_BATCH_CALL]
        items = [{"id": c["id_for_batch"], "text": c["text"]} for c in chunk]
        try:
            results = worker_classify_batch(items)
        except Exception as e:
            print(f"  ⚠ batch classify failed for {len(chunk)} creator(s): {e}")
            results = {}
        for c in chunk:
            weights = results.get(c["id_for_batch"]) or {}
            tag_string = weights_to_tag_string(weights) if weights else ""
            apply_fn(c, tag_string)
            processed += 1
        time.sleep(SLEEP_BETWEEN_BATCH_CALLS)
    return processed


def main():
    done = load_checkpoint()
    titles_by_name = load_titles_by_name()
    if not titles_by_name:
        print("No content_refreshed.json data yet — run top_content_refresh.py first. Skipping.")
        return

    changed_files = set()
    total_processed = 0

    # ── 4 flat JSON source files (category field) ──────────────────────
    file_records = {}   # path -> records (loaded once, saved once)
    candidates = []      # list of dicts describing a classify candidate
    for path in SOURCE_FILES:
        try:
            with open(path, "r", encoding="utf-8") as f:
                records = json.load(f)
        except FileNotFoundError:
            continue
        file_records[path] = records
        for idx, rec in enumerate(records):
            if total_processed + len(candidates) >= RETAG_BATCH_SIZE:
                break
            key = f"{path}|{(rec.get('name') or '').lower()}"
            if key in done:
                continue
            titles = titles_by_name.get((rec.get("name") or "").lower())
            if not titles:
                continue
            candidates.append({
                "key": key, "path": path, "idx": idx,
                "id_for_batch": f"file{len(candidates)}",
                "text": "\n".join(titles),
                "old": rec.get("category"),
            })

    def apply_file_candidate(c, tag_string):
        done.add(c["key"])
        if tag_string and tag_string != c["old"]:
            print(f"  {file_records[c['path']][c['idx']].get('name')} ({c['path']}): \"{c['old']}\" -> \"{tag_string}\"")
            file_records[c["path"]][c["idx"]]["category"] = tag_string
            changed_files.add(c["path"])

    if candidates:
        total_processed += run_batches(candidates, apply_file_candidate)
        for path in set(c["path"] for c in candidates):
            if path in changed_files:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(file_records[path], f, indent=2, ensure_ascii=False)

    # ── MANUAL_CREATORS embedded in index.html (niche field) ───────────
    if total_processed < RETAG_BATCH_SIZE:
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
                html_candidates = []
                for li, line in enumerate(lines):
                    if total_processed + len(html_candidates) >= RETAG_BATCH_SIZE:
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
                    html_candidates.append({
                        "key": key, "line_idx": li, "name": name, "old_niche": niche,
                        "id_for_batch": f"html{len(html_candidates)}",
                        "text": "\n".join(titles),
                    })

                html_changed = False

                def apply_html_candidate(c, tag_string):
                    nonlocal html_changed
                    done.add(c["key"])
                    if tag_string and tag_string != c["old_niche"]:
                        print(f"  {c['name']} (MANUAL_CREATORS): \"{c['old_niche']}\" -> \"{tag_string}\"")
                        line = lines[c["line_idx"]]
                        lines[c["line_idx"]] = line.replace(
                            f'niche:"{c["old_niche"]}"', f'niche:"{tag_string}"'
                        ).replace(
                            f'niche: "{c["old_niche"]}"', f'niche: "{tag_string}"'
                        )
                        html_changed = True

                if html_candidates:
                    total_processed += run_batches(html_candidates, apply_html_candidate)

                if html_changed:
                    block = "\n".join(lines)
                    html = html[:block_start] + block + html[end:]
                    with open(HTML_PATH, "w", encoding="utf-8") as f:
                        f.write(html)
                    changed_files.add(HTML_PATH)

    save_checkpoint(done)
    print(f"\nProcessed {total_processed} creator(s) this run "
          f"({len(done)} total done across all runs so far).")
    if changed_files:
        print(f"Changed: {', '.join(sorted(changed_files))}")
    else:
        print("No tag changes this run.")


if __name__ == "__main__":
    main()
