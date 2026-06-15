#!/usr/bin/env python3
"""
Roulette Random - Game Pool Harvester
Runs weekly via GitHub Actions.

Fully automated, zero manual input. Each run:
  1. Sweeps a large keyword list through Roblox's omni-search API, which
     returns REAL, played games (universeId, name, rootPlaceId, live player
     count, votes, maturity) - no junk personal places.
  2. Enriches each game with canonical metadata (visits, genre, maxPlayers)
     via the public /v1/games batch endpoint.
  3. Filters by maturity + a light quality floor, dedupes, shuffles.
  4. Writes data/games.json - the array the Roblox experience fetches at
     startup to build its spin pool.

The keyword list is shuffled with a per-ISO-week seed, so every weekly run
produces a fresh random batch of experiences (and re-runs within the same
week stay stable).
"""

import datetime
import json
import random
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "games.json"

TARGET_GAMES   = 6000   # stop discovery once we have this many unique games
MAX_POOL       = 6000   # final pool size cap
WORKERS        = 8      # concurrent HTTP requests (I/O-bound)
ENRICH         = False  # fetch visits/genre/maxPlayers via /v1/games. Off by
                        # default: it's the most rate-throttled phase and genre
                        # is ~82% useless "All"; omni already gives us live
                        # players + votes, which are better signals anyway.
KEYWORDS_PER_RUN = 120  # keywords actually swept per run (enough to hit target)
SAMPLE_KEYWORDS = 350   # random words drawn from the big list per run
PAGES_PER_KW   = 3      # omni-search result pages to collect per keyword
PAGE1_KEEP     = 0.5    # of page 1 (the popular head), randomly keep this fraction
                        # -> giants stay eligible but diluted, not taken wholesale.
                        # deeper (niche) pages are always kept in full.
MIN_PLAYERS    = 0      # live-player floor (0 = allow quiet games for variety)
MIN_UPVOTES    = 10     # light community-signal floor so games aren't dead/broken,
                        # low enough to keep the niche tail we actually want
MAX_MIN_AGE    = 13     # drop experiences whose minimum age is above this (family-safe-ish)
MAX_VISITS     = 0      # optional popularity cap (0 = disabled; keep top games eligible)

KEYWORDS_FILE  = Path(__file__).parent / "keywords.txt"

OMNI_URL  = "https://apis.roblox.com/search-api/omni-search"
GAMES_URL = "https://games.roblox.com/v1/games"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RouletteRandomHarvester/2.0",
    "Accept": "application/json",
})

# A small curated genre set, always mixed in so every pool is guaranteed some
# cleanly genre-taggable games. The BULK of each sweep comes from random words
# sampled out of keywords.txt (~8.7k common English words) - searching obscure
# words surfaces obscure, popularity-agnostic games, which is where real
# randomness comes from. omni-search ranks each keyword popular-first, so we
# also randomly skip the top page(s) per keyword to keep the giants from
# dominating.
GENRE_SEEDS = [
    "tycoon", "obby", "simulator", "rpg", "horror", "racing", "fighting",
    "tower", "pet", "survival", "roleplay", "adventure", "shooter", "parkour",
    "puzzle", "sports", "tower defense", "clicker", "anime", "zombie",
    "pirate", "dragon", "magic", "superhero", "mystery", "fantasy", "sandbox",
    "escape", "dance", "farm",
]


def load_word_pool():
    """Load the bundled common-English-word list used as random keywords."""
    try:
        return [w.strip() for w in KEYWORDS_FILE.read_text().split() if w.strip()]
    except FileNotFoundError:
        print(f"  WARN: {KEYWORDS_FILE} missing - falling back to genre seeds only")
        return []


def week_seed():
    """Deterministic-per-ISO-week seed so re-runs in the same week match,
    but each new week reshuffles to a fresh batch."""
    iso = datetime.date.today().isocalendar()
    return iso[0] * 100 + iso[1]


def omni_search(query, page_token=None, retries=4):
    params = {"searchQuery": query, "sessionId": SESSION_ID, "pageType": "Game"}
    if page_token:
        params["pageToken"] = page_token
    for attempt in range(retries):
        try:
            r = SESSION.get(OMNI_URL, params=params, timeout=20)
        except Exception as e:
            print(f"    omni '{query}' error: {e}")
            time.sleep(2)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            time.sleep(1 + attempt)   # gentle linear backoff: 1,2,3,4s
            continue
        # other errors: brief pause, give up after retries
        time.sleep(0.5)
    return None


def parse_games(payload):
    """Yield raw game dicts from an omni-search payload, plus the next token."""
    out = []
    if not payload:
        return out, None
    for grp in payload.get("searchResults", []):
        if grp.get("contentGroupType") != "Game":
            continue
        for g in grp.get("contents", []):
            if g.get("universeId"):
                out.append(g)
    return out, payload.get("nextPageToken")


def fetch_keyword(kw):
    """Walk one keyword's omni-search pages; return its raw game dicts.

    omni-search ranks each keyword popular-first, so from page 1 (the popular
    head) we randomly keep only PAGE1_KEEP of the results - giants stay
    eligible but diluted, never grabbed wholesale in rank order. Deeper pages
    (the niche tail) are kept in full. Runs in its own thread."""
    out = []
    token = None
    for page in range(PAGES_PER_KW):
        payload = omni_search(kw, token)
        games, token = parse_games(payload)
        if page == 0 and len(games) > 1:
            random.shuffle(games)
            games = games[:max(1, int(len(games) * PAGE1_KEEP))]
        out.extend(games)
        if not token:
            break
    return out


def discover():
    """Sweep keywords through omni-search CONCURRENTLY; return {uid: raw_game}.

    Keyword list = curated genre seeds + a random sample of common words,
    shuffled with the per-week seed so each run searches a different mix. Each
    keyword's full pagination chain runs as one worker across a thread pool."""
    pool = load_word_pool()
    sample = random.sample(pool, min(SAMPLE_KEYWORDS, len(pool))) if pool else []
    keywords = GENRE_SEEDS + sample
    random.shuffle(keywords)
    keywords = keywords[:KEYWORDS_PER_RUN]

    found = {}
    print(f"[1/3] omni-search sweep: {len(keywords)} keywords "
          f"x{WORKERS} workers, target {TARGET_GAMES:,}...")
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(fetch_keyword, kw): kw for kw in keywords}
        for fut in as_completed(futures):
            done += 1
            try:
                games = fut.result()
            except Exception as e:
                print(f"  '{futures[fut]}' failed: {e}")
                continue
            for g in games:
                found[g["universeId"]] = g
            if done % 20 == 0:
                print(f"  swept {done}/{len(keywords)} keywords  pool={len(found):,}")
    print(f"  discovery complete: {len(found):,} unique games")
    return found


def fetch_meta_batch(batch):
    """Fetch canonical metadata for up to 50 universe IDs. Runs in a thread."""
    for attempt in range(4):
        try:
            r = SESSION.get(GAMES_URL,
                            params={"universeIds": ",".join(map(str, batch))},
                            timeout=20)
        except Exception:
            time.sleep(2)
            continue
        if r.status_code == 200:
            return r.json().get("data", [])
        if r.status_code == 429:
            time.sleep(1 + attempt)   # gentle linear backoff: 1,2,3,4s
            continue
        break
    return []


def enrich(universe_ids):
    """Concurrently batch-fetch canonical metadata (visits, genre, maxPlayers).
    Best effort: universes that fail simply fall back to omni-search data."""
    meta = {}
    ids = list(universe_ids)
    batches = [ids[i:i + 50] for i in range(0, len(ids), 50)]
    print(f"[2/3] enriching {len(ids):,} games via /v1/games "
          f"({len(batches)} batches x{WORKERS} workers)...")
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for data in ex.map(fetch_meta_batch, batches):
            for e in data:
                meta[e.get("id")] = e
    print(f"  enrichment complete: {len(meta):,} resolved")
    return meta


def omni_ok(g):
    """Cheap quality gate using only omni-search fields (no enrich needed)."""
    if not g.get("rootPlaceId"):
        return False
    if (g.get("minimumAge") or 0) > MAX_MIN_AGE:
        return False
    if (g.get("playerCount", 0) or 0) < MIN_PLAYERS:
        return False
    if (g.get("totalUpVotes", 0) or 0) < MIN_UPVOTES:
        return False
    return bool((g.get("name") or "").strip())


def select(found):
    """Filter on omni fields and cap to MAX_POOL BEFORE the costly enrich step,
    so we only fetch metadata for games that will actually make the pool."""
    cands = [g for g in found.values() if omni_ok(g)]
    random.shuffle(cands)
    capped = cands[:MAX_POOL]
    print(f"  {len(cands):,} pass omni filters -> keeping {len(capped):,} "
          f"(capped to MAX_POOL)")
    return capped


def build(candidates, meta):
    print("[3/3] assembling pool...")
    games = []
    dropped_popular = 0
    for g in candidates:
        uid = g["universeId"]
        m = meta.get(uid, {})
        if MAX_VISITS and (m.get("visits", 0) or 0) > MAX_VISITS:
            dropped_popular += 1
            continue
        games.append({
            "title":       (g.get("name") or m.get("name") or "").strip(),
            "universeId":  uid,
            "rootPlaceId": g["rootPlaceId"],
            "visits":      m.get("visits", 0) or 0,
            "genre":       m.get("genre") or "All",
            "maxPlayers":  m.get("maxPlayers", 0) or 0,
            "players":     g.get("playerCount", 0) or 0,
            "upVotes":     g.get("totalUpVotes", 0) or 0,
            "downVotes":   g.get("totalDownVotes", 0) or 0,
        })
    random.shuffle(games)
    print(f"  final pool: {len(games):,}  (popular-capped {dropped_popular})")
    return games


def main():
    print("=== Roulette Random - Game Pool Harvester (omni-search) ===")
    random.seed(week_seed())
    print(f"week seed: {week_seed()}")

    found = discover()
    if not found:
        print("ERROR: discovery returned nothing - API may have changed")
        sys.exit(1)

    candidates = select(found)
    meta = enrich([g["universeId"] for g in candidates]) if ENRICH else {}
    if not ENRICH:
        print("[2/3] enrichment disabled (ENRICH=False) - using omni data only")
    games = build(candidates, meta)

    if len(games) < 100:
        print(f"ERROR: only {len(games)} games - aborting to avoid a thin pool")
        sys.exit(1)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(games, f, separators=(",", ":"), ensure_ascii=False)

    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"\nWROTE {len(games):,} games -> {OUTPUT_PATH}  ({size_kb:.1f} KB)")
    print("Done.")


# one session id reused for the whole run (omni-search expects a stable id)
SESSION_ID = str(uuid.uuid4())

if __name__ == "__main__":
    main()
