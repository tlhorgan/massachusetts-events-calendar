from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse, parse_qsl

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from icalendar import Calendar, Event
from playwright.sync_api import sync_playwright

OUTPUT = Path("massachusetts-events.ics")
TZNAME = "America/New_York"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MassachusettsEventsCalendar/1.0)"
}

SIMPLEVIEW_SOURCES = [
    ("VisitMA", "https://www.visitma.com/events/"),
    ("Cape Cod Chamber", "https://www.capecodchamber.org/events/"),
    ("Meet Boston", "https://www.meetboston.com/events/"),
    ("Discover Central Massachusetts", "https://www.discovercentralma.org/events/events-calendar/"),
]

TRIBE_SOURCES = [
    ("Explore Western Mass", "https://explorewesternmass.com"),
    ("The Berkshires", "https://berkshires.org"),
    ("North of Boston", "https://northofboston.org"),
]

MAX_SIMPLEVIEW_PAGES = 50
SIMPLEVIEW_PAGE_SIZE = 12
MAX_TRIBE_PAGES = 20


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    return clean(BeautifulSoup(html_lib.unescape(value), "html.parser").get_text(" "))


def norm(value: str | None) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return clean(value)


def event_day(item) -> date:
    start = item["start"]
    return start.date() if isinstance(start, datetime) else start


def naive_datetime(value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def get_json(url: str, params=None):
    response = requests.get(url, params=params, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return response.json()


def add_query(url: str, **params) -> str:
    parsed = urlparse(url)
    current = dict(parse_qsl(parsed.query))
    current.update({k: str(v) for k, v in params.items()})
    return parsed._replace(query=urlencode(current)).geturl()


def simpleview_event_urls(page, source_name: str, base_url: str):
    urls = set()
    empty_rounds = 0

    for page_num in range(MAX_SIMPLEVIEW_PAGES):
        skip = page_num * SIMPLEVIEW_PAGE_SIZE
        url = add_query(base_url, skip=skip, bounds="false", view="list", sort="date")

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(1800)
        except Exception as exc:
            print(f"  {source_name}: page {page_num + 1} load warning: {exc}")

        try:
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(500)
        except Exception:
            pass

        hrefs = page.locator("a[href]").evaluate_all("(els) => els.map(e => e.href)")
        before = len(urls)

        for href in hrefs:
            if not href:
                continue
            p = urlparse(href)
            if p.netloc != urlparse(base_url).netloc:
                continue
            path = p.path.rstrip("/")
            if "/event/" in path.lower():
                urls.add(href.split("?")[0].split("#")[0].rstrip("/"))

        added = len(urls) - before
        print(f"  {source_name}: listing page {page_num + 1}, +{added}, total {len(urls)}")

        empty_rounds = empty_rounds + 1 if added == 0 else 0
        if empty_rounds >= 2:
            break

    return sorted(urls)


def walk_jsonld(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from walk_jsonld(x)
    elif isinstance(obj, dict):
        if isinstance(obj.get("@graph"), list):
            for x in obj["@graph"]:
                yield from walk_jsonld(x)
        yield obj


def parse_jsonld_event_objects(soup: BeautifulSoup, source_name: str, page_url: str):
    items = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        for obj in walk_jsonld(data):
            if not isinstance(obj, dict):
                continue
            typ = obj.get("@type", [])
            if isinstance(typ, str):
                typ = [typ]
            if not any("event" in str(t).lower() for t in typ):
                continue

            title = clean(obj.get("name"))
            start_raw = obj.get("startDate")
            if not title or not start_raw:
                continue

            try:
                start = naive_datetime(dtparser.parse(str(start_raw)))
            except Exception:
                continue

            end = None
            if obj.get("endDate"):
                try:
                    end = naive_datetime(dtparser.parse(str(obj["endDate"])))
                except Exception:
                    pass
            if end is None:
                end = start + timedelta(hours=2)

            location = ""
            loc = obj.get("location")
            if isinstance(loc, dict):
                parts = [clean(loc.get("name"))]
                addr = loc.get("address")
                if isinstance(addr, dict):
                    parts.extend([
                        clean(addr.get("streetAddress")),
                        clean(addr.get("addressLocality")),
                        clean(addr.get("addressRegion")),
                        clean(addr.get("postalCode")),
                    ])
                elif isinstance(addr, str):
                    parts.append(clean(addr))
                location = ", ".join(x for x in parts if x)
            elif isinstance(loc, str):
                location = clean(loc)

            description = strip_html(str(obj.get("description", "")))
            event_url = clean(obj.get("url")) or page_url

            items.append({
                "title": title,
                "start": start,
                "end": end,
                "location": location,
                "description": description,
                "url": event_url,
                "sources": [source_name],
            })
    return items


def parse_simpleview_visible_fallback(soup: BeautifulSoup, source_name: str, page_url: str):
    h1 = soup.find("h1")
    title = clean(h1.get_text(" ", strip=True)) if h1 else ""
    if not title:
        return []

    text = clean(soup.get_text(" ", strip=True))
    pattern = re.compile(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December|"
        r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+"
        r"(\d{1,2}),?\s+(20\d{2})\s+"
        r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM))"
        r"(?:\s*[-–—]\s*(\d{1,2}(?::\d{2})?\s*(?:AM|PM)))?",
        re.I,
    )
    match = pattern.search(text)
    if not match:
        return []

    try:
        d = dtparser.parse(f"{match.group(1)} {match.group(2)}, {match.group(3)}").date()
        start_t = dtparser.parse(match.group(4)).time()
        start = datetime.combine(d, start_t)
        if match.group(5):
            end_t = dtparser.parse(match.group(5)).time()
            end = datetime.combine(d, end_t)
            if end <= start:
                end += timedelta(days=1)
        else:
            end = start + timedelta(hours=2)
    except Exception:
        return []

    meta = soup.find("meta", attrs={"name": "description"})
    description = clean(meta.get("content")) if meta and meta.get("content") else ""

    return [{
        "title": title,
        "start": start,
        "end": end,
        "location": "",
        "description": description,
        "url": page_url,
        "sources": [source_name],
    }]


def fetch_simpleview_sources():
    all_items = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1200}, user_agent=HEADERS["User-Agent"])

        for source_name, base_url in SIMPLEVIEW_SOURCES:
            print(f"Fetching {source_name}...")
            try:
                urls = simpleview_event_urls(page, source_name, base_url)
            except Exception as exc:
                print(f"ERROR discovering {source_name}: {exc}")
                continue

            print(f"  {source_name}: discovered {len(urls)} event pages")
            source_items = []

            for url in urls:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(250)
                    soup = BeautifulSoup(page.content(), "html.parser")
                    parsed = parse_jsonld_event_objects(soup, source_name, url)
                    if not parsed:
                        parsed = parse_simpleview_visible_fallback(soup, source_name, url)
                    source_items.extend(parsed)
                except Exception as exc:
                    print(f"    {source_name} skip {url}: {exc}")

            print(f"  {source_name}: {len(source_items)} parsed events")
            all_items.extend(source_items)

        browser.close()
    return all_items


def parse_tribe_location(event_obj):
    venue = event_obj.get("venue") or {}
    if not isinstance(venue, dict):
        return ""
    parts = [
        clean(venue.get("venue")),
        clean(venue.get("address")),
        clean(venue.get("city")),
        clean(venue.get("stateprovince")),
        clean(venue.get("zip")),
    ]
    return ", ".join(x for x in parts if x)


def is_massachusetts_event(event_obj, location: str) -> bool:
    venue = event_obj.get("venue") or {}
    state = clean(venue.get("stateprovince")) if isinstance(venue, dict) else ""
    if state:
        return norm(state) in {"ma", "massachusetts"}

    padded = f" {norm(location)} "
    foreign_markers = [
        " connecticut ", " ct ", " rhode island ", " ri ", " vermont ", " vt ",
        " new hampshire ", " nh ", " new york ", " ny ",
    ]
    return not any(marker in padded for marker in foreign_markers)


def fetch_tribe_source(source_name: str, root_url: str):
    print(f"Fetching {source_name}...")
    endpoint = root_url.rstrip("/") + "/wp-json/tribe/events/v1/events"
    items = []

    for page_num in range(1, MAX_TRIBE_PAGES + 1):
        params = {
            "per_page": 50,
            "page": page_num,
            "start_date": datetime.now().strftime("%Y-%m-%d"),
        }
        try:
            data = get_json(endpoint, params=params)
        except Exception as exc:
            print(f"  {source_name}: API page {page_num} failed: {exc}")
            break

        events = data.get("events") or []
        if not events:
            break

        for obj in events:
            title = strip_html(obj.get("title"))
            if not title:
                continue
            try:
                start = naive_datetime(dtparser.parse(obj.get("start_date")))
            except Exception:
                continue
            try:
                end = naive_datetime(dtparser.parse(obj.get("end_date"))) if obj.get("end_date") else start + timedelta(hours=2)
            except Exception:
                end = start + timedelta(hours=2)

            if obj.get("all_day"):
                start = start.date()
                end_date = end.date() if isinstance(end, datetime) else end
                end = end_date + timedelta(days=1)

            location = parse_tribe_location(obj)
            if not is_massachusetts_event(obj, location):
                continue

            items.append({
                "title": title,
                "start": start,
                "end": end,
                "location": location,
                "description": strip_html(obj.get("description")),
                "url": clean(obj.get("url")) or root_url,
                "sources": [source_name],
            })

        print(f"  {source_name}: API page {page_num}, {len(events)} records")
        total_pages = data.get("total_pages")
        if total_pages and page_num >= int(total_pages):
            break
        if len(events) < 50:
            break

    print(f"  {source_name}: {len(items)} Massachusetts events")
    return items


def fetch_tribe_sources():
    items = []
    for source_name, root_url in TRIBE_SOURCES:
        try:
            items.extend(fetch_tribe_source(source_name, root_url))
        except Exception as exc:
            print(f"ERROR loading {source_name}: {exc}")
    return items


SOURCE_PRIORITY = {
    "VisitMA": 0,
    "Cape Cod Chamber": 1,
    "Meet Boston": 2,
    "Discover Central Massachusetts": 3,
    "Explore Western Mass": 4,
    "The Berkshires": 5,
    "North of Boston": 6,
}


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def location_similarity(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.62
    if na in nb or nb in na:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def time_close(a, b) -> bool:
    sa, sb = a["start"], b["start"]
    if not isinstance(sa, datetime) or not isinstance(sb, datetime):
        return True
    return abs((sa - sb).total_seconds()) <= 4 * 3600


def is_duplicate(a, b) -> bool:
    if event_day(a) != event_day(b) or not time_close(a, b):
        return False
    ts = title_similarity(a["title"], b["title"])
    ls = location_similarity(a.get("location", ""), b.get("location", ""))
    if ts >= 0.93:
        return True
    return ts >= 0.82 and ls >= 0.76


def merge_event(existing, incoming):
    if isinstance(incoming["start"], datetime) and not isinstance(existing["start"], datetime):
        existing["start"] = incoming["start"]
        existing["end"] = incoming["end"]
    if len(clean(incoming.get("location"))) > len(clean(existing.get("location"))):
        existing["location"] = incoming["location"]
    if len(clean(incoming.get("description"))) > len(clean(existing.get("description"))):
        existing["description"] = incoming["description"]

    existing_priority = min(SOURCE_PRIORITY.get(s, 99) for s in existing["sources"])
    incoming_priority = min(SOURCE_PRIORITY.get(s, 99) for s in incoming["sources"])
    if incoming_priority < existing_priority and incoming.get("url"):
        existing["url"] = incoming["url"]

    for source in incoming["sources"]:
        if source not in existing["sources"]:
            existing["sources"].append(source)


def dedupe(items):
    items.sort(key=lambda x: (
        event_day(x),
        min(SOURCE_PRIORITY.get(s, 99) for s in x["sources"]),
        norm(x["title"]),
    ))
    kept = []
    duplicates = 0

    for item in items:
        match = None
        for existing in reversed(kept):
            delta = (event_day(item) - event_day(existing)).days
            if delta > 0:
                break
            if is_duplicate(existing, item):
                match = existing
                break
        if match:
            merge_event(match, item)
            duplicates += 1
        else:
            kept.append(item)

    print(f"Deduplicated {duplicates} overlapping events")
    return kept


def build_calendar(items):
    cal = Calendar()
    cal.add("prodid", "-//Combined Massachusetts Events Calendar//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Massachusetts Events")
    cal.add("x-wr-timezone", TZNAME)
    now = datetime.utcnow()

    for item in sorted(items, key=lambda x: (event_day(x), norm(x["title"]))):
        ev = Event()
        uid_seed = f"{event_day(item).isoformat()}|{norm(item['title'])}|{norm(item.get('location', ''))}"
        ev.add("uid", hashlib.sha256(uid_seed.encode()).hexdigest()[:30] + "@massachusetts-events")
        ev.add("dtstamp", now)
        ev.add("summary", item["title"])
        ev.add("dtstart", item["start"])
        ev.add("dtend", item["end"])
        if item.get("location"):
            ev.add("location", item["location"])
        if item.get("url"):
            ev.add("url", item["url"])

        desc = clean(item.get("description", ""))
        source_note = "Sources: " + ", ".join(item["sources"])
        if item.get("url"):
            source_note += f"\nEvent page: {item['url']}"
        if desc:
            desc += "\n\n"
        desc += source_note
        ev.add("description", desc)
        cal.add_component(ev)

    OUTPUT.write_bytes(cal.to_ical())
    print(f"Wrote {OUTPUT} with {len(items)} unique events")


def main():
    all_items = []
    try:
        all_items.extend(fetch_simpleview_sources())
    except Exception as exc:
        print(f"ERROR loading Simpleview sources: {exc}")
    try:
        all_items.extend(fetch_tribe_sources())
    except Exception as exc:
        print(f"ERROR loading Tribe sources: {exc}")

    if not all_items:
        raise RuntimeError("No Massachusetts events were collected from any source.")

    print(f"Collected {len(all_items)} events before deduplication")
    unique = dedupe(all_items)

    if len(unique) < 25:
        raise RuntimeError(
            f"Only {len(unique)} unique Massachusetts events were generated; refusing to publish a suspiciously small feed."
        )

    build_calendar(unique)


if __name__ == "__main__":
    main()
