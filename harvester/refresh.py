#!/usr/bin/env python3
"""
Roulette Random - Weekly Pool Refresh (runs in GitHub Actions).

omni-search (used by harvest.py for niche discovery) is blocked from datacenter
IPs, so the WEEKLY cloud job uses only endpoints that work from the cloud:

  * explore-api  -> Roblox's current trending/up-and-coming games (~700)
  * /v1/games    -> canonical metadata + liveness (unthrottled from the cloud)

Each run:
  1. Loads the existing pool (the niche games harvested from home by harvest.py).
  2. Pulls every explore-api sort and folds in the current trending games.
  3. Re-validates the whole pool through /v1/games: drops removed/dead games and
     refreshes live `playing`, `visits`, `genre`, `maxPlayers`.
  4. Caps, shuffles, writes data/games.json.

Net effect: the big niche pool stays alive and current, and fresh trending games
rotate in every week - all fully automated, no home machine required. Re-running
harvest.py from home (occasionally) is what injects new *niche* games.
"""

import json
import random
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

POOL_PATH   = Path(__file__).parent.parent / "data" / "games.json"
MAX_POOL    = 8000   # pool cap (existing niche pool + trending top-ups)
WORKERS     = 8      # explore-api fetches (light)
VALIDATE_WORKERS = 3 # /v1/games validation: LOW on purpose - it throttles hard
                     # under burst load even from the cloud, but tolerates a
                     # steady low-concurrency stream, which confirms ~everything
MAX_MIN_AGE = 13     # maturity gate for newly-added explore games
MIN_KEEP    = 100    # safety: never overwrite the pool with fewer than this

EXPLORE = "https://apis.roblox.com/explore-api/v1"
GAMES   = "https://games.roblox.com/v1/games"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RouletteRandomRefresh/1.0",
    "Accept": "application/json",
})
SID = str(uuid.uuid4())


def get_explore_games():
    """Every game across every explore-api sort (cloud-friendly discovery)."""
    sort_ids = []
    token = None
    for _ in range(30):
        params = {"sessionId": SID}
        if token:
            params["sortsPageToken"] = token
        try:
            r = SESSION.get(f"{EXPLORE}/get-sorts", params=params, timeout=20)
        except Exception:
            break
        if r.status_code != 200:
            break
        j = r.json()
        for s in j.get("sorts", []):
            if s.get("contentType") == "Games" and s.get("sortId"):
                sort_ids.append(s["sortId"])
        token = j.get("nextSortsPageToken")
        if not token:
            break
        time.sleep(0.3)

    def fetch_sort(sid):
        try:
            r = SESSION.get(f"{EXPLORE}/get-sort-content",
                            params={"sessionId": SID, "sortId": sid}, timeout=20)
            return r.json().get("games", []) if r.status_code == 200 else []
        except Exception:
            return []

    games = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for arr in ex.map(fetch_sort, sort_ids):
            for g in arr:
                uid = g.get("universeId")
                if not uid:
                    continue
                if (g.get("minimumAge") or 0) > MAX_MIN_AGE:
                    continue
                games[uid] = g
    print(f"  explore-api: {len(sort_ids)} sorts -> {len(games)} trending games")
    return games


def fetch_meta_batch(batch):
    """Return (ok, data). ok is True ONLY if a 200 was received - so absent IDs
    in a 200 response can be trusted as genuinely dead, never merely throttled.
    Retries hard, since a falsely-failed batch would wrongly mark games dead."""
    for attempt in range(6):
        try:
            r = SESSION.get(GAMES, params={"universeIds": ",".join(map(str, batch))},
                            timeout=20)
        except Exception:
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 200:
            return True, r.json().get("data", [])
        if r.status_code == 429:
            time.sleep(1.5 * (attempt + 1))
            continue
        break
    return False, []


def validate(uids):
    """Batch every universe ID through /v1/games.

    Returns (meta, queried): `meta` maps uid->metadata for games confirmed live;
    `queried` is the set of uids whose batch actually returned 200. A uid that is
    in `queried` but NOT in `meta` is genuinely dead. A uid NOT in `queried` was
    only throttled out - we must keep it, not drop it."""
    ids = list(uids)
    batches = [ids[i:i + 50] for i in range(0, len(ids), 50)]
    meta = {}
    queried = set()
    with ThreadPoolExecutor(max_workers=VALIDATE_WORKERS) as ex:
        for batch, (ok, arr) in ex.map(lambda b: (b, fetch_meta_batch(b)), batches):
            if not ok:
                continue
            queried.update(batch)
            for e in arr:
                if e.get("id"):
                    meta[e["id"]] = e
    return meta, queried


def main():
    print("=== Roulette Random - Weekly Refresh (explore-api + /v1/games) ===")

    existing = {}
    if POOL_PATH.exists():
        try:
            for rec in json.load(open(POOL_PATH, encoding="utf-8")):
                existing[rec["universeId"]] = rec
        except Exception as e:
            print(f"  WARN: could not read existing pool: {e}")
    print(f"loaded {len(existing):,} existing games")

    explore = get_explore_games()

    all_uids = set(existing) | set(explore)
    print(f"validating {len(all_uids):,} games via /v1/games...")
    meta, queried = validate(all_uids)
    confirmed_dead = queried - set(meta)        # in a 200 response but absent => gone
    unconfirmed = all_uids - queried            # only throttled => keep, don't drop
    print(f"  {len(meta):,} live, {len(confirmed_dead):,} confirmed dead, "
          f"{len(unconfirmed):,} unconfirmed (kept)")

    # Build a record for every uid that is NOT confirmed dead, preferring fresh
    # /v1/games metadata, then explore data, then the prior stored record.
    keep = (set(meta) | unconfirmed) - confirmed_dead
    pool = []
    for uid in keep:
        m = meta.get(uid, {})
        exp = explore.get(uid, {})
        base = existing.get(uid, {})
        place = m.get("rootPlaceId") or exp.get("rootPlaceId") or base.get("rootPlaceId")
        title = (m.get("name") or exp.get("name") or base.get("title") or "").strip()
        if not place or not title:
            continue
        pool.append({
            "title":       title,
            "universeId":  uid,
            "rootPlaceId": place,
            "visits":      m.get("visits", base.get("visits", 0)) or 0,
            "genre":       m.get("genre_l1") or m.get("genre") or base.get("genre") or "All",
            "maxPlayers":  m.get("maxPlayers", base.get("maxPlayers", 0)) or 0,
            "players":     m.get("playing", exp.get("playerCount", base.get("players", 0))) or 0,
            "upVotes":     exp.get("totalUpVotes", base.get("upVotes", 0)) or 0,
            "downVotes":   exp.get("totalDownVotes", base.get("downVotes", 0)) or 0,
        })

    random.shuffle(pool)
    if len(pool) > MAX_POOL:
        pool = pool[:MAX_POOL]

    added = len([u for u in explore if u not in existing])
    print(f"  pool: {len(pool):,}  (+{added} new from trending, "
          f"{len(confirmed_dead)} dead removed)")

    if len(pool) < MIN_KEEP:
        print(f"ERROR: only {len(pool)} games - refusing to overwrite a healthy pool")
        sys.exit(1)

    with open(POOL_PATH, "w", encoding="utf-8") as f:
        json.dump(pool, f, separators=(",", ":"), ensure_ascii=False)
    size_kb = POOL_PATH.stat().st_size / 1024
    print(f"\nWROTE {len(pool):,} games -> {POOL_PATH}  ({size_kb:.1f} KB)")
    print("Done.")


if __name__ == "__main__":
    main()
