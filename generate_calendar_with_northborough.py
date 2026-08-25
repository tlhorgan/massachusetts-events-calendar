from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser

import generate_calendar as base


TOWN_SOURCE = "Northborough Town Community Events"
TOWN_YEAR_URL = "https://www.northboroughma.gov/calendar-by-event-type/20/year"
LIBRARY_SOURCE = "Northborough Free Library"
LIBRARY_BASE = "https://northboroughlibrary.assabetinteractive.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MassachusettsEventsCalendar/1.1)"
}

MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]


def get(url: str) -> requests.Response:
    response = requests.get(url, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return response


def month_sequence(start: datetime, count: int = 12):
    year = start.year
    month = start.month
    for _ in range(count):
        yield year, month
        month += 1
        if month == 13:
            month = 1
            year += 1


# ---------------------------------------------------------------------
# Northborough town community events
# ---------------------------------------------------------------------


def discover_town_event_urls():
    soup = BeautifulSoup(get(TOWN_YEAR_URL).text, "html.parser")
    urls = set()

    for link in soup.find_all("a", href=True):
        href = urljoin(TOWN_YEAR_URL, link["href"])
        parsed = urlparse(href)
        if parsed.netloc.lower() != "www.northboroughma.gov":
            continue
        if re.fullmatch(r"/home/events/\d+", parsed.path.rstrip("/")):
            urls.add(href.split("?")[0].split("#")[0].rstrip("/"))

    print(f"  {TOWN_SOURCE}: discovered {len(urls)} event pages")
    return sorted(urls)


def parse_town_event(url: str):
    soup = BeautifulSoup(get(url).text, "html.parser")
    h1 = soup.find("h1")
    title = base.clean(h1.get_text(" ", strip=True)) if h1 else ""
    if not title:
        return None

    text = base.clean(soup.get_text(" ", strip=True))
    match = re.search(
        r"Event Date:\s*"
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*"
        r"([A-Za-z]+\s+\d{1,2},\s+20\d{2})\s*-\s*"
        r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))"
        r"(?:\s+to\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)))?",
        text,
        re.I,
    )
    if not match:
        return None

    try:
        day = dtparser.parse(match.group(1)).date()
        start = datetime.combine(day, dtparser.parse(match.group(2)).time())
        if match.group(3):
            end = datetime.combine(day, dtparser.parse(match.group(3)).time())
            if end <= start:
                end += timedelta(days=1)
        else:
            end = start + timedelta(hours=2)
    except Exception:
        return None

    # Keep only current/future occurrences.
    if day < datetime.now().date():
        return None

    location = "Northborough, MA"
    address_match = re.search(
        r"((?:[A-Za-z0-9 .&'()-]+,?\s+)?\d+\s+[A-Za-z0-9 .'-]+(?:Street|St\.?|Road|Rd\.?|Avenue|Ave\.?|Drive|Dr\.?|Lane|Ln\.?|Way|Boulevard|Blvd\.?).{0,100}?Northborough,\s*MA\s*\d{5})",
        text,
        re.I,
    )
    if address_match:
        location = base.clean(address_match.group(1))

    meta = soup.find("meta", attrs={"name": "description"})
    description = base.clean(meta.get("content")) if meta and meta.get("content") else ""

    return {
        "title": title,
        "start": start,
        "end": end,
        "location": location,
        "description": description,
        "url": url,
        "sources": [TOWN_SOURCE],
    }


def fetch_northborough_town():
    print(f"Fetching {TOWN_SOURCE}...")
    items = []
    try:
        urls = discover_town_event_urls()
    except Exception as exc:
        print(f"  {TOWN_SOURCE}: discovery failed: {exc}")
        return items

    for url in urls:
        try:
            event = parse_town_event(url)
            if event:
                items.append(event)
        except Exception as exc:
            print(f"  {TOWN_SOURCE}: skip {url}: {exc}")

    print(f"  {TOWN_SOURCE}: {len(items)} future events")
    return items


# ---------------------------------------------------------------------
# Northborough Free Library / Assabet Interactive
# ---------------------------------------------------------------------


def discover_library_event_urls(months: int = 12):
    discovered = {}
    now = datetime.now()

    for year, month in month_sequence(now, months):
        month_name = MONTH_NAMES[month - 1]
        listing_url = f"{LIBRARY_BASE}/calendar/{year}-{month_name}/"
        try:
            soup = BeautifulSoup(get(listing_url).text, "html.parser")
        except Exception as exc:
            print(f"  {LIBRARY_SOURCE}: could not read {listing_url}: {exc}")
            continue

        before = len(discovered)
        listing_path = urlparse(listing_url).path.rstrip("/")

        for link in soup.find_all("a", href=True):
            href = urljoin(listing_url, link["href"])
            parsed = urlparse(href)
            if parsed.netloc.lower() != "northboroughlibrary.assabetinteractive.com":
                continue
            path = parsed.path.rstrip("/")
            if not path.startswith("/calendar/") or path == listing_path:
                continue
            # Ignore other calendar views; event detail URLs have a single slug
            # after /calendar/ rather than a YYYY-month calendar page.
            tail = path[len("/calendar/"):]
            if re.fullmatch(r"20\d{2}-[a-z]+", tail, re.I):
                continue
            if "/" in tail:
                continue
            discovered[href.split("?")[0].split("#")[0].rstrip("/") + "/"] = year

        print(
            f"  {LIBRARY_SOURCE}: {year}-{month:02d}, "
            f"+{len(discovered) - before}, total {len(discovered)}"
        )

    return discovered


def library_title(soup: BeautifulSoup):
    for tag_name in ("h1", "h2", "h3"):
        for tag in soup.find_all(tag_name):
            value = base.clean(tag.get_text(" ", strip=True))
            if not value:
                continue
            low = value.lower()
            if low in {"calendar", "northborough free library"}:
                continue
            if re.match(r"^(monday|tuesday|wednesday|thursday|friday|saturday|sunday),", low):
                continue
            return value
    return ""


def parse_library_event(url: str, inferred_year: int):
    soup = BeautifulSoup(get(url).text, "html.parser")
    title = library_title(soup)
    if not title:
        return None

    text = base.clean(soup.get_text(" ", strip=True))

    # Example: Saturday, August 1 10:00—11:00 AM Children's Program Room ...
    timed = re.search(
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*"
        r"([A-Za-z]+)\s+(\d{1,2})\s+"
        r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM))"
        r"\s*[—–-]\s*"
        r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM))",
        text,
        re.I,
    )

    if timed:
        try:
            day = dtparser.parse(f"{timed.group(1)} {timed.group(2)}, {inferred_year}").date()
            start = datetime.combine(day, dtparser.parse(timed.group(3)).time())
            end = datetime.combine(day, dtparser.parse(timed.group(4)).time())
            if end <= start:
                end += timedelta(days=1)
        except Exception:
            return None
    else:
        single = re.search(
            r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*"
            r"([A-Za-z]+)\s+(\d{1,2})\s+"
            r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM))",
            text,
            re.I,
        )
        all_day = re.search(
            r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*"
            r"([A-Za-z]+)\s+(\d{1,2})\s+All Day",
            text,
            re.I,
        )

        if single:
            try:
                day = dtparser.parse(f"{single.group(1)} {single.group(2)}, {inferred_year}").date()
                start = datetime.combine(day, dtparser.parse(single.group(3)).time())
                end = start + timedelta(hours=1)
            except Exception:
                return None
        elif all_day:
            try:
                day = dtparser.parse(f"{all_day.group(1)} {all_day.group(2)}, {inferred_year}").date()
                start = day
                end = day + timedelta(days=1)
            except Exception:
                return None
        else:
            return None

    if day < datetime.now().date():
        return None

    meta = soup.find("meta", attrs={"name": "description"})
    description = base.clean(meta.get("content")) if meta and meta.get("content") else ""

    return {
        "title": title,
        "start": start,
        "end": end,
        "location": "Northborough Free Library, 34 Main St, Northborough, MA 01532",
        "description": description,
        "url": url,
        "sources": [LIBRARY_SOURCE],
    }


def fetch_northborough_library():
    print(f"Fetching {LIBRARY_SOURCE}...")
    items = []
    discovered = discover_library_event_urls(12)

    for url, year in sorted(discovered.items()):
        try:
            event = parse_library_event(url, year)
            if event:
                items.append(event)
        except Exception as exc:
            print(f"  {LIBRARY_SOURCE}: skip {url}: {exc}")

    print(f"  {LIBRARY_SOURCE}: {len(items)} future events")
    return items


def main():
    # Preserve the existing statewide/regional sources and priority rules.
    base.SOURCE_PRIORITY.update({
        TOWN_SOURCE: 7,
        LIBRARY_SOURCE: 8,
    })

    all_items = []

    try:
        all_items.extend(base.fetch_simpleview_sources())
    except Exception as exc:
        print(f"ERROR loading Simpleview sources: {exc}")

    try:
        all_items.extend(base.fetch_tribe_sources())
    except Exception as exc:
        print(f"ERROR loading Tribe sources: {exc}")

    try:
        all_items.extend(fetch_northborough_town())
    except Exception as exc:
        print(f"ERROR loading {TOWN_SOURCE}: {exc}")

    try:
        all_items.extend(fetch_northborough_library())
    except Exception as exc:
        print(f"ERROR loading {LIBRARY_SOURCE}: {exc}")

    if not all_items:
        raise RuntimeError("No Massachusetts events were collected from any source.")

    print(f"Collected {len(all_items)} events before deduplication")
    unique = base.dedupe(all_items)

    if len(unique) < 25:
        raise RuntimeError(
            f"Only {len(unique)} unique Massachusetts events were generated; "
            "refusing to publish a suspiciously small feed."
        )

    base.build_calendar(unique)


if __name__ == "__main__":
    main()
