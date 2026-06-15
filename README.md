# Roulette Random — Game Pool Backend

Weekly harvester that builds a curated pool of ~10,000 public Roblox experiences.
Used by the Roulette Random Roblox experience to power its random-game picker.

## How it works

A GitHub Actions workflow runs every Monday at 3am UTC:
1. Sweeps Roblox's discovery and search APIs across multiple genres and sort orders
2. Batch-fetches metadata (title, visits, genre, maxPlayers, rootPlaceId)
3. Filters out games with fewer than 1,000 visits
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
  "maxPlayers": 20
}
```
