from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from icalendar import Calendar, Event
from playwright.sync_api import sync_playwright


OUTPUT = Path("events-near-me.ics")
HOME_ADDRESS = "17 Reservoir St, Northborough, MA 01532"
CALENDAR_NAME = "Events Near Me"
TZNAME = "America/New_York"
LOOKAHEAD_DAYS = 120
MAX_RESULTS_PER_QUERY = 25
MAX_PAGES_TO_PARSE = 80

# When Google blocks a cloud runner, build a useful local feed from the calendars
# generated earlier in the same workflow. These communities are roughly within
# a 15-20 mile local-events radius of the Northborough home address.
LOCAL_TOWNS = {
    "northborough",
    "westborough",
    "southborough",
    "marlborough",
    "hudson",
    "berlin",
    "boylston",
    "west boylston",
    "shrewsbury",
    "grafton",
    "hopkinton",
    "upton",
    "ashland",
    "framingham",
    "worcester",
}
LOCAL_FEEDS = [
    Path("northborough-events.ics"),
    Path("massachusetts-events.ics"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0 Safari/537.36"
    )
}

# Google remains the primary discovery source.
SEARCH_QUERIES = [
    f"events near {HOME_ADDRESS}",
    f"live music near {HOME_ADDRESS}",
    f"festivals fairs near {HOME_ADDRESS}",
    f"community events near {HOME_ADDRESS}",
    f"things to do this weekend near {HOME_ADDRESS}",
]

GOOGLE_HOSTS = {
    "google.com",
    "www.google.com",
    "maps.google.com",
    "accounts.google.com",
    "support.google.com",
}


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


def naive(value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def event_day(item) -> date:
    start = item["start"]
    return start.date() if isinstance(start, datetime) else start


def unwrap_google_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("/url?"):
        query = parse_qs(urlparse(href).query)
        return (query.get("q") or query.get("url") or [""])[0]
    return href


def external_search_links(page, query: str):
    url = "https://www.google.com/search?q=" + quote_plus(query)
    print(f"Google search: {query}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(1800)
    except Exception as exc:
        print(f"  Google load warning: {exc}")

    text = clean(page.locator("body").inner_text()) if page.locator("body").count() else ""
    low = text.lower()
    if "unusual traffic" in low or "our systems have detected" in low or "recaptcha" in low:
        raise RuntimeError("Google blocked the automated search request")

    links = []
    seen = set()
    for href in page.locator("a[href]").evaluate_all("els => els.map(e => e.getAttribute('href'))"):
        target = unwrap_google_url(href or "")
        if not target.startswith("http"):
            continue
        host = urlparse(target).netloc.lower().split(":")[0]
        if host in GOOGLE_HOSTS or host.endswith(".google.com"):
            continue
        if any(x in host for x in ("gstatic.com", "googleusercontent.com", "youtube.com")):
            continue
        target = target.split("#")[0]
        if target not in seen:
            seen.add(target)
            links.append(target)
        if len(links) >= MAX_RESULTS_PER_QUERY:
            break

    print(f"  {len(links)} external result links")
    return links


def walk_jsonld(obj):
    if isinstance(obj, list):
        for value in obj:
            yield from walk_jsonld(value)
    elif isinstance(obj, dict):
        graph = obj.get("@graph")
        if isinstance(graph, list):
            for value in graph:
                yield from walk_jsonld(value)
        yield obj


def extract_location(obj) -> str:
    loc = obj.get("location")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, str):
        return clean(loc)
    if not isinstance(loc, dict):
        return ""

    parts = []
    if clean(loc.get("name")):
        parts.append(clean(loc.get("name")))
    address = loc.get("address")
    if isinstance(address, str):
        parts.append(clean(address))
    elif isinstance(address, dict):
        for key in ("streetAddress", "addressLocality", "addressRegion", "postalCode"):
            if clean(address.get(key)):
                parts.append(clean(address.get(key)))
    return ", ".join(dict.fromkeys(parts))


def extract_description(obj) -> str:
    value = obj.get("description", "")
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return clean(BeautifulSoup(str(value), "html.parser").get_text(" "))


def parse_event_object(obj, source_page: str):
    if not isinstance(obj, dict):
        return None
    typ = obj.get("@type", [])
    if isinstance(typ, str):
        typ = [typ]
    if not any("event" in str(t).lower() for t in typ):
        return None

    title = clean(obj.get("name"))
    start_raw = obj.get("startDate")
    if not title or not start_raw:
        return None

    try:
        start = naive(dtparser.parse(str(start_raw)))
    except Exception:
        return None

    end = None
    if obj.get("endDate"):
        try:
            end = naive(dtparser.parse(str(obj.get("endDate"))))
        except Exception:
            pass
    if end is None:
        end = start + timedelta(hours=2)

    today = datetime.now().date()
    day = start.date() if isinstance(start, datetime) else start
    if day < today or day > today + timedelta(days=LOOKAHEAD_DAYS):
        return None

    event_url = obj.get("url")
    if isinstance(event_url, dict):
        event_url = event_url.get("url")
    event_url = clean(event_url) or source_page

    return {
        "title": title,
        "start": start,
        "end": end,
        "location": extract_location(obj),
        "description": extract_description(obj),
        "url": event_url,
        "source_page": source_page,
        "discovery": "Google Search",
    }


def parse_event_page(url: str):
    try:
        response = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
        response.raise_for_status()
    except Exception as exc:
        print(f"  skip {url}: {exc}")
        return []

    content_type = response.headers.get("content-type", "").lower()
    if "html" not in content_type and "xml" not in content_type and not content_type.startswith("text/"):
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    items = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for obj in walk_jsonld(data):
            item = parse_event_object(obj, response.url)
            if item:
                items.append(item)
    return items


def location_is_local(location: str) -> bool:
    value = norm(location)
    if not value:
        return False
    return any(re.search(rf"\b{re.escape(town)}\b", value) for town in LOCAL_TOWNS)


def local_fallback_items():
    """Read already-generated local/state feeds when Google blocks the runner."""
    today = datetime.now().date()
    last_day = today + timedelta(days=LOOKAHEAD_DAYS)
    items = []

    for feed_path in LOCAL_FEEDS:
        if not feed_path.exists() or feed_path.stat().st_size < 100:
            print(f"  fallback feed unavailable: {feed_path}")
            continue

        try:
            cal = Calendar.from_ical(feed_path.read_bytes())
        except Exception as exc:
            print(f"  fallback could not read {feed_path}: {exc}")
            continue

        added = 0
        for component in cal.walk("VEVENT"):
            try:
                start = naive(component.decoded("DTSTART"))
            except Exception:
                continue
            try:
                end = naive(component.decoded("DTEND")) if component.get("DTEND") else None
            except Exception:
                end = None

            day = start.date() if isinstance(start, datetime) else start
            if day < today or day > last_day:
                continue

            location = clean(component.get("LOCATION", ""))
            # Every event in the dedicated Northborough feed is local even if a
            # source omits its location. Statewide events must name a nearby town.
            if feed_path.name != "northborough-events.ics" and not location_is_local(location):
                continue

            if not location and feed_path.name == "northborough-events.ics":
                location = "Northborough, MA 01532"

            title = clean(component.get("SUMMARY", ""))
            if not title:
                continue
            if end is None:
                end = start + (timedelta(hours=2) if isinstance(start, datetime) else timedelta(days=1))

            url = clean(component.get("URL", ""))
            description = clean(component.get("DESCRIPTION", ""))
            items.append({
                "title": title,
                "start": start,
                "end": end,
                "location": location,
                "description": description,
                "url": url,
                "source_page": url or feed_path.name,
                "discovery": f"Local fallback from {feed_path.name}",
            })
            added += 1

        print(f"  fallback {feed_path.name}: {added} nearby events")

    return items


def duplicate(a, b) -> bool:
    if event_day(a) != event_day(b):
        return False
    ta = norm(a["title"])
    tb = norm(b["title"])
    if ta == tb:
        return True
    if ta in tb or tb in ta:
        shorter = min(len(ta), len(tb))
        longer = max(len(ta), len(tb))
        return shorter >= 12 and shorter / max(longer, 1) >= 0.82
    return False


def dedupe(items):
    kept = []
    for item in sorted(items, key=lambda x: (event_day(x), norm(x["title"]))):
        match = next((existing for existing in kept if duplicate(existing, item)), None)
        if match:
            if len(item.get("location", "")) > len(match.get("location", "")):
                match["location"] = item["location"]
            if len(item.get("description", "")) > len(match.get("description", "")):
                match["description"] = item["description"]
            continue
        kept.append(item)
    return kept


def build_calendar(items):
    cal = Calendar()
    cal.add("prodid", "-//Events Near Me//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", CALENDAR_NAME)
    cal.add("x-wr-timezone", TZNAME)
    now = datetime.utcnow()

    for item in sorted(items, key=lambda x: (event_day(x), norm(x["title"]))):
        ev = Event()
        seed = f"{event_day(item).isoformat()}|{norm(item['title'])}|{norm(item.get('location'))}"
        ev.add("uid", hashlib.sha256(seed.encode()).hexdigest()[:30] + "@events-near-me")
        ev.add("dtstamp", now)
        ev.add("summary", item["title"])
        ev.add("dtstart", item["start"])
        ev.add("dtend", item["end"])
        if item.get("location"):
            ev.add("location", item["location"])
        if item.get("url"):
            ev.add("url", item["url"])

        description = clean(item.get("description"))
        discovery = item.get("discovery", "Google Search")
        note = f"Events Near Me centered on: {HOME_ADDRESS}\nDiscovery: {discovery}"
        if item.get("source_page"):
            note += f"\nSource page: {item['source_page']}"
        if description:
            description += "\n\n"
        ev.add("description", description + note)
        cal.add_component(ev)

    OUTPUT.write_bytes(cal.to_ical())
    print(f"Wrote {OUTPUT} with {len(items)} unique events")


def main():
    discovered_links = []
    seen_links = set()
    google_blocked = False

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                viewport={"width": 1440, "height": 1200},
                user_agent=HEADERS["User-Agent"],
                locale="en-US",
            )
            for query in SEARCH_QUERIES:
                try:
                    for link in external_search_links(page, query):
                        if link not in seen_links:
                            seen_links.add(link)
                            discovered_links.append(link)
                except Exception as exc:
                    print(f"  search failed: {exc}")
                    if "blocked" in str(exc).lower():
                        google_blocked = True
            browser.close()
    except Exception as exc:
        print(f"Google discovery failed: {exc}")
        google_blocked = True

    print(f"Collected {len(discovered_links)} unique Google result links")
    items = []
    for url in discovered_links[:MAX_PAGES_TO_PARSE]:
        items.extend(parse_event_page(url))

    unique = dedupe(items)
    print(f"Parsed {len(items)} Google candidate events; {len(unique)} unique future events")

    if not unique:
        if google_blocked:
            print("Google blocked the GitHub Actions runner; using local-calendar fallback")
        else:
            print("Google returned no usable future events; using local-calendar fallback")
        unique = dedupe(local_fallback_items())
        print(f"Fallback produced {len(unique)} unique nearby events")

    if not unique:
        if OUTPUT.exists() and OUTPUT.stat().st_size > 100:
            print("No new events found; preserving the existing Events Near Me feed")
            return
        raise RuntimeError("No usable Events Near Me events were available from Google or local fallback feeds.")

    build_calendar(unique)


if __name__ == "__main__":
    main()
