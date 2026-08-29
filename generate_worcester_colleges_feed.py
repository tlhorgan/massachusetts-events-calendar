from pathlib import Path

from icalendar import Calendar

import add_worcester_colleges as colleges


OUTPUT = Path("worcester-colleges-events.ics")


def initialize_calendar():
    cal = Calendar()
    cal.add("prodid", "-//Worcester Colleges Events//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "Worcester Colleges Events")
    OUTPUT.write_bytes(cal.to_ical())


def main():
    # Reuse the existing college collectors but direct their output to a
    # dedicated feed instead of appending to massachusetts-events.ics.
    colleges.OUTPUT = OUTPUT
    initialize_calendar()
    colleges.main()


if __name__ == "__main__":
    main()
