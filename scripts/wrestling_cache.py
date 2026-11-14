#!/usr/bin/env python3
"""Shared wrestling schedule for Nuvio One.

Reads the public WWE events page and the public AEW events page, then writes
wrestling.json in the shape the app's Sports page reads:

    {"source": "...", "generatedAt": <ms>, "leagues": [
        {"key": "wwe", "badge": "...", "brands": [{"word": "raw", "badge": "..."}]}
    ], "events": [
        {"id": "...", "name": "...", "league": "wwe",
         "start": "2026-11-14T00:30:00Z", "hasTime": true,
         "venue": "...", "city": "...", "country": "..."}
    ]}

No API key, no third-party package, only the Python standard library. The
app's Sports page turns these shows into guide matches and Calendar games.

Only upcoming shows are kept (from a day ago, so today's show still shows
while it is on). The pages are public schedules; the cache is refreshed every
few hours, not on every app request.
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

WWE_URL = "https://www.wwe.com/events"
AEW_URL = "https://www.allelitewrestling.com/events"
TNA_URL = "https://tnawrestling.com/events/"
BOXING_URL = "https://box.live/upcoming-fights-schedule/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
DEFAULT_OUT = "wrestling.json"
DEFAULT_MAX_AGE = 5 * 60 * 60  # five hours

WWE_LOGO = "https://www.wwe.com/themes/custom/wwe_theme/logo.svg"
AEW_LOGO = ("https://static.wixstatic.com/media/"
            "815952_5964d86934774f97a1a12b104c8b0a7a~mv2.png")

_last_request = 0.0


def log(message: str) -> None:
    print(message, flush=True)


def fetch(url: str, gap: float = 1.0) -> str | None:
    """One request, paced and retried. Returns None on failure, never raises."""
    global _last_request
    for attempt in range(3):
        wait = gap - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
        request = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "identity",
        })
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
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
            time.sleep(5)
    return None


def clean_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def iso_utc(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Time zones. Windows does not ship the IANA database, so use the small set
# of offsets the two schedules need and the standard DST rules for them.

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    day = dt.date(year, month, 1)
    while day.weekday() != weekday:
        day += dt.timedelta(days=1)
    return day + dt.timedelta(days=7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> dt.date:
    if month == 12:
        day = dt.date(year + 1, 1, 1) - dt.timedelta(days=1)
    else:
        day = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    while day.weekday() != weekday:
        day -= dt.timedelta(days=1)
    return day


def _us_dst(day: dt.date) -> bool:
    start = _nth_weekday(day.year, 3, 6, 2)   # second Sunday in March
    end = _nth_weekday(day.year, 11, 6, 1)    # first Sunday in November
    return start <= day < end


def _eu_dst(day: dt.date) -> bool:
    start = _last_weekday(day.year, 3, 6)     # last Sunday in March
    end = _last_weekday(day.year, 10, 6)      # last Sunday in October
    return start <= day < end


def _au_dst(day: dt.date) -> bool:
    start = _nth_weekday(day.year, 10, 6, 1)  # first Sunday in October
    end = _nth_weekday(day.year, 4, 6, 1)     # first Sunday in April
    return day >= start or day < end


# name -> (rule, standard offset in minutes)
ZONES = {
    "America/New_York": ("us", -5 * 60),
    "America/Chicago": ("us", -6 * 60),
    "America/Denver": ("us", -7 * 60),
    "America/Los_Angeles": ("us", -8 * 60),
    "America/Phoenix": ("fixed", -7 * 60),
    "America/Toronto": ("us", -5 * 60),
    "America/Vancouver": ("us", -8 * 60),
    "America/Edmonton": ("us", -7 * 60),
    "America/Winnipeg": ("us", -6 * 60),
    "America/Halifax": ("us", -4 * 60),
    "America/St_Johns": ("us", -3 * 60 - 30),
    "America/Mexico_City": ("fixed", -6 * 60),
    "Australia/Perth": ("fixed", 8 * 60),
    "Europe/London": ("eu", 0),
    "Europe/Paris": ("eu", 60),
    "Australia/Sydney": ("au", 10 * 60),
    "Asia/Tokyo": ("fixed", 9 * 60),
    "UTC": ("fixed", 0),
}


def zone_offset(name: str, day: dt.date) -> dt.timedelta:
    rule, base = ZONES.get(name, ("fixed", -5 * 60))
    if rule == "us" and _us_dst(day):
        base += 60
    elif rule == "eu" and _eu_dst(day):
        base += 60
    elif rule == "au" and _au_dst(day):
        base += 60
    return dt.timedelta(minutes=base)


def local_to_utc(day: dt.date, hour: int, minute: int, zone: str) -> dt.datetime:
    local = dt.datetime(day.year, day.month, day.day, hour, minute,
                        tzinfo=dt.timezone(zone_offset(zone, day)))
    return local.astimezone(dt.timezone.utc)


STATE_ZONES = {
    # Eastern
    "CT": "America/New_York", "DE": "America/New_York", "DC": "America/New_York",
    "FL": "America/New_York", "GA": "America/New_York", "IN": "America/New_York",
    "ME": "America/New_York", "MD": "America/New_York", "MA": "America/New_York",
    "MI": "America/New_York", "NH": "America/New_York", "NJ": "America/New_York",
    "NY": "America/New_York", "NC": "America/New_York", "OH": "America/New_York",
    "PA": "America/New_York", "RI": "America/New_York", "SC": "America/New_York",
    "VT": "America/New_York", "VA": "America/New_York", "WV": "America/New_York",
    # Central
    "AL": "America/Chicago", "AR": "America/Chicago", "IL": "America/Chicago",
    "IA": "America/Chicago", "KS": "America/Chicago", "LA": "America/Chicago",
    "MN": "America/Chicago", "MS": "America/Chicago", "MO": "America/Chicago",
    "NE": "America/Chicago", "ND": "America/Chicago", "OK": "America/Chicago",
    "SD": "America/Chicago", "TN": "America/Chicago", "TX": "America/Chicago",
    "WI": "America/Chicago", "KY": "America/Chicago",
    # Mountain
    "AZ": "America/Phoenix", "CO": "America/Denver", "ID": "America/Denver",
    "MT": "America/Denver", "NM": "America/Denver", "UT": "America/Denver",
    "WY": "America/Denver",
    # Pacific
    "CA": "America/Los_Angeles", "NV": "America/Los_Angeles",
    "OR": "America/Los_Angeles", "WA": "America/Los_Angeles",
    # Canada and the rest
    "ON": "America/Toronto", "QC": "America/Toronto", "BC": "America/Vancouver",
    "AB": "America/Edmonton", "MB": "America/Winnipeg", "SK": "America/Regina",
    "NS": "America/Halifax", "NB": "America/Halifax", "PE": "America/Halifax",
    "NL": "America/St_Johns",
}

ABBR_ZONES = {
    "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
    "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
    "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
    "PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "GMT": "UTC", "UTC": "UTC", "BST": "Europe/London",
    "CET": "Europe/Paris", "CEST": "Europe/Paris",
    "AEST": "Australia/Sydney", "AEDT": "Australia/Sydney", "JST": "Asia/Tokyo",
}

COUNTRY_NAMES = {
    "united states", "usa", "us", "canada", "can", "mexico", "mx", "united kingdom", "uk",
    "england", "scotland", "wales", "northern ireland", "ireland", "australia",
    "new zealand", "japan", "saudi arabia", "germany", "france", "spain",
    "italy", "netherlands", "belgium", "switzerland", "austria", "denmark",
    "sweden", "norway", "finland", "poland", "portugal", "brazil", "argentina",
    "south africa", "india", "china", "south korea", "singapore", "philippines",
    "puerto rico", "dominican republic", "chile", "colombia", "peru", "uruguay",
}

COUNTRY_LABEL = {
    "usa": "United States", "us": "United States", "united states": "United States",
    "uk": "United Kingdom", "england": "United Kingdom", "scotland": "United Kingdom",
    "wales": "United Kingdom", "northern ireland": "United Kingdom",
    "united kingdom": "United Kingdom", "ireland": "Ireland", "canada": "Canada",
    "mexico": "Mexico", "mx": "Mexico", "australia": "Australia", "new zealand": "New Zealand",
    "canada": "Canada", "can": "Canada",
}

AUSTRALIA_CITIES = {
    "perth", "melbourne", "sydney", "brisbane", "adelaide", "canberra",
    "gold coast", "hobart", "darwin", "newcastle", "wollongong",
}

CANADA_STATES = {"ON", "QC", "BC", "AB", "MB", "SK", "NS", "NB", "PE", "NL", "YT", "NT", "NU"}
MEXICO_STATES = {
    "CMX", "CDMX", "MEX", "PUE", "NL", "JAL", "YUC", "QROO", "BC", "SON", "CHH",
    "GUA", "GTO", "QRO", "TAM", "VER", "SIN", "DGO", "ZAC", "AGS", "MOR", "HGO",
    "TLA", "COL", "NAY", "OAX", "CHP", "TAB", "CAM", "ROO", "BCS", "COA", "DUR",
}


def split_place(place: str) -> tuple[str, str]:
    """'Trenton, NJ' -> ('Trenton', 'United States')."""
    parts = [part.strip() for part in (place or "").split(",") if part.strip()]
    if not parts:
        return "", ""
    city = parts[0]
    if city.lower() in AUSTRALIA_CITIES:
        return city, "Australia"
    for part in parts[1:]:
        key = part.lower()
        if key in COUNTRY_NAMES:
            return city, COUNTRY_LABEL.get(key, part)
    for part in reversed(parts[1:]):
        key = part.upper()
        if key in STATE_ZONES:
            return city, "Canada" if key in CANADA_STATES else "United States"
        if key in {"AK", "HI"}:
            return city, "United States"
        if key in MEXICO_STATES:
            return city, "Mexico"
    if len(parts) > 1:
        return city, COUNTRY_LABEL.get(parts[-1].lower(), parts[-1])
    return city, ""


def zone_from_place(place: str, fallback: str = "America/New_York") -> str:
    parts = [part.strip() for part in (place or "").split(",") if part.strip()]
    if not parts:
        return fallback
    if parts[0].lower() in AUSTRALIA_CITIES:
        return "Australia/Perth" if parts[0].lower() == "perth" else "Australia/Sydney"
    for part in reversed(parts):
        key = part.upper()
        if key in STATE_ZONES:
            return STATE_ZONES[key]
        if key in MEXICO_STATES:
            return "America/Mexico_City"
    tail = parts[-1].lower()
    if tail in {"united kingdom", "uk", "england", "scotland", "wales"}:
        return "Europe/London"
    if tail in {"australia"}:
        return "Australia/Sydney"
    if tail in {"mexico", "mx"}:
        return "America/Mexico_City"
    return fallback


# --------------------------------------------------------------------------
# WWE

WWE_MONTHS = {name: i for i, name in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


def _wwe_rows(page: str) -> list[dict]:
    rows = []
    for block in re.findall(r"<tr>(.*?)</tr>", page, re.S):
        if "events-upcoming-header" not in block:
            continue
        title_match = re.search(
            r'<h2 class="events-upcoming-header"><a[^>]*>(.*?)</a>', block, re.S)
        when_match = re.search(r'<time datetime="([^"]+)"', block)
        if not title_match or not when_match:
            continue
        title = clean_text(title_match.group(1))
        try:
            when = dt.datetime.fromisoformat(when_match.group(1).replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        city_match = re.search(
            r'events-upcoming-card--venue events-search-results--venue[^>]*>\s*<p>(.*?)</p>',
            block, re.S)
        venue_match = re.search(
            r'<time[^>]*>[^<]*</time>\s*\|\s*([^<]+)</p>', block, re.S)
        if not venue_match:
            venue_match = re.search(r'venue-container">([^<]+)</div>', block)
        city = clean_text(city_match.group(1)) if city_match else ""
        venue = clean_text(venue_match.group(1)) if venue_match else ""
        href_match = re.search(r'href="([^"]+)"', block)
        logo_match = re.search(
            r'events-upcoming-logo.*?<img[^>]+src="([^"]+)"', block, re.S)
        logo = logo_match.group(1) if logo_match else ""
        if logo.startswith("/"):
            logo = "https://www.wwe.com" + logo
        rows.append({
            "title": title,
            "when": when,
            "city": city,
            "venue": venue,
            "href": href_match.group(1) if href_match else "",
            "logo": logo,
        })
    return rows


WWE_PPV_WORDS = [
    "wrestlemania", "royal rumble", "summer slam", "summerslam", "survivor series",
    "money in the bank", "backlash", "bad blood", "crown jewel", "clash",
    "saturday night's main event", "saturday nights main event", "hell in a cell",
    "fastlane", "payback", "night of champions", "battleground", "extreme rules",
    "tlc", "armageddon", "judgment day", "king and queen of the ring",
    "elimination chamber", "mitb", "no mercy", "unforgiven", "cyber sunday",
    "new year's revolution", "great american bash", "vengeance", "fully loaded",
    "invasion", "insurrextion", "rebellion", "backlash", "bad blood",
    "wrestlepalooza", "john cena classic",
]


def wwe_brand(title: str) -> str | None:
    lower = title.lower()
    if "smackdown" in lower:
        return "smackdown"
    if "raw" in lower:
        return "raw"
    if "nxt" in lower:
        return "nxt"
    for word in WWE_PPV_WORDS:
        if word in lower:
            return word
    return None


def wwe_is_televised(title: str) -> bool:
    """House-show tours are not on TV, so they would only make false guide matches."""
    lower = title.lower()
    if "live tour" in lower or lower.startswith("road to "):
        return False
    return wwe_brand(title) is not None


def parse_wwe(page: str, now: dt.datetime) -> tuple[list[dict], dict[str, str]]:
    events = []
    brands: dict[str, str] = {}
    cutoff = now - dt.timedelta(days=1)
    for row in _wwe_rows(page):
        if row["when"] < cutoff:
            continue
        title = row["title"]
        if not title or not wwe_is_televised(title):
            continue
        slug_part = slug(row["href"].rsplit("/", 1)[-1]) if row["href"] else slug(title)
        city, country = split_place(row["city"])
        event_id = "wwe-" + (slug_part or slug(title))
        brand = wwe_brand(title)
        if brand and row["logo"]:
            brands.setdefault(brand, row["logo"])
        events.append({
            "id": event_id,
            "name": title,
            "league": "wwe",
            "start": iso_utc(row["when"]),
            "hasTime": True,
            "venue": row["venue"] or None,
            "city": city or None,
            "country": country or None,
        })
    return events, brands


def _wwe_date(value: str, now: dt.datetime) -> dt.date | None:
    text = re.sub(r"(?i)^\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s*", "", value.strip())
    text = re.sub(r"(?i)(\d{1,2})(st|nd|rd|th)", r"\1", text)
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            day = dt.datetime.strptime(f"{text} {now.year}", fmt).date()
        except ValueError:
            continue
        if day < now.date() - dt.timedelta(days=1):
            day = day.replace(year=now.year + 1)
        return day
    return None


def _wwe_details(href: str) -> dict:
    """The event page has the precise UTC time and the venue; the carousel only a date."""
    if not href:
        return {}
    page = fetch("https://www.wwe.com" + href)
    if page is None:
        return {}
    when_match = re.search(r'<time datetime="([^"]+)"', page)
    venue_match = re.search(
        r'event-promo-content--date--venue">(.*?)</p>', page, re.S)
    place_match = re.search(
        r'event-promo-content--date--location">(.*?)</p>', page, re.S)
    out = {}
    if when_match:
        try:
            when = dt.datetime.fromisoformat(when_match.group(1).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=dt.timezone.utc)
            out["when"] = when
        except ValueError:
            pass
    if venue_match:
        out["venue"] = clean_text(venue_match.group(1))
    if place_match:
        out["city"] = clean_text(place_match.group(1))
    return out


def parse_wwe_trending(page: str, now: dt.datetime) -> tuple[list[dict], dict[str, str]]:
    events = []
    brands: dict[str, str] = {}
    cutoff = now - dt.timedelta(days=1)
    seen = set()
    for block in page.split('<div class="event-item">')[1:]:
        block = block[:4000]
        href_match = re.search(r'href="([^"]+)"', block)
        title_match = re.search(r'<h3 class="le-card-meta-title">(.*?)</h3>', block, re.S)
        date_match = re.search(r'<p class="le-card-meta-date">(.*?)</p>', block, re.S)
        place_match = re.search(r'<p class="le-card-meta-venue">(.*?)</p>', block, re.S)
        image_match = re.search(r'<img[^>]+src="([^"]+)"', block)
        if not href_match or not title_match:
            continue
        href = href_match.group(1)
        if href in seen:
            continue
        seen.add(href)
        title = clean_text(title_match.group(1))
        if not title or not wwe_is_televised(title):
            continue
        day = _wwe_date(clean_text(date_match.group(1)) if date_match else "", now)
        if day is None:
            continue
        details = _wwe_details(href)
        when = details.get("when")
        has_time = when is not None
        if when is None:
            when = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
        if when < cutoff:
            continue
        place = details.get("city") or (clean_text(place_match.group(1)) if place_match else "")
        city, country = split_place(place)
        slug_part = slug(href.rsplit("/", 1)[-1]) or slug(title)
        image = image_match.group(1) if image_match else ""
        if image.startswith("/"):
            image = "https://www.wwe.com" + image
        brand = wwe_brand(title)
        if brand and image:
            brands.setdefault(brand, image)
        events.append({
            "id": "wwe-" + slug_part,
            "name": title,
            "league": "wwe",
            "start": iso_utc(when),
            "hasTime": has_time,
            "venue": details.get("venue") or None,
            "city": city or None,
            "country": country or None,
        })
    return events, brands


# --------------------------------------------------------------------------
# AEW (Wix embeds the event records in the page as JSON)

def _brace_object(text: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _aew_records(page: str) -> dict:
    marker = '"recordsByCollectionId"'
    marker_at = page.find(marker)
    if marker_at < 0:
        return {}
    key = '"AEWEvents":'
    key_at = page.find(key, marker_at)
    if key_at < 0:
        return {}
    start = key_at + len(key)
    raw = _brace_object(page, start)
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _aew_name(event_type: str, title: str) -> str:
    kind = re.sub(r"(?i)^\s*AEW\s*:\s*", "", event_type or "").strip()
    kind = re.sub(r"(?i)\s+PPV\s*$", "", kind).strip()
    kind = re.sub(r"(?i)dyamite", "Dynamite", kind)
    kind = re.sub(r"(?i)dynamte", "Dynamite", kind)
    kind = kind.replace("/", " & ")
    kind = re.sub(r"\s+", " ", kind).strip()
    if not kind:
        return title
    return "AEW " + kind


BRAND_FIXES = {"dyamite": "dynamite", "dynamte": "dynamite"}


def _aew_brands(event_type: str) -> list[str]:
    kind = re.sub(r"(?i)^\s*AEW\s*:\s*", "", event_type or "").strip()
    kind = re.sub(r"(?i)\s+PPV\s*$", "", kind).strip()
    words = [word.strip() for word in re.split(r"[/&]", kind) if word.strip()]
    if not words:
        return []
    return [BRAND_FIXES.get(word.lower(), word.lower()) for word in words]


def _parse_clock(value: str) -> tuple[int, int, str | None]:
    match = re.search(r"(\d{1,2}):(\d{2})\s*([ap]m)?\s*([A-Za-z]{2,4})?", value or "")
    if not match:
        return 0, 0, None
    hour = int(match.group(1))
    minute = int(match.group(2))
    suffix = (match.group(3) or "").lower()
    if suffix == "pm" and hour != 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    return hour, minute, (match.group(4) or "").upper() or None


def _aew_date(value: str) -> dt.date | None:
    cleaned = clean_text(value).title().replace(",", "")
    for fmt in ("%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(cleaned, fmt).date()
        except ValueError:
            pass
    return None


def parse_aew(page: str, now: dt.datetime) -> tuple[list[dict], dict[str, str]]:
    events = []
    brands: dict[str, str] = {}
    cutoff = now - dt.timedelta(days=1)
    for record in _aew_records(page).values():
        if not isinstance(record, dict):
            continue
        title = clean_text(record.get("title") or record.get("eventName") or "")
        event_type = clean_text(record.get("eventType") or "")
        if not title:
            continue
        if "roh" in title.lower() or "roh" in event_type.lower():
            continue
        day = _aew_date(record.get("date") or "")
        if day is None:
            continue
        clock_text = record.get("time") or ""
        has_clock = bool(re.search(r"\d", clock_text))
        hour, minute, abbr = _parse_clock(clock_text)
        zone = ABBR_ZONES.get(abbr or "", "") or zone_from_place(record.get("city") or "")
        start = local_to_utc(day, hour, minute, zone)
        if not has_clock:
            start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
        if start < cutoff:
            continue
        city, country = split_place(record.get("city") or "")
        link = record.get("link-aew-events-1-title-2") or ""
        slug_part = slug(link.rsplit("/", 1)[-1]) if link else slug(title + "-" + day.isoformat())
        if not slug_part.startswith("aew-"):
            slug_part = "aew-" + slug_part
        event_id = slug_part or ("aew-" + slug(title + "-" + day.isoformat()))
        name = _aew_name(event_type, title)
        promo = record.get("promoImage") or ""
        if promo.startswith("wix:image://v1/"):
            promo = "https://static.wixstatic.com/media/" + promo[len("wix:image://v1/"):]
            promo = promo.split("#", 1)[0]
        for brand in _aew_brands(event_type):
            if promo:
                brands.setdefault(brand, promo)
        about = clean_text(record.get("eventDescription") or "")
        events.append({
            "id": event_id,
            "name": name,
            "league": "aew",
            "start": iso_utc(start),
            "hasTime": has_clock,
            "venue": clean_text(record.get("venue") or "") or None,
            "city": city or None,
            "country": country or None,
            "about": about[:600] or None,
        })
    return events, brands


# --------------------------------------------------------------------------

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _tna_date(href: str, date_text: str, now: dt.datetime) -> dt.date | None:
    match = re.search(r"([A-Za-z]+)-(\d{1,2})-(\d{4})", href or "")
    if match:
        month = MONTHS.get(match.group(1).lower())
        if month:
            try:
                return dt.date(int(match.group(3)), month, int(match.group(2)))
            except ValueError:
                pass
    match = re.search(r"([A-Za-z]+)\s*<br\s*/?>\s*(\d{1,2})", date_text or "")
    if match:
        month = MONTHS.get(match.group(1).lower())
        if month:
            try:
                day = dt.date(now.year, month, int(match.group(2)))
                if day < now.date() - dt.timedelta(days=1):
                    day = day.replace(year=now.year + 1)
                return day
            except ValueError:
                pass
    return None


def parse_tna(page: str, now: dt.datetime) -> list[dict]:
    events = []
    cutoff = now - dt.timedelta(days=1)
    seen = set()
    for block in re.split(r'<div class="col-lg-4 mb-3"', page)[1:]:
        block = block[:3500]
        href_match = re.search(r'href="(https://tnawrestling\.com/events/[^"]+)"', block)
        if not href_match:
            continue
        href = href_match.group(1)
        slug_part = slug(href.rsplit("/", 1)[-1])
        if slug_part in seen:
            continue
        paragraphs = re.findall(r'<p class="mb-0">(.*?)</p>', block, re.S)
        title = clean_text(paragraphs[-1]) if paragraphs else ""
        if not title or re.search(r"(?i)^(sunday|monday|tuesday|wednesday|thursday|friday|saturday)\s*\|", title):
            title = "TNA"
        date_box = re.search(r'style="background-color: #eee;[^"]*">(.*?)</div>', block, re.S)
        day = _tna_date(href, date_box.group(1) if date_box else "", now)
        if day is None:
            continue
        when = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
        if when < cutoff:
            continue
        h4_match = re.search(r'<h4[^>]*>(.*?)</h4>', block, re.S)
        h4_raw = h4_match.group(1) if h4_match else ""
        place = ""
        venue = clean_text(h4_raw)
        if "</i>" in h4_raw:
            left, right = h4_raw.split("</i>", 1)
            place = clean_text(left)
            venue = clean_text(right)
        city, country = split_place(place or h4_raw)
        seen.add(slug_part)
        events.append({
            "id": "tna-" + (slug_part or slug(title)),
            "name": title,
            "league": "tna",
            "start": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hasTime": False,
            "venue": venue or None,
            "city": city or None,
            "country": country or None,
        })
    return events


def parse_boxing(page: str, now: dt.datetime) -> list[dict]:
    events = []
    cutoff = now - dt.timedelta(days=1)
    seen = set()
    table = re.search(r'id="boxing-schedule-table".*?<tbody>(.*?)</tbody>', page, re.S)
    if not table:
        return events
    for row in re.findall(r"<tr>(.*?)</tr>", table.group(1), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 3:
            continue
        date_text = clean_text(cells[0])
        fight_match = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', cells[1], re.S)
        if not fight_match:
            continue
        name = clean_text(fight_match.group(2))
        href = fight_match.group(1)
        if not name:
            continue
        day = None
        for fmt in ("%A, %d %B %Y", "%d %B %Y", "%A %d %B %Y", "%d %b %Y"):
            try:
                day = dt.datetime.strptime(date_text, fmt).date()
                break
            except ValueError:
                pass
        if day is None:
            continue
        when = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
        if when < cutoff:
            continue
        parts = [part.strip() for part in clean_text(cells[2]).split(",") if part.strip()]
        if len(parts) >= 3:
            venue, city, country = parts[0], parts[1], parts[-1]
        elif len(parts) == 2:
            venue, city, country = parts[0], "", parts[1]
        elif parts:
            venue, city, country = parts[0], "", ""
        else:
            venue = city = country = ""
        slug_part = slug(href.rstrip("/").rsplit("/", 1)[-1]) or slug(name + "-" + day.isoformat())
        if slug_part in seen:
            continue
        seen.add(slug_part)
        events.append({
            "id": "boxing-" + slug_part,
            "name": name,
            "league": "boxing",
            "start": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hasTime": False,
            "venue": venue or None,
            "city": city or None,
            "country": country or None,
        })
    return events


def _dedupe(events: list[dict]) -> list[dict]:
    seen_keys = set()
    seen_ids = set()
    out = []
    for event in sorted(events, key=lambda item: item["start"]):
        show_key = (event["league"], event["name"].lower(), event["start"])
        if show_key in seen_keys:
            continue
        seen_keys.add(show_key)
        base = event["id"]
        key = base
        while key in seen_ids:
            key = base + "-" + event["start"][:10]
            if key in seen_ids:
                key = base + "-" + event["start"].replace(":", "").replace("-", "")
        event["id"] = key
        seen_ids.add(key)
        out.append(event)
    return out


def build(wwe_page: str, aew_page: str, tna_page: str, boxing_page: str,
          now: dt.datetime) -> dict:
    wwe_events, wwe_brands = parse_wwe(wwe_page, now)
    trending, trending_brands = parse_wwe_trending(wwe_page, now)
    for word, badge in trending_brands.items():
        wwe_brands.setdefault(word, badge)
    known = {event["id"] for event in wwe_events}
    for event in trending:
        if event["id"] not in known:
            wwe_events.append(event)
    aew_events, aew_brands = parse_aew(aew_page, now)
    tna_events = parse_tna(tna_page, now)
    boxing_events = parse_boxing(boxing_page, now)
    leagues = [
        {
            "key": "wwe",
            "badge": WWE_LOGO,
            "brands": [{"word": word, "badge": badge}
                       for word, badge in sorted(wwe_brands.items())],
        },
        {
            "key": "aew",
            "badge": AEW_LOGO,
            "brands": [{"word": word, "badge": badge}
                       for word, badge in sorted(aew_brands.items())],
        },
        {"key": "tna", "brands": []},
        {"key": "boxing", "brands": []},
    ]
    return {
        "source": "WWE.com, AEW, TNA and Box.live, unofficial",
        "generatedAt": int(now.timestamp() * 1000),
        "leagues": leagues,
        "events": _dedupe(wwe_events + aew_events + tna_events + boxing_events),
    }


def write_if_changed(path: str, payload: dict) -> bool:
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


def is_fresh(path: str, max_age: int) -> bool:
    if max_age <= 0 or not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as handle:
            generated = int(json.load(handle).get("generatedAt", 0))
    except (OSError, ValueError, TypeError):
        return False
    return generated > 0 and (time.time() * 1000 - generated) < max_age * 1000


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=DEFAULT_OUT,
                        help="file to write (default wrestling.json)")
    parser.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE,
                        help="skip when the file is younger than this many seconds")
    parser.add_argument("--force", action="store_true", help="fetch even when fresh")
    args = parser.parse_args()

    if not args.force and is_fresh(args.output, args.max_age):
        log(f"{args.output} is fresh; skipping.")
        return

    now = dt.datetime.now(dt.timezone.utc)
    pages = {
        "wwe": fetch(WWE_URL),
        "aew": fetch(AEW_URL),
        "tna": fetch(TNA_URL),
        "boxing": fetch(BOXING_URL),
    }
    if all(page is None for page in pages.values()):
        log("All schedule pages failed; keeping the old file.")
        sys.exit(1)
    for name, page in pages.items():
        if page is None:
            log(f"{name.upper()} page failed; skipping it.")
            pages[name] = "<html></html>"

    payload = build(pages["wwe"], pages["aew"], pages["tna"], pages["boxing"], now)
    changed = write_if_changed(args.output, payload)
    counts = {}
    for event in payload["events"]:
        counts[event["league"]] = counts.get(event["league"], 0) + 1
    summary = ", ".join(f"{key.upper()} {counts.get(key, 0)}" for key in ("wwe", "aew", "tna", "boxing"))
    log(f"Wrestling schedule: {summary} events; "
        f"{'updated' if changed else 'unchanged'} {args.output}")


if __name__ == "__main__":
    main()
