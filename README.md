# Massachusetts Events → Proton Calendar

This project combines multiple Massachusetts tourism/event calendars into one automatically refreshed iCalendar feed.

## Sources

- VisitMA — official Massachusetts Office of Travel & Tourism statewide calendar
- Cape Cod Chamber of Commerce
- Meet Boston
- Discover Central Massachusetts
- Explore Western Mass
- The Berkshires
- North of Boston

The regional calendars supplement VisitMA and help capture events that may not appear in the statewide listing.

## Output

`massachusetts-events.ics`

## Proton Calendar subscription URL

After creating a public GitHub repository named `massachusetts-events-calendar`, subscribe to:

https://raw.githubusercontent.com/YOUR-GITHUB-USERNAME/massachusetts-events-calendar/main/massachusetts-events.ics

Replace `YOUR-GITHUB-USERNAME` with your GitHub username.

## Deduplication

Events on the same date are merged when their normalized titles are very close, or when both title and location strongly match. A timed event must also be within four hours of the other event's start time.

When duplicates are merged, the feed preserves richer description/location data and lists every contributing source in the event description.

## Safety check

The workflow refuses to publish if fewer than 25 unique events are generated. That prevents a temporary source failure or website redesign from replacing a healthy feed with a nearly empty calendar.

## Manual run

Actions → Update Massachusetts events calendar → Run workflow
