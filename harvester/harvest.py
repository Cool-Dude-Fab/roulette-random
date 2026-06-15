#!/usr/bin/env python3
"""
Roulette Random — Game Pool Harvester
Runs weekly via GitHub Actions.
Collects ~10,000 Roblox games across visit tiers and genres, writes data/games.json.
"""

import json
import random
import sys
import time
from pathlib import Path

import requests

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; RouletteRandomHarvester/1.0)",
    "Accept": "application/json",
})

OUTPUT_PATH = Path(__file__).parent.parent / "data" / "games.json"
TARGET_COUNT = 10_000
MIN_VISITS = 1_000

GENRES = [
    "All", "Adventure", "Tutorial", "Funny", "Horror",
    "Town and City", "Fantasy", "War", "Comedy", "Medieval",
    "Building", "Sci-Fi", "FPS", "RPG", "Sports",
    "Fighting", "Western", "Naval",
]

SORT_TOKENS = ["", "1", "2", "3", "4", "5", "6"]

KEYWORDS = [
    "simulator", "tycoon", "obby", "horror", "roleplay", "fighting",
    "adventure", "survival", "zombie", "escape", "tower", "battle",
    "racing", "anime", "city", "war", "pirate", "dragon", "ninja",
    "sword", "gun", "magic", "farm", "pet", "defense", "puzzle",
    "parkour", "shooting", "mining", "building", "story", "cops",
    "military", "cooking", "hospital", "school", "space", "underwater",
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


def collect_from_list(sort_token="", genre="All", max_pages=5):
    ids = set()
    for page in range(max_pages):
        data = fetch_json("https://games.roblox.com/v1/games/list", params={
            "model.sortToken": sort_token,
            "model.gameSetTypeId": 0,
            "model.startRowIndex": page * 100,
            "model.maxRows": 100,
            "model.browserFilter": "false",
            "model.genreFilter": genre,
        })
        if not data or not data.get("games"):
            break
        for g in data["games"]:
            uid = g.get("universeId")
            if uid:
                ids.add(uid)
        time.sleep(0.5)
    return ids


def collect_from_search(keyword, max_pages=3):
    ids = set()
    for page in range(max_pages):
        data = fetch_json("https://games.roblox.com/v1/games/list", params={
            "model.keyword": keyword,
            "model.startRowIndex": page * 100,
            "model.maxRows": 100,
            "model.browserFilter": "false",
        })
        if not data or not data.get("games"):
            break
        for g in data["games"]:
            uid = g.get("universeId")
            if uid:
                ids.add(uid)
        time.sleep(0.5)
    return ids


def fetch_metadata(universe_ids):
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
    all_ids = set()

    # Phase 1: sort/genre sweep (catches popular + genre-specific games)
    print("\n[1/3] Sort + genre sweep...")
    for token in SORT_TOKENS:
        for genre in GENRES:
            ids = collect_from_list(token, genre, max_pages=5)
            all_ids.update(ids)
            print(f"  sort={token!r:3s} genre={genre:<18s}: +{len(ids):4d}  total={len(all_ids)}")
            if len(all_ids) >= TARGET_COUNT * 2:
                break
        if len(all_ids) >= TARGET_COUNT * 2:
            break

    # Phase 2: keyword search (catches hidden gems + niche titles)
    print("\n[2/3] Keyword search sweep...")
    random.shuffle(KEYWORDS)
    for kw in KEYWORDS:
        if len(all_ids) >= TARGET_COUNT * 3:
            break
        ids = collect_from_search(kw, max_pages=3)
        all_ids.update(ids)
        print(f"  keyword={kw!r:<14s}: +{len(ids):4d}  total={len(all_ids)}")

    print(f"\nUnique universe IDs collected: {len(all_ids)}")

    # Phase 3: metadata fetch + filter
    print("\n[3/3] Fetching metadata...")
    raw = fetch_metadata(all_ids)

    games = []
    for entry in raw:
        uid = entry.get("id")
        pid = entry.get("rootPlaceId")
        title = (entry.get("name") or "").strip()
        visits = entry.get("visits", 0)

        if not uid or not pid or not title:
            continue
        if visits < MIN_VISITS:
            continue

        games.append({
            "title": title,
            "universeId": uid,
            "rootPlaceId": pid,
            "visits": visits,
            "genre": entry.get("genre") or "All",
            "maxPlayers": entry.get("maxPlayers") or 0,
        })

    # Build a balanced pool: top half by visits, bottom half random (variety floor)
    games.sort(key=lambda g: g["visits"], reverse=True)
    if len(games) > TARGET_COUNT:
        top = games[: TARGET_COUNT // 2]
        rest = games[TARGET_COUNT // 2 :]
        random.shuffle(rest)
        games = top + rest[: TARGET_COUNT // 2]

    random.shuffle(games)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(games, f, separators=(",", ":"))

    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"\nPool: {len(games)} games  |  {size_kb:.1f} KB  →  {OUTPUT_PATH}")

    if len(games) < 50:
        print("ERROR: Too few games collected — check API endpoints")
        sys.exit(1)

    print("Done!")


if __name__ == "__main__":
    main()
