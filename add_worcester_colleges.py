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
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MassachusettsEventsCalendar/2.0)"}

# Worcester's official city site lists these eight institutions in the city.
COLLEGE_ADDRESSES = {
    "Assumption University": "Assumption University, 500 Salisbury St, Worcester, MA 01609",
    "Clark University": "Clark University, 950 Main St, Worcester, MA 01610",
    "College of the Holy Cross": "College of the Holy Cross, 1 College St, Worcester, MA 01610",
    "MCPHS University - Worcester": "MCPHS University, 19 Foster St, Worcester, MA 01608",
    "Quinsigamond Community College": "Quinsigamond Community College, 670 W Boylston St, Worcester, MA 01606",
    "UMass Chan Medical School": "UMass Chan Medical School, 55 Lake Ave N, Worcester, MA 01655",
    "Worcester Polytechnic Institute": "Worcester Polytechnic Institute, 100 Institute Rd, Worcester, MA 01609",
    "Worcester State University": "Worcester State University, 486 Chandler St, Worcester, MA 01602",
}

TRIBE_COLLEGES = [
    ("Assumption University", "https://www.assumption.edu"),
    ("Clark University", "https://www.clarku.edu"),
    ("Worcester State University", "https://www.worcester.edu"),
]

ICS_COLLEGES = [
    (
        "College of the Holy Cross",
        "https://myhc.holycross.edu/ical/holycross/ical_holycross.ics",
    ),
    (
        "Worcester Polytechnic Institute",
        "https://mywpi.wpi.edu/ical/wpi/ical_wpi.ics",
    ),
]


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


def event_is_future(start, end=None):
    test = end if end is not None else start
    return day_of(test) >= TODAY


def make_item(source, title, start, end, location="", description="", url=""):
    if not title or start is None:
        return None
    if end is None:
        end = start + (timedelta(hours=2) if isinstance(start, datetime) else timedelta(days=1))
    if not event_is_future(start, end):
        return None
    return {
        "source": source,
        "title": clean(title),
        "start": naive(start),
        "end": naive(end),
        "location": clean(location) or COLLEGE_ADDRESSES[source],
        "description": clean(description),
        "url": clean(url),
    }


def fetch_tribe(source, root):
    endpoint = root.rstrip("/") + "/wp-json/tribe/events/v1/events"
    items = []
    for page in range(1, 21):
        try:
            r = requests.get(
                endpoint,
                params={"per_page": 50, "page": page, "start_date": TODAY.isoformat()},
                headers=HEADERS,
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            print(f"  {source}: Tribe API page {page} failed: {exc}")
            break

        events = data.get("events") or []
        if not events:
            break

        for obj in events:
            try:
                start = naive(dtparser.parse(obj.get("start_date")))
            except Exception:
                continue
            try:
                end = naive(dtparser.parse(obj.get("end_date"))) if obj.get("end_date") else start + timedelta(hours=2)
            except Exception:
                end = start + timedelta(hours=2)

            if obj.get("all_day"):
                start = start.date()
                end = (end.date() if isinstance(end, datetime) else end) + timedelta(days=1)

            venue = obj.get("venue") or {}
            parts = []
            if isinstance(venue, dict):
                for key in ("venue", "address", "city", "stateprovince", "zip"):
                    if clean(venue.get(key)):
                        parts.append(clean(venue.get(key)))
            location = ", ".join(parts)

            title = BeautifulSoup(str(obj.get("title", "")), "html.parser").get_text(" ")
            description = BeautifulSoup(str(obj.get("description", "")), "html.parser").get_text(" ")
            item = make_item(
                source,
                title,
                start,
                end,
                location,
                description,
                obj.get("url") or root,
            )
            if item:
                items.append(item)

        total_pages = data.get("total_pages")
        if total_pages and page >= int(total_pages):
            break
        if len(events) < 50:
            break

    print(f"  {source}: {len(items)} events")
    return items


def fetch_ics(source, url):
    items = []
    try:
        r = requests.get(url, headers=HEADERS, timeout=60)
        r.raise_for_status()
        cal = Calendar.from_ical(r.content)
    except Exception as exc:
        print(f"  {source}: ICS failed: {exc}")
        return items

    for component in cal.walk("VEVENT"):
        try:
            start = naive(component.decoded("DTSTART"))
        except Exception:
            continue
        try:
            end = naive(component.decoded("DTEND")) if component.get("DTEND") else None
        except Exception:
            end = None

        # Keep recurring entries even when DTSTART is earlier than today; the RRULE
        # may still generate future occurrences in subscribing calendars.
        if not component.get("RRULE") and not event_is_future(start, end):
            continue

        item = make_item(
            source,
            component.get("SUMMARY", ""),
            start,
            end,
            component.get("LOCATION", ""),
            component.get("DESCRIPTION", ""),
            component.get("URL", "") or url,
        )
        if item:
            item["rrule"] = component.get("RRULE")
            items.append(item)

    print(f"  {source}: {len(items)} events from ICS")
    return items


def fetch_mcphs():
    source = "MCPHS University - Worcester"
    endpoint = "https://events.mcphs.edu/api/2/events"
    items = []

    for page in range(1, 21):
        try:
            r = requests.get(
                endpoint,
                params={"days": 365, "pp": 100, "page": page},
                headers=HEADERS,
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            print(f"  {source}: Localist API page {page} failed: {exc}")
            break

        wrappers = data.get("events") or []
        if not wrappers:
            break

        for wrapper in wrappers:
            obj = wrapper.get("event", wrapper) if isinstance(wrapper, dict) else {}
            location_text = " ".join(
                clean(obj.get(k)) for k in ("location_name", "address", "title") if clean(obj.get(k))
            )
            if "worcester" not in location_text.lower():
                continue

            instances = obj.get("event_instances") or []
            if not instances and obj.get("date"):
                instances = [{"event_instance": {"start": obj.get("date"), "end": obj.get("date")}}]

            for wrapper_instance in instances:
                inst = wrapper_instance.get("event_instance", wrapper_instance)
                try:
                    start = naive(dtparser.parse(str(inst.get("start"))))
                except Exception:
                    continue
                try:
                    end = naive(dtparser.parse(str(inst.get("end")))) if inst.get("end") else start + timedelta(hours=2)
                except Exception:
                    end = start + timedelta(hours=2)

                location = ", ".join(
                    x for x in [clean(obj.get("location_name")), clean(obj.get("address"))] if x
                )
                item = make_item(
                    source,
                    obj.get("title"),
                    start,
                    end,
                    location,
                    obj.get("description_text") or obj.get("description"),
                    obj.get("localist_url") or obj.get("url") or "https://events.mcphs.edu/worcester_campus",
                )
                if item:
                    items.append(item)

        page_info = data.get("page") or {}
        total = page_info.get("total") if isinstance(page_info, dict) else None
        if len(wrappers) < 100 or (total and page * 100 >= int(total)):
            break

    print(f"  {source}: {len(items)} Worcester events")
    return items


def jsonld_events(soup, source, fallback_url):
    items = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        queue = data if isinstance(data, list) else [data]
        expanded = []
        for obj in queue:
            if isinstance(obj, dict) and isinstance(obj.get("@graph"), list):
                expanded.extend(obj["@graph"])
            expanded.append(obj)

        for obj in expanded:
            if not isinstance(obj, dict):
                continue
            typ = obj.get("@type", [])
            typ = [typ] if isinstance(typ, str) else typ
            if not any("event" in str(x).lower() for x in typ):
                continue
            if not obj.get("name") or not obj.get("startDate"):
                continue
            try:
                start = naive(dtparser.parse(str(obj.get("startDate"))))
            except Exception:
                continue
            try:
                end = naive(dtparser.parse(str(obj.get("endDate")))) if obj.get("endDate") else start + timedelta(hours=2)
            except Exception:
                end = start + timedelta(hours=2)

            location = ""
            loc = obj.get("location")
            if isinstance(loc, dict):
                parts = [clean(loc.get("name"))]
                addr = loc.get("address")
                if isinstance(addr, dict):
                    parts += [clean(addr.get(k)) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode")]
                elif isinstance(addr, str):
                    parts.append(clean(addr))
                location = ", ".join(x for x in parts if x)
            elif isinstance(loc, str):
                location = clean(loc)

            item = make_item(
                source,
                obj.get("name"),
                start,
                end,
                location,
                BeautifulSoup(str(obj.get("description", "")), "html.parser").get_text(" "),
                obj.get("url") or fallback_url,
            )
            if item:
                items.append(item)
    return items


def fetch_qcc():
    source = "Quinsigamond Community College"
    root = "https://www.qcc.edu/events"
    urls = set()

    for page in range(0, 25):
        try:
            r = requests.get(root, params={"page": page}, headers=HEADERS, timeout=60)
            r.raise_for_status()
        except Exception as exc:
            print(f"  {source}: listing page {page} failed: {exc}")
            break
        soup = BeautifulSoup(r.text, "html.parser")
        before = len(urls)
        for a in soup.find_all("a", href=True):
            href = urljoin(root, a["href"]).split("#")[0]
            p = urlparse(href)
            if p.netloc == "www.qcc.edu" and p.path.startswith("/events/") and p.path.rstrip("/") != "/events":
                urls.add(href)
        if len(urls) == before and page > 1:
            break

    items = []
    for url in sorted(urls):
        try:
            r = requests.get(url, headers=HEADERS, timeout=45)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            parsed = jsonld_events(soup, source, url)
            if parsed:
                items.extend(parsed)
                continue

            h1 = soup.find("h1")
            title = clean(h1.get_text(" ", strip=True)) if h1 else ""
            text = clean(soup.get_text(" ", strip=True))
            match = re.search(
                r"([A-Za-z]+[- ]\d{1,2}[-, ]+20\d{2}).{0,120}?"
                r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s*[-–—]\s*"
                r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))",
                text,
                re.I,
            )
            if title and match:
                d = dtparser.parse(match.group(1), fuzzy=True).date()
                start = datetime.combine(d, dtparser.parse(match.group(2)).time())
                end = datetime.combine(d, dtparser.parse(match.group(3)).time())
                if end <= start:
                    end += timedelta(days=1)
                item = make_item(source, title, start, end, "", "", url)
                if item:
                    items.append(item)
        except Exception as exc:
            print(f"  {source}: skip {url}: {exc}")

    print(f"  {source}: {len(items)} events")
    return items


def parse_visible_umass_event(url):
    source = "UMass Chan Medical School"
    try:
        r = requests.get(url, headers=HEADERS, timeout=45)
        r.raise_for_status()
    except Exception:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    parsed = jsonld_events(soup, source, url)
    if parsed:
        return parsed

    h1 = soup.find("h1")
    title = clean(h1.get_text(" ", strip=True)) if h1 else ""
    if not title:
        return []
    text = clean(soup.get_text(" ", strip=True))
    match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December|"
        r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})"
        r"(?:.{0,120}?(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)))?"
        r"(?:\s*[-–—to]+\s*(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)))?",
        text,
        re.I,
    )
    if not match:
        return []
    try:
        d = dtparser.parse(f"{match.group(1)} {match.group(2)}, {match.group(3)}").date()
        if match.group(4):
            start = datetime.combine(d, dtparser.parse(match.group(4).replace(".", "")).time())
            if match.group(5):
                end = datetime.combine(d, dtparser.parse(match.group(5).replace(".", "")).time())
                if end <= start:
                    end += timedelta(days=1)
            else:
                end = start + timedelta(hours=2)
        else:
            start = d
            end = d + timedelta(days=1)
    except Exception:
        return []

    meta = soup.find("meta", attrs={"name": "description"})
    item = make_item(source, title, start, end, "", meta.get("content") if meta else "", url)
    return [item] if item else []


def fetch_umass_chan():
    source = "UMass Chan Medical School"
    hubs = [
        "https://www.umassmed.edu/universityevents/",
        "https://www.umassmed.edu/academy-of-educators/events/",
        "https://www.umassmed.edu/studentlife/student-organizations/event-calendar/",
    ]
    urls = set(hubs)
    for hub in hubs:
        try:
            r = requests.get(hub, headers=HEADERS, timeout=45)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as exc:
            print(f"  {source}: hub failed {hub}: {exc}")
            continue
        for a in soup.find_all("a", href=True):
            url = urljoin(hub, a["href"]).split("#")[0]
            p = urlparse(url)
            if p.netloc != "www.umassmed.edu":
                continue
            low = p.path.lower()
            if "/events/" in low or low.startswith("/universityevents/"):
                urls.add(url)

    items = []
    for url in sorted(urls)[:120]:
        items.extend(parse_visible_umass_event(url))

    print(f"  {source}: {len(items)} public events")
    return items


def fetch_all_college_events():
    items = []
    for source, root in TRIBE_COLLEGES:
        print(f"Fetching {source}...")
        items.extend(fetch_tribe(source, root))
    for source, url in ICS_COLLEGES:
        print(f"Fetching {source}...")
        items.extend(fetch_ics(source, url))

    print("Fetching MCPHS University - Worcester...")
    items.extend(fetch_mcphs())
    print("Fetching Quinsigamond Community College...")
    items.extend(fetch_qcc())
    print("Fetching UMass Chan Medical School...")
    items.extend(fetch_umass_chan())
    return items


def existing_signature(component):
    try:
        start = naive(component.decoded("DTSTART"))
    except Exception:
        return None
    return (day_of(start), norm(component.get("SUMMARY", "")), norm(component.get("LOCATION", "")))


def is_duplicate(item, existing_sigs):
    d = day_of(item["start"])
    title = norm(item["title"])
    loc = norm(item["location"])
    for eday, etitle, eloc in existing_sigs:
        if d != eday:
            continue
        title_score = SequenceMatcher(None, title, etitle).ratio()
        if title_score >= 0.94:
            return True
        if title_score >= 0.84 and loc and eloc:
            loc_score = SequenceMatcher(None, loc, eloc).ratio()
            if loc_score >= 0.72:
                return True
    return False


def append_college_events(items):
    if not OUTPUT.exists():
        raise RuntimeError(f"{OUTPUT} does not exist; generate the Massachusetts feed first")

    cal = Calendar.from_ical(OUTPUT.read_bytes())
    sigs = []
    for component in cal.walk("VEVENT"):
        sig = existing_signature(component)
        if sig:
            sigs.append(sig)

    added = 0
    source_counts = {}
    for item in sorted(items, key=lambda x: (day_of(x["start"]), norm(x["title"]))):
        if is_duplicate(item, sigs):
            continue

        ev = Event()
        uid_seed = f"{item['source']}|{item['title']}|{item['start']}|{item['location']}"
        ev.add("uid", hashlib.sha256(uid_seed.encode()).hexdigest()[:30] + "@worcester-colleges")
        ev.add("dtstamp", datetime.utcnow())
        ev.add("summary", item["title"])
        ev.add("dtstart", item["start"])
        ev.add("dtend", item["end"])
        if item["location"]:
            ev.add("location", item["location"])
        if item["url"]:
            ev.add("url", item["url"])
        if item.get("rrule"):
            ev.add("rrule", item["rrule"])

        description = item["description"]
        source_note = f"Source: {item['source']}"
        if item["url"]:
            source_note += f"\nEvent page: {item['url']}"
        ev.add("description", f"{description}\n\n{source_note}" if description else source_note)
        cal.add_component(ev)

        sigs.append((day_of(item["start"]), norm(item["title"]), norm(item["location"])))
        added += 1
        source_counts[item["source"]] = source_counts.get(item["source"], 0) + 1

    OUTPUT.write_bytes(cal.to_ical())
    print(f"Added {added} Worcester college events to {OUTPUT}")
    for source in COLLEGE_ADDRESSES:
        print(f"  {source}: {source_counts.get(source, 0)} added after deduplication")


def main():
    items = fetch_all_college_events()
    print(f"Collected {len(items)} Worcester college events before cross-feed deduplication")
    append_college_events(items)


if __name__ == "__main__":
    main()
