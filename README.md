# Roulette Random — Game Pool Backend

Weekly harvester that builds a pool of several thousand public Roblox
experiences. Used by the Roulette Random Roblox experience to power its
random-game picker.

## How it works

A GitHub Actions workflow runs every Monday at 3am UTC:
1. Sweeps a random sample of keywords through Roblox's `omni-search` API,
   which returns real, played experiences (universeId, name, rootPlaceId,
   live player count, votes, maturity) with everything we need inline. The
   keywords are drawn from a bundled ~8.7k common-word list and shuffled with
   a per-ISO-week seed, so every run surfaces a fresh random batch. From each
   keyword's first (most-popular) page we keep only a random half, so giant
   games stay eligible but don't dominate.
2. Filters by maturity and a light community-signal floor, dedupes, shuffles,
   caps to the pool size. All requests run concurrently across a thread pool.
3. Commits the result to `data/games.json`

Discovery is intentionally the only network phase: omni-search already returns
live players + votes, which are better freshness signals than lifetime visits.
An optional enrichment pass (`ENRICH=True` in `harvest.py`) can backfill
canonical `visits`/`genre`/`maxPlayers` from the `/v1/games` endpoint, but it's
off by default because that endpoint is heavily rate-limited and Roblox's genre
field is mostly empty.

The Roblox experience fetches `data/games.json` at server startup via HttpService.

## Running manually

To trigger a fresh harvest without waiting for the schedule:
1. Go to **Actions** tab on GitHub
2. Click **Harvest Game Pool**
3. Click **Run workflow**

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

`players`, `upVotes`, and `downVotes` come from omni-search and feed the
planned community-rating ("Roulette Respected") features.
