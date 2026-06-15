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
from pathlib import Path

import requests

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "games.json"

TARGET_GAMES   = 6000   # stop discovery once we have this many unique games
MAX_POOL       = 6000   # final pool size cap
SAMPLE_KEYWORDS = 350   # random words drawn from the big list per run
PAGES_PER_KW   = 4      # omni-search result pages to collect per keyword
PAGE1_KEEP     = 0.5    # of page 1 (the popular head), randomly keep this fraction
                        # -> giants stay eligible but diluted, not taken wholesale.
                        # deeper (niche) pages are always kept in full.
MIN_PLAYERS    = 0      # live-player floor (0 = allow quiet games for variety)
MIN_UPVOTES    = 30     # require a little community signal so games aren't dead
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
            wait = 2 ** (attempt + 1)
            time.sleep(wait)
            continue
        # other errors: brief pause, give up after retries
        time.sleep(1)
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


def discover():
    """Sweep keywords through omni-search; return {universeId: raw_game}.

    Keyword list = curated genre seeds + a random sample of common words,
    shuffled with the per-week seed so each run searches a different mix.
    omni-search ranks each keyword popular-first, so from page 1 (the popular
    head) we randomly keep only PAGE1_KEEP of the results - giants stay
    eligible but diluted, never grabbed wholesale in rank order. Deeper pages
    (the niche tail) are kept in full."""
    found = {}
    pool = load_word_pool()
    sample = random.sample(pool, min(SAMPLE_KEYWORDS, len(pool))) if pool else []
    keywords = GENRE_SEEDS + sample
    random.shuffle(keywords)
    print(f"[1/3] omni-search sweep: {len(GENRE_SEEDS)} genre seeds + "
          f"{len(sample)} random words, target {TARGET_GAMES:,}...")
    for kw in keywords:
        if len(found) >= TARGET_GAMES:
            break
        token = None
        added = 0
        for page in range(PAGES_PER_KW):
            payload = omni_search(kw, token)
            games, token = parse_games(payload)
            if page == 0 and len(games) > 1:
                # randomly sample the popular head instead of taking it in order
                random.shuffle(games)
                games = games[:max(1, int(len(games) * PAGE1_KEEP))]
            for g in games:
                uid = g["universeId"]
                if uid not in found:
                    found[uid] = g
                    added += 1
            if not token:
                break
            time.sleep(0.5)
        print(f"  '{kw}': +{added}  (pool={len(found)})")
        time.sleep(0.4)
    print(f"  discovery complete: {len(found):,} unique games")
    return found


def enrich(universe_ids):
    """Batch-fetch canonical metadata (visits, genre, maxPlayers). Best effort:
    universes that fail simply fall back to omni-search data."""
    meta = {}
    ids = list(universe_ids)
    print(f"[2/3] enriching {len(ids):,} games via /v1/games (batches of 50)...")
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        for attempt in range(4):
            try:
                r = SESSION.get(GAMES_URL,
                                params={"universeIds": ",".join(map(str, batch))},
                                timeout=20)
            except Exception:
                time.sleep(2)
                continue
            if r.status_code == 200:
                for e in r.json().get("data", []):
                    meta[e.get("id")] = e
                break
            if r.status_code == 429:
                time.sleep(2 ** (attempt + 1))
                continue
            break
        if (i // 50) % 20 == 0:
            print(f"  enriched ~{min(i + 50, len(ids)):,}/{len(ids):,}")
        time.sleep(0.4)
    print(f"  enrichment complete: {len(meta):,} resolved")
    return meta


def build(found, meta):
    print("[3/3] filtering + assembling pool...")
    games = []
    dropped_age = dropped_quality = no_place = dropped_popular = 0
    for uid, g in found.items():
        place = g.get("rootPlaceId")
        if not place:
            no_place += 1
            continue
        if (g.get("minimumAge") or 0) > MAX_MIN_AGE:
            dropped_age += 1
            continue
        up = g.get("totalUpVotes", 0) or 0
        players = g.get("playerCount", 0) or 0
        if players < MIN_PLAYERS or up < MIN_UPVOTES:
            dropped_quality += 1
            continue

        m = meta.get(uid, {})
        if MAX_VISITS and (m.get("visits", 0) or 0) > MAX_VISITS:
            dropped_popular += 1
            continue
        games.append({
            "title":       (g.get("name") or m.get("name") or "").strip(),
            "universeId":  uid,
            "rootPlaceId": place,
            "visits":      m.get("visits", 0) or 0,
            "genre":       m.get("genre") or "All",
            "maxPlayers":  m.get("maxPlayers", 0) or 0,
            "players":     players,
            "upVotes":     up,
            "downVotes":   g.get("totalDownVotes", 0) or 0,
        })

    games = [g for g in games if g["title"]]
    random.shuffle(games)
    if len(games) > MAX_POOL:
        games = games[:MAX_POOL]

    print(f"  kept {len(games):,}  |  dropped: age={dropped_age} "
          f"quality={dropped_quality} no_place={no_place} popular={dropped_popular}")
    return games


def main():
    print("=== Roulette Random - Game Pool Harvester (omni-search) ===")
    random.seed(week_seed())
    print(f"week seed: {week_seed()}")

    found = discover()
    if not found:
        print("ERROR: discovery returned nothing - API may have changed")
        sys.exit(1)

    meta = enrich(found.keys())
    games = build(found, meta)

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
