#!/usr/bin/env python3
"""Shared ESPN scoreboard cache for Nuvio One.

Asks ESPN's public, unauthenticated scoreboard endpoint for every league in
espn-leagues.txt, one day at a time, and writes the raw JSON into
espn/<league>.json in the shape the app reads:

    {"source": "...", "id": "<league>", "days": {
        "<yyyyMMdd>": {"fetchedAt": <ms>, "raw": "<ESPN JSON text>", "liveOrSoon": bool}
    }}

Only yesterday to a week ahead is kept. Each run asks for the days that are
due, rotates its starting league, and stops when its time budget is spent, so
a slow run never blocks the next one or empties the cache.

No API key and only the Python standard library.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ESPN = "https://site.api.espn.com/apis/site/v2/sports/"
UA = ("Mozilla/5.0 (Linux; Android 12; TV) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
OUT_DIR = "espn"
LEAGUES_FILE = "espn-leagues.txt"

TODAY_TTL = 8 * 60
EDGE_TTL = 45 * 60
FAR_TTL = 5 * 60 * 60
LIVE_TTL = 90

_last_request = 0.0
_started = time.time()


def log(message: str) -> None:
    print(message, flush=True)


def time_left(budget: float) -> float:
    return budget - (time.time() - _started)


def fetch(url: str, gap: float) -> str | None:
    """One request, paced and retried. Returns None on failure, never raises."""
    global _last_request
    for attempt in range(3):
        wait = gap - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
        request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            if error.code == 400:
                log(f"  {url.split('?')[0]}: HTTP 400")
                return None
            if error.code == 429 or error.code >= 500:
                time.sleep(8)
                continue
            log(f"  {url.split('?')[0]}: HTTP {error.code}")
            return None
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            log(f"  {url.split('?')[0]}: {type(error).__name__}")
            time.sleep(4)
    return None


def load_leagues(path: str) -> list[dict]:
    leagues = []
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 2:
                continue
            league_id, espn_path = parts[0].strip(), parts[1].strip()
            query = parts[2].strip() if len(parts) > 2 else ""
            if league_id and espn_path:
                leagues.append({"id": league_id, "path": espn_path, "query": query})
    return leagues


def safe(league_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", league_id)


def days_to_keep(now: dt.datetime) -> list[tuple[str, str]]:
    # ESPN groups games by US Eastern days. A fixed offset is close enough; the job
    # runs often enough that the DST hour sorts itself out. Phase 1 caches
    # yesterday/today/tomorrow; the app asks ESPN directly for the week ahead.
    local = now + dt.timedelta(hours=-4)
    out = []
    for offset in range(-1, 2):
        day = (local + dt.timedelta(days=offset)).strftime("%Y%m%d")
        category = "today" if offset == 0 else "edge"
        out.append((day, category))
    return out


def has_live_or_soon(raw: str | None, now: dt.datetime) -> bool:
    if not raw:
        return False
    if re.search(r'"state"\s*:\s*"in"', raw):
        return True
    for match in re.finditer(r'"date"\s*:\s*"([^"]+)"', raw):
        try:
            start = dt.datetime.strptime(match.group(1), "%Y-%m-%dT%H:%MZ").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if dt.timedelta(0) <= start - now <= dt.timedelta(minutes=30):
            return True
    return False


def load_existing(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle).get("days", {})
    except (ValueError, OSError):
        return {}


def is_due(info: dict | None, category: str, now_ms: int) -> bool:
    if info is None:
        return True
    age = now_ms - int(info.get("fetchedAt", 0))
    if info.get("liveOrSoon"):
        return age > LIVE_TTL * 1000
    if category == "today":
        return age > TODAY_TTL * 1000
    if category == "edge":
        return age > EDGE_TTL * 1000
    return age > FAR_TTL * 1000


def write_if_changed(path: str, payload: dict) -> bool:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            if handle.read() == text:
                return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return True


def run(budget: float, gap: float, only: list[str] | None, output: str) -> int:
    now = dt.datetime.now(dt.timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    days = days_to_keep(now)
    leagues = load_leagues(LEAGUES_FILE)
    if only:
        wanted = {item.strip() for item in only}
        leagues = [league for league in leagues if league["id"] in wanted or safe(league["id"]) in wanted]
    if not leagues:
        log("No leagues to fetch.")
        return 0
    # Start from a different league each run so a budget-cut run still gets around to all of them.
    rotate = int(now.timestamp() // 300) % len(leagues)
    leagues = leagues[rotate:] + leagues[:rotate]

    os.makedirs(output, exist_ok=True)
    changed = asked = skipped = 0
    oldest = days[0][0]
    for league in leagues:
        if time_left(budget) < 5:
            skipped += 1
            continue
        path = os.path.join(output, safe(league["id"]) + ".json")
        existing = load_existing(path)
        kept = {day: value for day, value in existing.items() if day >= oldest}
        touched = len(kept) != len(existing)
        for day, category in days:
            if not is_due(kept.get(day), category, now_ms):
                continue
            query = f"&limit=1000{league['query']}"
            url = f"{ESPN}{league['path']}/scoreboard?dates={day}{query}"
            raw = fetch(url, gap)
            asked += 1
            if raw is None:
                continue
            kept[day] = {
                "fetchedAt": now_ms,
                "raw": raw,
                "liveOrSoon": has_live_or_soon(raw, now),
            }
            touched = True
        if touched and write_if_changed(path, {
            "source": "ESPN (site.api.espn.com), unofficial",
            "id": league["id"],
            "days": kept,
        }):
            changed += 1
    log(f"ESPN cache: {asked} requests, {changed} files changed, {skipped} leagues skipped on budget")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget", type=float, default=270, help="seconds for this run (default 270)")
    parser.add_argument("--gap", type=float, default=0.25, help="seconds between requests (default 0.25)")
    parser.add_argument("--leagues", default="", help="comma-separated league ids for a test run")
    parser.add_argument("--output", default=OUT_DIR, help="folder for the JSON files (default espn)")
    args = parser.parse_args()
    only = [item for item in args.leagues.split(",") if item.strip()] if args.leagues else None
    sys.exit(run(args.budget, args.gap, only, args.output))


if __name__ == "__main__":
    main()
