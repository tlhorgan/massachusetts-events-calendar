from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from icalendar import Calendar, Event

OUTPUT = Path("massachusetts-events.ics")
TODAY = datetime.now().date()
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MassachusettsEventsCalendar/2.1)"}

VISIT_NORTH_CENTRAL = "Visit North Central Massachusetts"
WORCESTER_CITY = "City of Worcester Special Events"
WORCESTER_CHAMBER = "Worcester Regional Chamber of Commerce"


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


def naive(value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def day_of(value):
    return value.date() if isinstance(value, datetime) else value


def future(start, end=None):
    test = end if end is not None else start
    return day_of(test) >= TODAY


def get(url, params=None):
    response = requests.get(url, params=params, headers=HEADERS, timeout=60)
    response.raise_for_status()
    return response


def strip_html(value) -> str:
    return clean(BeautifulSoup(str(value or ""), "html.parser").get_text(" "))


def make_item(source, title, start, end=None, location="", description="", url=""):
    if not clean(title) or start is None:
        return None
    start = naive(start)
    if end is None:
        end = start + (timedelta(hours=2) if isinstance(start, datetime) else timedelta(days=1))
    end = naive(end)
    if not future(start, end):
        return None
    return {
        "source": source,
        "title": clean(title),
        "start": start,
        "end": end,
        "location": clean(location),
        "description": clean(description),
        "url": clean(url),
    }


def walk_jsonld(obj):
    if isinstance(obj, list):
        for child in obj:
            yield from walk_jsonld(child)
    elif isinstance(obj, dict):
        graph = obj.get("@graph")
        if isinstance(graph, list):
            for child in graph:
                yield from walk_jsonld(child)
        yield obj


def jsonld_location(obj) -> str:
    loc = obj.get("location")
    if isinstance(loc, str):
        return clean(loc)
    if not isinstance(loc, dict):
        return ""
    parts = [clean(loc.get("name"))]
    address = loc.get("address")
    if isinstance(address, str):
        parts.append(clean(address))
    elif isinstance(address, dict):
        parts.extend(clean(address.get(key)) for key in (
            "streetAddress", "addressLocality", "addressRegion", "postalCode"
        ))
    return ", ".join(part for part in parts if part)


def parse_jsonld_events(source, html, page_url):
    soup = BeautifulSoup(html, "html.parser")
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
            typ = [typ] if isinstance(typ, str) else typ
            if not any("event" in str(t).lower() for t in typ):
                continue
            try:
                start = naive(dtparser.parse(str(obj.get("startDate"))))
            except Exception:
                continue
            try:
                end = naive(dtparser.parse(str(obj.get("endDate")))) if obj.get("endDate") else None
            except Exception:
                end = None
            item = make_item(
                source,
                obj.get("name"),
                start,
                end,
                jsonld_location(obj),
                strip_html(obj.get("description")),
                obj.get("url") or page_url,
            )
            if item:
                items.append(item)
    return items


def tribe_location(obj) -> str:
    venue = obj.get("venue") or {}
    if not isinstance(venue, dict):
        return ""
    parts = [clean(venue.get(k)) for k in ("venue", "address", "city", "stateprovince", "zip")]
    return ", ".join(part for part in parts if part)


def fetch_visit_north_central():
    source = VISIT_NORTH_CENTRAL
    root = "https://www.visitnorthcentral.com"
    endpoint = root + "/wp-json/tribe/events/v1/events"
    items = []

    # Prefer The Events Calendar REST API when available.
    for page in range(1, 21):
        try:
            data = get(endpoint, {
                "per_page": 50,
                "page": page,
                "start_date": TODAY.isoformat(),
            }).json()
        except Exception as exc:
            if page == 1:
                print(f"  {source}: Tribe API unavailable: {exc}")
            break
        events = data.get("events") or []
        if not events:
            break
        for obj in events:
            try:
                start = naive(dtparser.parse(str(obj.get("start_date"))))
            except Exception:
                continue
            try:
                end = naive(dtparser.parse(str(obj.get("end_date")))) if obj.get("end_date") else None
            except Exception:
                end = None
            if obj.get("all_day"):
                start = start.date()
                if isinstance(end, datetime):
                    end = end.date() + timedelta(days=1)
            item = make_item(
                source,
                strip_html(obj.get("title")),
                start,
                end,
                tribe_location(obj),
                strip_html(obj.get("description")),
                obj.get("url") or root + "/events-calendar/",
            )
            if item:
                items.append(item)
        total_pages = data.get("total_pages")
        if total_pages and page >= int(total_pages):
            break
        if len(events) < 50:
            break

    if items:
        print(f"  {source}: {len(items)} events from Tribe API")
        return items

    # Fallback for sites that render the calendar without exposing Tribe REST.
    calendar_url = root + "/events-calendar/"
    try:
        response = get(calendar_url)
    except Exception as exc:
        print(f"  {source}: calendar page failed: {exc}")
        return []

    items.extend(parse_jsonld_events(source, response.text, calendar_url))
    soup = BeautifulSoup(response.text, "html.parser")
    detail_urls = set()
    for link in soup.find_all("a", href=True):
        href = urljoin(calendar_url, link["href"]).split("#")[0]
        parsed = urlparse(href)
        if parsed.netloc.lower() != "www.visitnorthcentral.com":
            continue
        path = parsed.path.rstrip("/").lower()
        if path == "/events-calendar":
            continue
        if "/event/" in path or "/events/" in path:
            detail_urls.add(href)

    for url in sorted(detail_urls)[:300]:
        try:
            items.extend(parse_jsonld_events(source, get(url).text, url))
        except Exception:
            continue

    print(f"  {source}: {len(items)} events from calendar pages")
    return items


def add_months(d: date, months: int) -> date:
    month_index = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(month_index, 12)
    return date(year, month0 + 1, 1)


def parse_worcester_city_event(url):
    source = WORCESTER_CITY
    response = get(url)
    soup = BeautifulSoup(response.text, "html.parser")
    h1 = soup.find("h1")
    title = clean(h1.get_text(" ", strip=True)) if h1 else ""
    text = clean(soup.get_text(" ", strip=True))

    date_match = re.search(
        r"Date/Time\s+"
        r"((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
        r"[A-Za-z]+\s+\d{1,2},\s+20\d{2}\s*-\s*\d{1,2}(?::\d{2})?\s*(?:am|pm))"
        r"\s+to\s+"
        r"((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
        r"[A-Za-z]+\s+\d{1,2},\s+20\d{2}\s*-\s*\d{1,2}(?::\d{2})?\s*(?:am|pm))",
        text,
        re.I,
    )
    if not date_match:
        single = re.search(
            r"Date/Time\s+((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
            r"[A-Za-z]+\s+\d{1,2},\s+20\d{2}\s*-\s*\d{1,2}(?::\d{2})?\s*(?:am|pm))",
            text,
            re.I,
        )
        if not single:
            return None
        start = dtparser.parse(single.group(1).replace(" - ", " "))
        end = start + timedelta(hours=2)
    else:
        start = dtparser.parse(date_match.group(1).replace(" - ", " "))
        end = dtparser.parse(date_match.group(2).replace(" - ", " "))

    location = ""
    loc_heading = soup.find(lambda tag: tag.name in {"h2", "h3", "h4"} and "event location" in clean(tag.get_text()).lower())
    if loc_heading:
        node = loc_heading.find_next()
        for _ in range(8):
            if not node:
                break
            candidate = clean(node.get_text(" ", strip=True)) if hasattr(node, "get_text") else ""
            if candidate and "event location" not in candidate.lower():
                location = candidate
                break
            node = node.find_next()
    if not location:
        loc_match = re.search(r"Event location\s+(.{3,180}?)(?:\s+Map It|\s+Contact|\s+Event Details|$)", text, re.I)
        if loc_match:
            location = clean(loc_match.group(1))

    meta = soup.find("meta", attrs={"name": "description"})
    description = clean(meta.get("content")) if meta and meta.get("content") else ""
    return make_item(source, title, start, end, location, description, url)


def fetch_worcester_city():
    source = WORCESTER_CITY
    base = "https://www.worcesterma.gov/calendar"
    detail_urls = set()

    for offset in range(12):
        month = add_months(TODAY.replace(day=1), offset)
        stamp = int(datetime(month.year, month.month, 1).timestamp())
        try:
            soup = BeautifulSoup(get(base, {"calendar_timestamp": stamp}).text, "html.parser")
        except Exception as exc:
            print(f"  {source}: month {month:%Y-%m} failed: {exc}")
            continue
        for link in soup.find_all("a", href=True):
            href = urljoin(base, link["href"]).split("?")[0].split("#")[0].rstrip("/")
            parsed = urlparse(href)
            if parsed.netloc.lower() != "www.worcesterma.gov":
                continue
            if re.fullmatch(r"/calendar/[a-z0-9-]+", parsed.path.rstrip("/"), re.I):
                detail_urls.add(href)

    items = []
    for url in sorted(detail_urls):
        try:
            item = parse_worcester_city_event(url)
            if item:
                items.append(item)
        except Exception as exc:
            print(f"  {source}: skip {url}: {exc}")

    print(f"  {source}: {len(items)} events")
    return items


def location_is_mappable(location: str) -> bool:
    value = clean(location)
    low = value.lower()
    return bool(re.search(r"\b\d{1,6}\s+", value)) and (" ma " in f" {low} " or "massachusetts" in low)


def parse_chamber_detail(url):
    source = WORCESTER_CHAMBER
    response = get(url)
    json_items = parse_jsonld_events(source, response.text, url)
    good = [item for item in json_items if location_is_mappable(item.get("location", ""))]
    if good:
        return good

    soup = BeautifulSoup(response.text, "html.parser")
    h1 = soup.find("h1") or soup.find("h2")
    title = clean(h1.get_text(" ", strip=True)) if h1 else ""
    text = clean(soup.get_text(" ", strip=True))

    # GrowthZone pages commonly expose a readable date/time block even without JSON-LD.
    date_match = re.search(
        r"((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December|"
        r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+\d{1,2},?\s+20\d{2})"
        r"(?:\s+(\d{1,2}(?::\d{2})?\s*(?:AM|PM)))?",
        text,
        re.I,
    )
    if not date_match:
        return []
    try:
        start = dtparser.parse(" ".join(x for x in date_match.groups() if x))
    except Exception:
        return []
    end = start + timedelta(hours=2)

    location = ""
    # Prefer a full street address; this intentionally avoids the poorly located pins
    # that can result from venue names or city-only locations.
    address_match = re.search(
        r"([A-Za-z0-9 .&'()#-]*\d{1,6}\s+[A-Za-z0-9 .&'()#-]+(?:Street|St\.?|Road|Rd\.?|Avenue|Ave\.?|"
        r"Drive|Dr\.?|Lane|Ln\.?|Way|Boulevard|Blvd\.?|Turnpike|Tpke\.?|Highway|Hwy\.?)[^|]{0,120}?"
        r"(?:,\s*)?[A-Za-z .'-]+,\s*MA\s*\d{5})",
        text,
        re.I,
    )
    if address_match:
        location = clean(address_match.group(1))
    if not location_is_mappable(location):
        return []

    meta = soup.find("meta", attrs={"name": "description"})
    description = clean(meta.get("content")) if meta and meta.get("content") else ""
    item = make_item(source, title, start, end, location, description, url)
    return [item] if item else []


def fetch_worcester_chamber():
    source = WORCESTER_CHAMBER
    base = "https://business.worcesterchamber.org/events"
    end = TODAY + timedelta(days=365)
    try:
        response = get(base, {
            "from": TODAY.strftime("%-m/%-d/%Y"),
            "to": end.strftime("%-m/%-d/%Y"),
            "d": 1,
        })
    except ValueError:
        # Windows-compatible fallback, harmless on Linux/GitHub Actions.
        response = get(base, {
            "from": f"{TODAY.month}/{TODAY.day}/{TODAY.year}",
            "to": f"{end.month}/{end.day}/{end.year}",
            "d": 1,
        })
    except Exception as exc:
        print(f"  {source}: listing failed: {exc}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    detail_urls = set()
    for link in soup.find_all("a", href=True):
        href = urljoin(base, link["href"]).split("#")[0]
        parsed = urlparse(href)
        if parsed.netloc.lower() != "business.worcesterchamber.org":
            continue
        if "/events/details/" in parsed.path.lower():
            detail_urls.add(href)

    items = []
    for url in sorted(detail_urls)[:400]:
        try:
            items.extend(parse_chamber_detail(url))
        except Exception as exc:
            print(f"  {source}: skip {url}: {exc}")

    print(f"  {source}: {len(items)} mappable events")
    return items


def component_start(component):
    try:
        return naive(component.decoded("DTSTART"))
    except Exception:
        return None


def existing_records(cal):
    records = []
    for component in cal.walk("VEVENT"):
        start = component_start(component)
        if start is None:
            continue
        records.append({
            "day": day_of(start),
            "title": norm(component.get("SUMMARY", "")),
            "location": norm(component.get("LOCATION", "")),
        })
    return records


def duplicate(item, records):
    title = norm(item["title"])
    location = norm(item.get("location", ""))
    day = day_of(item["start"])
    for record in records:
        if record["day"] != day:
            continue
        similarity = SequenceMatcher(None, title, record["title"]).ratio()
        if similarity >= 0.94:
            return True
        if similarity >= 0.84 and location and record["location"]:
            loc_similarity = SequenceMatcher(None, location, record["location"]).ratio()
            if loc_similarity >= 0.75:
                return True
    return False


def append_item(cal, item):
    event = Event()
    seed = f"{day_of(item['start']).isoformat()}|{norm(item['title'])}|{norm(item.get('location', ''))}"
    event.add("uid", hashlib.sha256(seed.encode()).hexdigest()[:30] + "@central-massachusetts-events")
    event.add("dtstamp", datetime.utcnow())
    event.add("summary", item["title"])
    event.add("dtstart", item["start"])
    event.add("dtend", item["end"])
    if item.get("location"):
        event.add("location", item["location"])
    if item.get("url"):
        event.add("url", item["url"])
    description = clean(item.get("description"))
    source_note = f"Source: {item['source']}"
    if item.get("url"):
        source_note += f"\nEvent page: {item['url']}"
    event.add("description", (description + "\n\n" if description else "") + source_note)
    cal.add_component(event)


def main():
    if not OUTPUT.exists():
        raise RuntimeError(f"{OUTPUT} does not exist; generate the Massachusetts calendar first")

    cal = Calendar.from_ical(OUTPUT.read_bytes())
    records = existing_records(cal)
    candidates = []

    for fetcher in (fetch_visit_north_central, fetch_worcester_city, fetch_worcester_chamber):
        try:
            candidates.extend(fetcher())
        except Exception as exc:
            print(f"ERROR loading Central Massachusetts source: {exc}")

    added = 0
    skipped = 0
    for item in sorted(candidates, key=lambda x: (day_of(x["start"]), norm(x["title"]))):
        if duplicate(item, records):
            skipped += 1
            continue
        append_item(cal, item)
        records.append({
            "day": day_of(item["start"]),
            "title": norm(item["title"]),
            "location": norm(item.get("location", "")),
        })
        added += 1

    OUTPUT.write_bytes(cal.to_ical())
    print(f"Central Massachusetts: added {added} events; skipped {skipped} duplicates")


if __name__ == "__main__":
    main()
