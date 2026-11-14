# nuvioone-sports-data

Shared sports data for the Nuvio One apps. A GitHub Action publishes everything on
the `data` branch:

```
https://raw.githubusercontent.com/zoga2o3-ops/nuvioone-sports-data/data/
```

## ESPN scoreboard cache

`scripts/espn_cache.py` asks ESPN's public, unauthenticated scoreboard endpoint for
every league in `espn-leagues.txt`, one day at a time, and writes
`espn/<league>.json`:

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

Only yesterday/today/tomorrow is cached. Today refreshes most often, the edge days
less often, and a game that's live or starting soon every 90 seconds. The app asks
ESPN directly for the week-ahead schedule, and reads these files first for the days
they cover.

`espn-leagues.txt` mirrors the leagues the app fetches through ESPN's plain
scoreboard endpoint. Leagues the app reads through HockeyTech, the MLB Stats API or
ESPN's cricket panel are left out on purpose.

## Extra football leagues

`scripts/fotmob_cache.py` fills the leagues ESPN does not carry (Poland, Croatia,
Serbia, Ukraine, Hungary, the Gulf, much of Africa and Asia, and so on) from
FotMob's public league pages. It writes the same shapes the app's TheSportsDB
reader already understands:

- `sportsdb/index.json` — each league with its teams, badge, guide words and
  region.
- `sportsdb/<fotmob id>.json` — the next six weeks and the last two weeks of
  matches, plus the league table.
- `sportsdb/games/<match id>.json` — goals, team stats and line-ups for matches
  from the last seven days (older files are removed).

A slow pass reads each league page every six hours. The every-five-minute cache
run asks FotMob for today's matches in one request and updates the scores in the
existing files, so the extra leagues stay live without re-reading every page.
`fotmob-leagues.txt` is the list of leagues; add a line to include one.

## Wrestling and boxing schedule

`scripts/wrestling_cache.py` reads the public WWE events page, the public AEW
events page, TNA's events page and Box.live's schedule table, then writes
`wrestling.json`:

```json
{
  "source": "WWE.com, AEW, TNA and Box.live, unofficial",
  "generatedAt": 1794608559065,
  "leagues": [
    {"key": "wwe", "badge": "...", "brands": [{"word": "raw", "badge": "..."}]},
    {"key": "aew", "badge": "...", "brands": [{"word": "dynamite", "badge": "..."}]}
  ],
  "events": [
    {
      "id": "wwe-friday-night-smackdown-91",
      "name": "Friday Night SmackDown",
      "league": "wwe",
      "start": "2026-11-20T23:30:00Z",
      "hasTime": true,
      "venue": "Ford Center Evansville",
      "city": "Evansville",
      "country": "United States"
    }
  ]
}
```

TV shows and pay-per-views are included; house-show tours are left out because they
are not televised. WWE times come from the event pages (UTC); AEW times are built
from the promotion's own local start time and time-zone abbreviation. TNA and
boxing dates are added without a time because their public pages only give a
local date. The file is rebuilt when it is more than five hours old, so the
every-five-minute cache job does not re-fetch it every run.

## Notes

- No API keys. ESPN, FotMob, WWE, AEW, TNA and Box.live are fetched from their
  public pages; this repository is not affiliated with or endorsed by any of them.
- Sports data: ESPN and FotMob. Wrestling schedule: WWE.com, All Elite Wrestling
  and TNA. Boxing schedule: Box.live.
- The app falls back to asking ESPN directly when a cache file is missing or stale.
