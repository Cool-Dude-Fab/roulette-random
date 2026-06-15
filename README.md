# Roulette Random — Game Pool Backend

Weekly harvester that builds a pool of several thousand public Roblox
experiences. Used by the Roulette Random Roblox experience to power its
random-game picker.

## How it works

Roblox's keyword search (`omni-search`) — the only API that surfaces deep,
niche games — is blocked from datacenter IPs, so the work is split across two
scripts:

### `harvester/harvest.py` — niche discovery (run from home)

Sweeps a random sample of keywords through `omni-search`, which returns real,
played experiences with live players + votes inline. Keywords are drawn from a
bundled ~8.7k common-word list and shuffled with a per-ISO-week seed; from each
keyword's most-popular page only a random half is kept, so giant games stay
eligible but don't dominate. Filters maturity + a light community-signal floor,
dedupes, caps, writes `data/games.json`. Runs concurrently in ~1.5 min.

This is the only step that needs a non-datacenter IP. Run it from your machine
whenever you want to inject fresh *niche* games into the pool:

```bash
pip install requests
python harvester/harvest.py
git commit -am "refresh niche pool" && git push
```

### `harvester/refresh.py` — weekly upkeep (runs in GitHub Actions)

A workflow runs every Monday at 3am UTC and, using only cloud-friendly APIs:
1. Re-validates every game in the pool via `/v1/games` — drops removed/private
   games (only when the API *confirms* they're gone) and refreshes live
   `playing` count, `visits`, `genre`, and `maxPlayers`.
2. Folds in Roblox's current `explore-api` trending / up-and-coming games.
3. Caps, shuffles, commits `data/games.json`.

Keeps the pool alive and current with zero manual effort. To run it on demand:
**Actions** tab → **Weekly Pool Refresh** → **Run workflow**.

The Roblox experience fetches `data/games.json` at server startup via HttpService
and picks randomly from it on every spin.

## JSON format

Each entry in `data/games.json`:
```json
{
  "title": "Game Name",
  "universeId": 12345,
  "rootPlaceId": 67890,
  "visits": 1000000,
  "genre": "Adventure",
  "maxPlayers": 20,
  "players": 1500,
  "upVotes": 90000,
  "downVotes": 4000
}
```

`players` (live count), `upVotes`, and `downVotes` feed the spin UI and the
planned community-rating ("Roulette Respected") features. `visits`/`genre`/
`maxPlayers` are populated/refreshed by `refresh.py` via `/v1/games`.
