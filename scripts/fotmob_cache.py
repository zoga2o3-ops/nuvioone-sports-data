#!/usr/bin/env python3
"""Extra football leagues for Nuvio One, from FotMob's public league pages.

The app's ESPN cache covers its own leagues. This fills the rest: leagues ESPN
does not carry (Poland, Croatia, Serbia, Ukraine, Japan's J2, Korea, the Gulf,
Africa, South America's smaller divisions and so on). It writes the same shapes
the app already reads for its TheSportsDB files:

    sportsdb/index.json          leagues + teams (Pick Your Teams)
    sportsdb/<fotmob id>.json    near-term events + league table
    sportsdb/games/<match>.json  goals, stats and line-ups (optional)

A slow pass (every six hours) reads each league page once and rebuilds the
index, the table and the schedule. A fast pass (each cache run) asks FotMob for
today's matches in one request and updates the scores in the existing league
files. No API key and only the Python standard library.

The app falls back to its ESPN data when a file is missing, so a failed league
never empties the Sports page.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "https://www.fotmob.com"
API = BASE + "/api/data"
CONFIG = "fotmob-leagues.txt"
DEFAULT_OUT = "sportsdb"
DEFAULT_GAMES_OUT = "sportsdb/games"
DEFAULT_MAX_AGE = 6 * 60 * 60
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

# A league name this generic needs its country in front, otherwise the guide
# would match the first "Premier League" it sees.
GENERIC_NAMES = {
    "premier league", "1. liga", "1. division", "super league", "super liga",
    "national division", "regular season", "championship", "pro league",
    "ligue 1", "liga i", "prva liga", "first professional league",
    "professional football league", "superliga", "stars league", "lpf",
    "liga super", "v-league", "persian gulf", "thai league",
    # Second divisions and cups whose own name is too generic to be a guide word.
    "2. liga", "2. division", "first division", "i liga", "liga ii", "nb ii",
    "challenge league", "leumit league", "ykkosliiga",
    "liga 2", "1. deild", "second professional league", "cup", "fa cup",
    "state cup", "president's cup",
}

_last_request = 0.0


def log(message: str) -> None:
    print(message, flush=True)


def fetch(url: str, gap: float = 0.7, accept: str = "application/json") -> str | None:
    global _last_request
    for attempt in range(3):
        wait = gap - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
        request = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": accept,
            "Accept-Encoding": "identity",
            "Referer": BASE + "/",
        })
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            if error.code == 429 or error.code >= 500:
                retry = error.headers.get("Retry-After")
                time.sleep(int(retry) if retry and retry.isdigit() else 15)
                continue
            log(f"  {url}: HTTP {error.code}")
            return None
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            log(f"  {url}: {type(error).__name__}")
            time.sleep(4)
    return None


def fetch_json(url: str) -> dict | None:
    raw = fetch(url)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def page_json(path: str) -> dict | None:
    raw = fetch(BASE + path, accept="text/html,application/xhtml+xml")
    if raw is None:
        return None
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        raw, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None
    return ((data.get("props") or {}).get("pageProps") or {}) if isinstance(data, dict) else None


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def iso_utc(value: str | None) -> str | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> dt.datetime | None:
    text = (value or "").replace("Z", "+00:00")
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def parse_score(value: str | None) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\s*[-:]\s*(\d+)", value or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def team_logo(team_id) -> str:
    return f"https://images.fotmob.com/image_resources/logo/teamlogo/{team_id}.png"


def league_logo(league_id) -> str:
    return f"https://images.fotmob.com/image_resources/logo/leaguelogo/{league_id}.png"


def short_name(name: str) -> str:
    words = [w for w in re.split(r"[\s./-]+", name) if w]
    if not words:
        return name[:6].upper()
    initials = "".join(w[0] for w in words if w[0].isalnum()).upper()
    if len(initials) >= 3:
        return initials[:4]
    compact = "".join(words)[:6].upper()
    return compact or initials or name[:6].upper()


def regions_for(ccode: str) -> list[str]:
    # Extra leagues are always available: they only reach the page when they have
    # a game on, and the old country gate kept them out of Search and Tables in
    # most regions.
    return ["US", "CA", "UK", "IE", "AU", "OTHER"]


def display_name(name: str, country: str) -> str:
    if name.lower() in GENERIC_NAMES:
        return f"{country} {name}"
    return name


def words_for(name: str, country: str, shown: str) -> list[str]:
    out = [name, shown]
    if country and not name.lower().startswith(country.lower()):
        out.append(f"{country} {name}")
    return list(dict.fromkeys(w for w in out if w))


def table_rows(page: dict, teams: dict[str, dict], forms: dict[str, str]) -> list[dict]:
    tables = page.get("table") or []
    if not tables:
        return []
    data = (tables[0] or {}).get("data") or {}
    table_data = data
    if not ((data.get("table") or {}).get("all")):
        # Some leagues (K League, the Panamanian LPF) use a composite page:
        # the first sub-table is the overall one.
        sub = (data.get("tables") or [{}])[0]
        table_data = sub or data
    legend = table_data.get("legend") or data.get("legend") or []
    note_by_index: dict[int, str] = {}
    for item in legend:
        title = item.get("title")
        for index in item.get("indices") or []:
            note_by_index[int(index)] = title
    rows = []
    for row in ((table_data.get("table") or {}).get("all") or []):
        team_id = row.get("id")
        name = row.get("name")
        if team_id is None or not name:
            continue
        key = str(team_id)
        pos = row.get("idx")
        note = note_by_index.get((int(pos) - 1) if isinstance(pos, int) and pos > 0 else -1)
        rows.append({
            "id": key,
            "name": clean(name),
            "badge": team_logo(team_id),
            "pos": pos,
            "p": row.get("played"),
            "w": row.get("wins"),
            "d": row.get("draws"),
            "l": row.get("losses"),
            "gd": row.get("goalConDiff"),
            "pts": row.get("pts"),
            "form": forms.get(key) or None,
            "note": note,
        })
    rows.sort(key=lambda item: item.get("pos") or 999)
    return rows


class LeagueConfig:
    def __init__(self, raw: str):
        parts = [part.strip() for part in raw.split("|")]
        while len(parts) < 5:
            parts.append("")
        self.id = parts[0]
        self.ccode = parts[1]
        self.name = parts[2]
        self.country = parts[3]
        self.path = parts[4]


def load_config(path: str) -> list[LeagueConfig]:
    out = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            config = LeagueConfig(line)
            if config.id and config.path:
                out.append(config)
    return out


def write_json(path: str, payload: dict) -> bool:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                if handle.read() == text:
                    return False
        except OSError:
            pass
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return True


def load_json(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def config_ids(configs: list[LeagueConfig]) -> set[int]:
    out = set()
    for config in configs:
        try:
            out.add(int(config.id))
        except ValueError:
            pass
    return out


# --------------------------------------------------------------------------
# Slow pass: league page

def team_map(page: dict) -> dict[str, dict]:
    teams: dict[str, dict] = {}

    def add(team_id, name):
        if team_id is None or not name:
            return
        key = str(team_id)
        teams.setdefault(key, {"id": key, "name": clean(name), "badge": team_logo(team_id)})

    tables = page.get("table") or []
    for table in tables:
        data = (table or {}).get("data") or {}
        for row in ((data.get("table") or {}).get("all") or []):
            add(row.get("id"), row.get("name"))
    matches = ((page.get("fixtures") or {}).get("allMatches") or [])
    for match in matches:
        add((match.get("home") or {}).get("id"), (match.get("home") or {}).get("name"))
        add((match.get("away") or {}).get("id"), (match.get("away") or {}).get("name"))
    return teams


def form_map(page: dict) -> dict[str, str]:
    by_team: dict[str, list[tuple[dt.datetime, str]]] = {}
    for match in ((page.get("fixtures") or {}).get("allMatches") or []):
        status = match.get("status") or {}
        if not status.get("finished"):
            continue
        start = parse_iso(status.get("utcTime") or "")
        if start is None:
            continue
        score = parse_score(status.get("scoreStr"))
        if score is None:
            continue
        home = match.get("home") or {}
        away = match.get("away") or {}
        hs, away_score = score
        by_team.setdefault(str(home.get("id")), []).append(
            (start, "W" if hs > away_score else "D" if hs == away_score else "L"))
        by_team.setdefault(str(away.get("id")), []).append(
            (start, "W" if away_score > hs else "D" if away_score == hs else "L"))
    out = {}
    for team_id, results in by_team.items():
        results.sort(key=lambda item: item[0])
        out[team_id] = "".join(result for _, result in results[-5:])
    return out


def event_of(match: dict) -> dict | None:
    status = match.get("status") or {}
    start = iso_utc(status.get("utcTime"))
    if not start:
        return None
    started = bool(status.get("started"))
    finished = bool(status.get("finished"))
    cancelled = bool(status.get("cancelled"))
    score = parse_score(status.get("scoreStr")) if (started or finished) else None
    home = match.get("home") or {}
    away = match.get("away") or {}
    if not home.get("name") or not away.get("name"):
        return None
    state = "off" if cancelled else "post" if finished else "in" if started else "pre"
    reason = (status.get("reason") or {}).get("short") or ""
    status_text = "FT" if finished else "Live" if started else "Postponed" if cancelled else "NS"
    round_value = match.get("roundName") or match.get("round")
    try:
        round_number = int(round_value)
    except (TypeError, ValueError):
        round_number = 0
    event = {
        "id": str(match.get("id")),
        "hasTime": True,
        "start": start,
        "home": clean(home.get("name")),
        "away": clean(away.get("name")),
        "homeId": str(home.get("id") or ""),
        "awayId": str(away.get("id") or ""),
        "state": state,
        "round": round_number,
        "status": reason or status_text,
    }
    if score is not None:
        event["hs"] = str(score[0])
        event["as"] = str(score[1])
    return event


def build_league(config: LeagueConfig, page: dict, now: dt.datetime) -> tuple[dict | None, dict | None]:
    details = page.get("details") or {}
    name = clean(details.get("name") or config.name)
    country = config.country or clean(details.get("country") or "")
    shown = display_name(name, country)
    teams = team_map(page)
    forms = form_map(page)
    table = table_rows(page, teams, forms)
    league_id = str(details.get("id") or config.id)
    index_entry = {
        "id": league_id,
        "name": shown,
        "sport": "FOOTBALL",
        "words": words_for(name, country, shown),
        "regions": regions_for((details.get("country") or config.ccode or "").upper()),
        "short": short_name(shown),
        "kind": "teams",
        "minutes": 115,
        "badge": league_logo(league_id),
        "teams": sorted(teams.values(), key=lambda item: item["name"].lower()),
    }
    lines = []
    start_from = now - dt.timedelta(days=14)
    start_to = now + dt.timedelta(days=45)
    for match in ((page.get("fixtures") or {}).get("allMatches") or []):
        event = event_of(match)
        if event is None:
            continue
        moment = parse_iso(event["start"])
        if moment is None or not (start_from <= moment <= start_to):
            continue
        lines.append(event)
    lines.sort(key=lambda item: item["start"])
    league = {
        "source": "FotMob (fotmob.com), unofficial",
        "id": league_id,
        "season": details.get("selectedSeason") or "",
        "generatedAt": int(now.timestamp() * 1000),
        "events": lines,
        "table": table,
    }
    return index_entry, league


def slow_pass(configs: list[LeagueConfig], output: str, now: dt.datetime) -> dict:
    entries = []
    ok = 0
    for config in configs:
        log(f"League {config.name} ({config.id})")
        page = page_json(config.path)
        if page is None:
            log("  page failed; keeping the old file")
            old = load_json(os.path.join(output, config.id + ".json"))
            if old:
                ok += 1
            continue
        entry, league = build_league(config, page, now)
        if entry is None or league is None:
            continue
        entries.append(entry)
        write_json(os.path.join(output, config.id + ".json"), league)
        ok += 1
    index = {
        "source": "FotMob (fotmob.com), unofficial",
        "generatedAt": int(now.timestamp() * 1000),
        "leagues": entries,
    }
    write_json(os.path.join(output, "index.json"), index)
    log(f"Built {len(entries)} extra leagues ({ok} pages read)")
    return index


def index_is_fresh(output: str, max_age: int) -> bool:
    if max_age <= 0:
        return False
    index = load_json(os.path.join(output, "index.json"))
    if not index:
        return False
    generated = index.get("generatedAt") or 0
    try:
        return (time.time() * 1000 - int(generated)) < max_age * 1000
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------
# Fast pass: today's scores in one request

def fast_update(configs: list[LeagueConfig], output: str, now: dt.datetime) -> int:
    day = now.strftime("%Y%m%d")
    data = fetch_json(f"{API}/matches?date={day}&timezone=UTC&ccode3=USA")
    if not data:
        return 0
    wanted = config_ids(configs)
    updates: dict[str, dict] = {}
    for league in data.get("leagues") or []:
        try:
            primary = int(league.get("primaryId") or league.get("id"))
        except (TypeError, ValueError):
            continue
        if primary not in wanted:
            continue
        for match in league.get("matches") or []:
            status = match.get("status") or {}
            home = match.get("home") or {}
            away = match.get("away") or {}
            match_id = str(match.get("id"))
            updates[match_id] = {
                "start": iso_utc(status.get("utcTime")),
                "state": "off" if status.get("cancelled") else "post" if status.get("finished")
                else "in" if status.get("started") else "pre",
                "hs": home.get("score"),
                "as": away.get("score"),
            }
    if not updates:
        return 0
    changed = 0
    for config in configs:
        path = os.path.join(output, config.id + ".json")
        league = load_json(path)
        if not league:
            continue
        touched = False
        for event in league.get("events") or []:
            update = updates.get(str(event.get("id")))
            if not update:
                continue
            for key in ("start", "state"):
                if update.get(key) and event.get(key) != update[key]:
                    event[key] = update[key]
                    touched = True
            if update["state"] in {"in", "post"}:
                for key in ("hs", "as"):
                    if update.get(key) is not None and str(event.get(key)) != str(update[key]):
                        event[key] = str(update[key])
                        touched = True
            status = "FT" if update["state"] == "post" else "Live" if update["state"] == "in" else "Postponed" if update["state"] == "off" else "NS"
            if event.get("status") != status:
                event["status"] = status
                touched = True
        if touched:
            league["generatedAt"] = int(now.timestamp() * 1000)
            write_json(path, league)
            changed += 1
    return changed


# --------------------------------------------------------------------------
# Game details (optional)

def game_details(match_id: str, now: dt.datetime) -> dict | None:
    data = fetch_json(f"{API}/matchDetails?matchId={match_id}")
    if not data:
        return None
    general = data.get("general") or {}
    content = data.get("content") or {}
    facts = content.get("matchFacts") or {}
    moments = []
    for event in ((facts.get("events") or {}).get("events") or []):
        kind = clean(event.get("type") or "")
        if kind.lower() not in {"goal", "own goal", "owngoal", "penalty"}:
            continue
        if event.get("isPenaltyShootoutEvent"):
            continue
        home = bool(event.get("isHome"))
        if kind.lower() in {"own goal", "owngoal"}:
            home = not home
        moments.append({
            "kind": "Goal",
            "detail": clean(event.get("goalDescription") or ""),
            "home": home,
            "player": clean(event.get("nameStr") or (event.get("player") or {}).get("name") or ""),
            "assist": clean(event.get("assistInput") or ""),
            "min": event.get("time") if event.get("time") is not None else event.get("timeStr"),
        })
    stats = []
    periods = (content.get("stats") or {}).get("Periods") or {}
    group = periods.get("All") or {}
    for block in group.get("stats") or []:
        for stat in block.get("stats") or []:
            values = stat.get("stats") or []
            if len(values) < 2:
                continue
            stats.append({
                "name": clean(stat.get("title") or stat.get("key") or ""),
                "home": str(values[0]),
                "away": str(values[1]),
            })
    lineups = {}
    lineup = content.get("lineup") or {}
    for side in ("home", "away"):
        team = lineup.get(side + "Team") or {}
        players = []
        for sub, key in ((False, "starters"), (True, "subs")):
            for player in team.get(key) or []:
                players.append({
                    "num": str(player.get("shirtNumber") or ""),
                    "name": clean(player.get("name") or ""),
                    "sub": sub,
                })
        if players:
            lineups[side] = players
    return {
        "source": "FotMob (fotmob.com), unofficial",
        "generatedAt": int(now.timestamp() * 1000),
        "start": iso_utc((general.get("matchTimeUTCDate") or "")),
        "moments": moments,
        "stats": stats,
        "lineups": lineups,
    }


def details_pass(configs: list[LeagueConfig], output: str, games_output: str,
                 now: dt.datetime, days: int) -> int:
    if days <= 0:
        return 0
    cutoff = now - dt.timedelta(days=days)
    keep = set()
    made = 0
    os.makedirs(games_output, exist_ok=True)
    for config in configs:
        league = load_json(os.path.join(output, config.id + ".json"))
        if not league:
            continue
        for event in league.get("events") or []:
            start = parse_iso(event.get("start") or "")
            if start is None or start < cutoff or start > now + dt.timedelta(days=1):
                continue
            if event.get("state") == "pre":
                continue
            match_id = str(event.get("id"))
            keep.add(match_id)
            path = os.path.join(games_output, match_id + ".json")
            existing = load_json(path)
            if existing and event.get("state") == "post":
                generated = int(existing.get("generatedAt") or 0)
                if time.time() * 1000 - generated < 6 * 60 * 60 * 1000:
                    continue
            details = game_details(match_id, now)
            if details:
                write_json(path, details)
                made += 1
    # Remove game files no longer in the window, so the branch stays small.
    for name in os.listdir(games_output):
        if not name.endswith(".json"):
            continue
        match_id = name[:-5]
        if match_id not in keep:
            try:
                os.remove(os.path.join(games_output, name))
            except OSError:
                pass
    return made


# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=CONFIG)
    parser.add_argument("--output", default=DEFAULT_OUT)
    parser.add_argument("--games-output", default=DEFAULT_GAMES_OUT)
    parser.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE,
                        help="skip the slow pass when index.json is younger (seconds)")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--fast", action="store_true",
                        help="only update today's scores")
    parser.add_argument("--details-days", type=int, default=0,
                        help="also build game details for matches this many days back")
    args = parser.parse_args()

    configs = load_config(args.config)
    if not configs:
        log("No leagues in " + args.config)
        sys.exit(1)
    now = dt.datetime.now(dt.timezone.utc)
    os.makedirs(args.output, exist_ok=True)

    if args.fast:
        changed = fast_update(configs, args.output, now)
        log(f"Fast pass: {changed} league files updated")
        return

    slow = False
    if not args.force and index_is_fresh(args.output, args.max_age):
        log("Extra-league index is fresh; only refreshing today's scores.")
        changed = fast_update(configs, args.output, now)
        log(f"Fast pass: {changed} league files updated")
    else:
        slow_pass(configs, args.output, now)
        fast_update(configs, args.output, now)
        slow = True

    if args.details_days > 0 and slow:
        made = details_pass(configs, args.output, args.games_output, now, args.details_days)
        log(f"Game details: {made} files written")


if __name__ == "__main__":
    main()
