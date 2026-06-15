# Roulette Random — Game Pool Backend

Weekly harvester that builds a pool of several thousand public Roblox
experiences. Used by the Roulette Random Roblox experience to power its
random-game picker.

## How it works

A GitHub Actions workflow runs every Monday at 3am UTC:
1. Sweeps a large keyword list through Roblox's `omni-search` API, which
   returns real, played experiences (universeId, name, rootPlaceId, live
   player count, votes, maturity) with metadata inline. The keyword list is
   shuffled with a per-ISO-week seed, so every run surfaces a fresh random
   batch of games.
2. Enriches each game with canonical metadata (visits, genre, maxPlayers)
   via the public `/v1/games` batch endpoint.
3. Filters by maturity and a light community-signal floor, dedupes, shuffles.
4. Commits the result to `data/games.json`

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
