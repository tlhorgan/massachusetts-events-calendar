from datetime import datetime, timedelta
from pathlib import Path
import re

from bs4 import BeautifulSoup
from dateutil import parser as dtparser

import generate_calendar as base
from generate_calendar_with_northborough import (
    LIBRARY_SOURCE,
    TOWN_SOURCE,
    discover_library_event_urls,
    fetch_northborough_town,
    get,
    library_title,
)


OUTPUT = Path("northborough-events.ics")


def parse_library_event_fixed(url: str, inferred_year: int):
    """
    Parse a Northborough Free Library event page.

    Assabet frequently renders ranges like "10:00—11:00 AM", where the
    first time has no AM/PM marker. Infer the first meridiem from the ending
    time instead of rejecting the event.
    """
    soup = BeautifulSoup(get(url).text, "html.parser")
    title = library_title(soup)
    if not title:
        return None

    text = base.clean(soup.get_text(" ", strip=True))

    date_prefix = (
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*"
        r"([A-Za-z]+)\s+(\d{1,2})\s+"
    )

    timed = re.search(
        date_prefix
        + r"(\d{1,2}(?::\d{2})?)\s*(AM|PM)?"
        + r"\s*[—–-]\s*"
        + r"(\d{1,2}(?::\d{2})?)\s*(AM|PM)",
        text,
        re.I,
    )

    if timed:
        try:
            day = dtparser.parse(
                f"{timed.group(1)} {timed.group(2)}, {inferred_year}"
            ).date()

            start_meridiem = timed.group(4) or timed.group(6)
            end_meridiem = timed.group(6)

            start_time = dtparser.parse(
                f"{timed.group(3)} {start_meridiem}"
            ).time()
            end_time = dtparser.parse(
                f"{timed.group(5)} {end_meridiem}"
            ).time()

            start = datetime.combine(day, start_time)
            end = datetime.combine(day, end_time)

            # If inheriting the ending meridiem makes the end earlier than
            # the start, try the opposite meridiem for the start (for example
            # 11:00—1:00 PM should mean 11 AM to 1 PM).
            if end <= start and timed.group(4) is None:
                opposite = "AM" if end_meridiem.upper() == "PM" else "PM"
                alternate_start = dtparser.parse(
                    f"{timed.group(3)} {opposite}"
                ).time()
                alternate = datetime.combine(day, alternate_start)
                if alternate < end:
                    start = alternate

            if end <= start:
                end += timedelta(days=1)

        except Exception:
            return None
    else:
        single = re.search(
            date_prefix + r"(\d{1,2}(?::\d{2})?)\s*(AM|PM)",
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
                day = dtparser.parse(
                    f"{single.group(1)} {single.group(2)}, {inferred_year}"
                ).date()
                start = datetime.combine(
                    day,
                    dtparser.parse(f"{single.group(3)} {single.group(4)}").time(),
                )
                end = start + timedelta(hours=1)
            except Exception:
                return None
        elif all_day:
            try:
                day = dtparser.parse(
                    f"{all_day.group(1)} {all_day.group(2)}, {inferred_year}"
                ).date()
                start = day
                end = day + timedelta(days=1)
            except Exception:
                return None
        else:
            return None

    if day < datetime.now().date():
        return None

    # Use the actual venue/address shown by Assabet when possible.
    location = "Northborough Free Library, 34 Main St, Northborough, MA 01532"
    location_match = re.search(
        r"([A-Za-z][A-Za-z &'().-]{2,80})\s+"
        r"(\d+\s+[A-Za-z0-9 .'-]+(?:St|Street|Rd|Road|Ave|Avenue|Dr|Drive|Ln|Lane|Way|Blvd|Boulevard))"
        r",?\s*Northborough,\s*MA,?\s*(\d{5})",
        text,
        re.I,
    )
    if location_match:
        location = base.clean(
            f"{location_match.group(1)}, {location_match.group(2)}, "
            f"Northborough, MA {location_match.group(3)}"
        )

    meta = soup.find("meta", attrs={"name": "description"})
    description = (
        base.clean(meta.get("content"))
        if meta and meta.get("content")
        else ""
    )

    return {
        "title": title,
        "start": start,
        "end": end,
        "location": location,
        "description": description,
        "url": url,
        "sources": [LIBRARY_SOURCE],
    }


def fetch_northborough_library_fixed():
    print(f"Fetching {LIBRARY_SOURCE}...")
    items = []
    discovered = discover_library_event_urls(12)

    for url, year in sorted(discovered.items()):
        try:
            event = parse_library_event_fixed(url, year)
            if event:
                items.append(event)
        except Exception as exc:
            print(f"  {LIBRARY_SOURCE}: skip {url}: {exc}")

    print(f"  {LIBRARY_SOURCE}: {len(items)} future events")
    return items


def main():
    base.SOURCE_PRIORITY.update({
        TOWN_SOURCE: 1,
        LIBRARY_SOURCE: 2,
    })

    items = []

    # The town site currently returns 403 to GitHub Actions. Keep trying it,
    # but do not let that prevent the library calendar from being published.
    try:
        items.extend(fetch_northborough_town())
    except Exception as exc:
        print(f"ERROR loading {TOWN_SOURCE}: {exc}")

    try:
        items.extend(fetch_northborough_library_fixed())
    except Exception as exc:
        print(f"ERROR loading {LIBRARY_SOURCE}: {exc}")

    if not items:
        raise RuntimeError("No Northborough events were collected from any source.")

    print(f"Collected {len(items)} Northborough events before deduplication")
    unique = base.dedupe(items)

    if len(unique) < 3:
        raise RuntimeError(
            f"Only {len(unique)} unique Northborough events were generated; "
            "refusing to publish a suspiciously small feed."
        )

    original_output = base.OUTPUT
    try:
        base.OUTPUT = OUTPUT
        base.build_calendar(unique)
    finally:
        base.OUTPUT = original_output

    print(f"Wrote {OUTPUT} with {len(unique)} unique Northborough events")


if __name__ == "__main__":
    main()
