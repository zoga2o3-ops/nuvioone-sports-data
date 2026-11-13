# nuvio-sports-data

Shared ESPN scoreboard cache for the Nuvio One apps.

A GitHub Action asks ESPN's public, unauthenticated scoreboard endpoint for every
league in `espn-leagues.txt`, one day at a time, and publishes the raw answers on
the `data` branch:

```
https://raw.githubusercontent.com/zoga2o3-ops/nuvio-sports-data/data/espn/<league>.json
```

Each file:

```json
{
  "source": "ESPN (site.api.espn.com), unofficial",
  "id": "football/nfl",
  "days": {
    "20261112": {
      "fetchedAt": 1794601571198,
      "raw": "<ESPN scoreboard JSON>",
      "liveOrSoon": false
    }
  }
}
```

Only yesterday/today/tomorrow is cached in phase 1. Today refreshes most often,
the edge days less often, and a game that's live or starting soon every 90
seconds. The app asks ESPN directly for the week-ahead schedule, and reads these
files first for the days it covers.

`espn-leagues.txt` mirrors the leagues the app fetches through ESPN's plain
scoreboard endpoint. Leagues the app reads through HockeyTech, the MLB Stats API
or ESPN's cricket panel are left out on purpose.

ESPN's scoreboard is unofficial and unauthenticated; this repository is not
affiliated with or endorsed by ESPN. Sports data: ESPN.
