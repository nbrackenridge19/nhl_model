"""
nhl_diagnose_espn_event_lookup.py -- find a way to get ESPN event IDs for odds
without the blocked site.api.espn.com scoreboard endpoint.

Background: ESPN odds (sports.core.api.espn.com/.../odds) answers fine from
GitHub, and NHL.com's own feed has no odds at all. The piece missing is how to
get the ESPN event_id for a given date's games from GitHub, since the usual
way -- site.api.espn.com's scoreboard -- returns 403 there. This tries several
candidate endpoints on other ESPN domains/paths that are NOT known to be
blocked, plus a fallback of walking the whole-season events list.

Nothing is written anywhere. Run on GitHub Actions (same job style as
nhl_check_sources.py) so the results reflect what GitHub's servers can reach,
not what this computer can reach.

Run:
    python nhl_diagnose_espn_event_lookup.py                  # yesterday + today
    python nhl_diagnose_espn_event_lookup.py 2026-09-29        # one date
"""

import sys
import time
from datetime import date, timedelta

import requests

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# Known-known ids for cross-checking a lookup against something real.
KNOWN = {"2026-09-19": {"event_id": "401881922", "home": "TOR", "away": "MTL"}}

CANDIDATES = [
    # (label, url template — {d8}=YYYYMMDD {diso}=YYYY-MM-DD)
    ("core API events, dates param", "https://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl/events?dates={d8}&limit=50"),
    ("core API events, date param (singular)", "https://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl/events?date={d8}&limit=50"),
    ("core API scoreboard mirror", "https://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl/scoreboard?dates={d8}"),
    ("site.web.api scoreboard", "https://site.web.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?dates={d8}"),
    ("cdn.espn.com scoreboard", "https://cdn.espn.com/core/nhl/scoreboard?xhr=1&dates={d8}"),
    ("core API events, no date (page 1, to see if events are date-ordered)", "https://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl/events?limit=50"),
]


def describe_event_ids(js, url):
    """Pull out whatever looks like {event_id, home, away, date} regardless of shape."""
    out = []
    # shape 1: {"events": [...]} like the site.api scoreboard
    for ev in js.get("events", []) or []:
        comp = (ev.get("competitions") or [{}])[0]
        comps = comp.get("competitors") or []
        home = next((c for c in comps if c.get("homeAway") == "home"), {})
        away = next((c for c in comps if c.get("homeAway") == "away"), {})
        out.append({
            "event_id": ev.get("id"), "date": ev.get("date"),
            "home": (home.get("team") or {}).get("abbreviation"), "away": (away.get("team") or {}).get("abbreviation"),
        })
    # shape 2: {"items": [{"$ref": ".../events/401881922?..."}]} like a core-API collection
    for it in js.get("items", []) or []:
        ref = it.get("$ref", "")
        if "/events/" in ref:
            eid = ref.split("/events/")[1].split("?")[0].split("/")[0]
            out.append({"event_id": eid, "date": None, "home": None, "away": None, "note": "from $ref, needs a follow-up fetch for teams/date"})
    return out


def try_one(label, url):
    t0 = time.time()
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL {label}: request error: {e}")
        return None
    secs = time.time() - t0
    if r.status_code != 200:
        print(f"FAIL {label}: HTTP {r.status_code} in {secs:.1f}s | {r.text[:150]!r}")
        return None
    try:
        js = r.json()
    except Exception as e:  # noqa: BLE001
        print(f"FAIL {label}: HTTP 200 but not JSON ({e}) | {r.text[:150]!r}")
        return None
    events = describe_event_ids(js, url)
    top_keys = sorted(js.keys())
    print(f"OK   {label}: HTTP 200 in {secs:.1f}s | top-level keys: {top_keys} | events found: {len(events)}")
    for e in events[:15]:
        print(f"       {e}")
    return events


def main():
    args = sys.argv[1:]
    dates = [date.fromisoformat(a) for a in args] if args else [date.today() - timedelta(days=1), date.today()]
    for d in dates:
        d8, diso = d.strftime("%Y%m%d"), d.isoformat()
        print(f"\n=== {diso} ===")
        found_any = False
        for label, tmpl in CANDIDATES:
            url = tmpl.format(d8=d8, diso=diso)
            evs = try_one(label, url)
            if evs:
                found_any = True
                known = KNOWN.get(diso)
                if known and any(str(e.get("event_id")) == known["event_id"] for e in evs):
                    print(f"       MATCH: found the known event_id {known['event_id']} for {diso}")
            time.sleep(1.5)
        if not found_any:
            print(f"  Nothing worked for {diso}.")

    print("\nDone. Paste everything above into the chat.")


if __name__ == "__main__":
    main()
