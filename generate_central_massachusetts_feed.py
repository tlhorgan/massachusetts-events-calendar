from pathlib import Path

from icalendar import Calendar

import add_central_massachusetts as central


OUTPUT = Path("central-massachusetts-events.ics")


def initialize_calendar():
    cal = Calendar()
    cal.add("prodid", "-//Central Massachusetts Events//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "Central Massachusetts Events")
    OUTPUT.write_bytes(cal.to_ical())


def main():
    # The existing collector writes to its module-level OUTPUT. Point it at a
    # dedicated feed so these events no longer get mixed into Massachusetts.
    central.OUTPUT = OUTPUT
    initialize_calendar()
    central.main()


if __name__ == "__main__":
    main()
