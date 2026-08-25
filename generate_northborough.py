from pathlib import Path

import generate_calendar as base
from generate_calendar_with_northborough import (
    LIBRARY_SOURCE,
    TOWN_SOURCE,
    fetch_northborough_library,
    fetch_northborough_town,
)


OUTPUT = Path("northborough-events.ics")


def main():
    # Give the two Northborough sources stable deduplication priority.
    base.SOURCE_PRIORITY.update({
        TOWN_SOURCE: 1,
        LIBRARY_SOURCE: 2,
    })

    items = []

    try:
        items.extend(fetch_northborough_town())
    except Exception as exc:
        print(f"ERROR loading {TOWN_SOURCE}: {exc}")

    try:
        items.extend(fetch_northborough_library())
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

    # Reuse the proven Massachusetts ICS builder, but write to a separate file.
    original_output = base.OUTPUT
    try:
        base.OUTPUT = OUTPUT
        base.build_calendar(unique)
    finally:
        base.OUTPUT = original_output

    print(f"Wrote {OUTPUT} with {len(unique)} unique Northborough events")


if __name__ == "__main__":
    main()
