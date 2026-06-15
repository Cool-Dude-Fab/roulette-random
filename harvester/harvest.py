#!/usr/bin/env python3
"""
Roulette Random — Game Pool Harvester
Runs weekly via GitHub Actions.
Discovers games via Roblox's recommendations API (BFS from diverse seeds),
then batch-fetches metadata. Writes data/games.json.
"""

import json
import random
import sys
import time
from collections import deque
from pathlib import Path

import requests

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; RouletteRandomHarvester/1.0)",
    "Accept": "application/json",
})

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "games.json"
TARGET_IDS  = 15_000   # collect this many IDs before filtering
TARGET_GAMES = 10_000  # final pool size after metadata filter
MIN_VISITS  = 1_000

# Diverse seed universe IDs spanning genres and popularity tiers
SEEDS = [
    223462836,   # Adopt Me!
    2753915549,  # Blox Fruits
    1537690962,  # Brookhaven RP
    1060804,     # Tower of Hell
    142823291,   # Murder Mystery 2
    189106,      # Natural Disaster Survival
    606849621,   # Jailbreak
    10359919,    # MeepCity
    192800,      # Work at a Pizza Place
    735030788,   # Royale High
    4680386299,  # Piggy
    286090429,   # Arsenal
    1730984664,  # Shindo Life
    1962086868,  # Doors
    1599679858,  # Pet Simulator X
    4922741943,  # Islands
    16732694052, # Fisch
    234247886,   # Car Crushers 2
    1536954679,  # Obby But You're on a Bike
    2518745239,  # Anime Fighting Simulator X
    5851073373,  # Sonic Speed Simulator
    3959661290,  # Dragon Adventures
    5120535452,  # Fruit Battlegrounds
    3260590327,  # Bedwars
    1705128720,  # Rainbow Friends
    3233893879,  # Funky Friday
    4483381587,  # Wacky Wizards
    5550087938,  # Squid Game (popular fan game)
    7415484311,  # The Mimic
]

# Intentionally diverse low-tier seeds to reach hidden gems via recommendations
LOW_TIER_SEEDS = [
    286090429,   # Arsenal (mid-tier shooter)
    234247886,   # Car Crushers 2 (sandbox)
    4922741943,  # Islands (building/survival)
    189106,      # Natural Disaster Survival (classic)
    192800,      # Work at a Pizza Place (classic)
]


def fetch_json(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                wait = 2 ** (attempt + 2)
                print(f"  Rate limited — waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"  HTTP {resp.status_code} for {url}")
                return None
        except Exception as e:
            print(f"  Request error: {e}")
            time.sleep(2)
    return None


def get_recommendations(universe_id, max_rows=50):
    """Fetch recommendation universe IDs for a given game."""
    data = fetch_json(
        "https://games.roblox.com/v1/games/recommendations/algorithm/2",
        params={"maxRows": max_rows, "targetUniverseId": universe_id},
    )
    if not data or not data.get("games"):
        return []
    return [g["universeId"] for g in data["games"] if g.get("universeId")]


def get_sorts_games():
    """Pull game IDs from Roblox home-page sort lists."""
    ids = set()
    data = fetch_json(
        "https://games.roblox.com/v1/games/sorts",
        params={"gameSortsContext": "GamesDefaultSorts"},
    )
    if not data:
        return ids
    sorts = data.get("sorts") or []
    for sort in sorts:
        token = sort.get("token")
        if not token:
            continue
        games = sort.get("games") or []
        for g in games:
            uid = g.get("universeId")
            if uid:
                ids.add(uid)
    return ids


def fetch_metadata(universe_ids):
    """Batch-fetch game metadata for up to 100 IDs per request."""
    results = []
    ids_list = list(universe_ids)
    for i in range(0, len(ids_list), 100):
        batch = ids_list[i : i + 100]
        data = fetch_json(
            "https://games.roblox.com/v1/games",
            params={"universeIds": ",".join(str(x) for x in batch)},
        )
        if data and data.get("data"):
            results.extend(data["data"])
        time.sleep(0.4)
    return results


def main():
    print("=== Roulette Random — Game Pool Harvester ===")

    # Clean seed list (remove any bad entries)
    seeds = [s for s in SEEDS if isinstance(s, int)]

    all_ids = set(seeds)
    queue = deque(seeds)
    visited = set()

    # Phase 1: home-page sorts (instant, no BFS cost)
    print("\n[1/3] Pulling home-page sort lists...")
    sort_ids = get_sorts_games()
    all_ids.update(sort_ids)
    # Add sort games as BFS seeds too
    queue.extend(s for s in sort_ids if s not in visited)
    print(f"  Got {len(sort_ids)} IDs from sorts  |  total={len(all_ids)}")

    # Phase 2: BFS recommendation chain
    print(f"\n[2/3] BFS recommendation chain (target: {TARGET_IDS:,} IDs)...")
    while queue and len(all_ids) < TARGET_IDS:
        uid = queue.popleft()
        if uid in visited:
            continue
        visited.add(uid)

        recs = get_recommendations(uid, max_rows=50)
        new = [r for r in recs if r not in all_ids]
        all_ids.update(recs)
        for r in new:
            queue.append(r)

        if len(visited) % 25 == 0:
            print(f"  Visited={len(visited):5d}  Pool={len(all_ids):6d}  Queue={len(queue):6d}")

        time.sleep(0.35)

    print(f"\nTotal unique IDs collected: {len(all_ids)}")

    # Phase 3: batch metadata + filter
    print(f"\n[3/3] Fetching metadata (min {MIN_VISITS:,} visits)...")
    raw = fetch_metadata(all_ids)

    games = []
    for entry in raw:
        uid   = entry.get("id")
        pid   = entry.get("rootPlaceId")
        title = (entry.get("name") or "").strip()
        visits = entry.get("visits", 0)

        if not uid or not pid or not title:
            continue
        if visits < MIN_VISITS:
            continue

        games.append({
            "title":       title,
            "universeId":  uid,
            "rootPlaceId": pid,
            "visits":      visits,
            "genre":       entry.get("genre") or "All",
            "maxPlayers":  entry.get("maxPlayers") or 0,
        })

    # Balanced pool: top half by visits + random sample from the rest
    games.sort(key=lambda g: g["visits"], reverse=True)
    if len(games) > TARGET_GAMES:
        top  = games[: TARGET_GAMES // 2]
        rest = games[TARGET_GAMES // 2 :]
        random.shuffle(rest)
        games = top + rest[: TARGET_GAMES // 2]

    random.shuffle(games)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(games, f, separators=(",", ":"))

    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"\nFinal pool: {len(games)} games  |  {size_kb:.1f} KB  →  {OUTPUT_PATH}")

    if len(games) < 50:
        print("ERROR: Too few games collected — API may have changed")
        sys.exit(1)

    print("Done!")


if __name__ == "__main__":
    main()
