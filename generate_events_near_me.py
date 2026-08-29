from __future__ import annotations

import hashlib
import json
import math
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
CALENDAR_NAME = "Events Near Me"
TZNAME = "America/New_York"
LOOKAHEAD_DAYS = 120
MAX_RESULTS_PER_QUERY = 25
MAX_PAGES_TO_PARSE = 80
RADIUS_MILES = 30.0
CENTER_LABEL = "Northborough, MA 01532"
CENTER_LAT = 42.3195
CENTER_LON = -71.6412

# Approximate municipal-center coordinates used to apply a true distance test
# rather than relying on a hand-maintained yes/no town list. Locations near the
# edge of the radius are approximate because events are tested by municipality
# center when only a town/city name is available.
TOWN_CENTERS = {
    "northborough": (42.3195, -71.6412),
    "westborough": (42.2695, -71.6162),
    "southborough": (42.3057, -71.5245),
    "marlborough": (42.3459, -71.5523),
    "hudson": (42.3918, -71.5662),
    "berlin": (42.3812, -71.6370),
    "boylston": (42.3518, -71.7312),
    "west boylston": (42.3668, -71.7856),
    "shrewsbury": (42.2959, -71.7128),
    "grafton": (42.2070, -71.6856),
    "hopkinton": (42.2287, -71.5226),
    "upton": (42.1745, -71.6023),
    "ashland": (42.2612, -71.4634),
    "framingham": (42.2793, -71.4162),
    "worcester": (42.2626, -71.8023),
    "clinton": (42.4168, -71.6828),
    "bolton": (42.4334, -71.6078),
    "stow": (42.4370, -71.5056),
    "maynard": (42.4334, -71.4495),
    "acton": (42.4851, -71.4328),
    "sudbury": (42.3834, -71.4162),
    "wayland": (42.3626, -71.3615),
    "natick": (42.2834, -71.3495),
    "sherborn": (42.2387, -71.3698),
    "holliston": (42.2001, -71.4245),
    "milford": (42.1398, -71.5162),
    "hopedale": (42.1307, -71.5412),
    "mendon": (42.1057, -71.5523),
    "millbury": (42.1934, -71.7601),
    "sutton": (42.1501, -71.7628),
    "auburn": (42.1945, -71.8356),
    "leicester": (42.2459, -71.9087),
    "paxton": (42.3112, -71.9281),
    "holden": (42.3518, -71.8634),
    "rutland": (42.3695, -71.9481),
    "sterling": (42.4376, -71.7606),
    "lancaster": (42.4557, -71.6737),
    "harvard": (42.5001, -71.5823),
    "littleton": (42.5376, -71.5120),
    "boxborough": (42.4834, -71.5162),
    "concord": (42.4604, -71.3489),
    "lincoln": (42.4259, -71.3039),
    "weston": (42.3668, -71.3031),
    "wellesley": (42.2968, -71.2924),
    "needham": (42.2834, -71.2328),
    "newton": (42.3370, -71.2092),
    "waltham": (42.3765, -71.2356),
    "medway": (42.1418, -71.3967),
    "millis": (42.1676, -71.3578),
    "franklin": (42.0834, -71.3967),
    "bellingham": (42.0868, -71.4751),
    "northbridge": (42.1515, -71.6495),
    "uxbridge": (42.0773, -71.6301),
    "douglas": (42.0543, -71.7395),
    "oxford": (42.1168, -71.8648),
    "charlton": (42.1357, -71.9701),
    "spencer": (42.2439, -71.9923),
    "leominster": (42.5251, -71.7598),
    "fitchburg": (42.5834, -71.8023),
    "princeton": (42.4487, -71.8773),
    "westminster": (42.5459, -71.9109),
    "gardner": (42.5751, -71.9981),
    "ayer": (42.5612, -71.5898),
    "shirley": (42.5437, -71.6495),
    "groton": (42.6112, -71.5745),
}

LOCAL_FEEDS = [
    Path("northborough-events.ics"),
    Path("massachusetts-events.ics"),
    Path("central-massachusetts-events.ics"),
    Path("worcester-colleges-events.ics"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0 Safari/537.36"
    )
}

SEARCH_QUERIES = [
    f"events within {int(RADIUS_MILES)} miles of {CENTER_LABEL}",
    f"live music within {int(RADIUS_MILES)} miles of {CENTER_LABEL}",
    f"festivals fairs within {int(RADIUS_MILES)} miles of {CENTER_LABEL}",
    f"community events within {int(RADIUS_MILES)} miles of {CENTER_LABEL}",
    f"things to do this weekend within {int(RADIUS_MILES)} miles of {CENTER_LABEL}",
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


def distance_miles(lat1, lon1, lat2, lon2) -> float:
    radius = 3958.7613
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def location_town(location: str):
    value = norm(location)
    if not value:
        return None
    # Match longer municipality names first (e.g. West Boylston before Boylston).
    for town in sorted(TOWN_CENTERS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(town)}\b", value):
            return town
    return None


def location_is_local(location: str) -> bool:
    town = location_town(location)
    if town is None:
        return False
    lat, lon = TOWN_CENTERS[town]
    return distance_miles(CENTER_LAT, CENTER_LON, lat, lon) <= RADIUS_MILES


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

    location = extract_location(obj)
    if not location_is_local(location):
        return None

    event_url = obj.get("url")
    if isinstance(event_url, dict):
        event_url = event_url.get("url")
    event_url = clean(event_url) or source_page

    return {
        "title": title,
        "start": start,
        "end": end,
        "location": location,
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
            # source omits its location. Other feeds must resolve to a municipality
            # whose center is within the configured radius.
            if feed_path.name != "northborough-events.ics" and not location_is_local(location):
                continue

            if not location and feed_path.name == "northborough-events.ics":
                location = CENTER_LABEL

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

        print(f"  fallback {feed_path.name}: {added} events within {RADIUS_MILES:g} miles")

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
        note = f"Events Near Me: within {RADIUS_MILES:g} miles of {CENTER_LABEL}\nDiscovery: {discovery}"
        if item.get("source_page"):
            note += f"\nSource page: {item['source_page']}"
        if description:
            description += "\n\n"
        ev.add("description", description + note)
        cal.add_component(ev)

    OUTPUT.write_bytes(cal.to_ical())
    print(f"Wrote {OUTPUT} with {len(items)} unique events within {RADIUS_MILES:g} miles")


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
    print(f"Parsed {len(items)} Google candidate events; {len(unique)} unique future events within radius")

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
