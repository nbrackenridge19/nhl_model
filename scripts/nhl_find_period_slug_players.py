"""
nhl_find_period_slug_players.py — targeted, one-time scan

Finds every hockey-reference player whose ID slug contains a literal period
(e.g. J.T. Compher -> comphj.01). These are exactly the players the OLD
regex bug in nhl_backfill_player_appearances.py / nhl_full_historical_backfill.py
(`[a-zA-Z0-9]+` instead of `[a-zA-Z0-9.]+`) would have silently dropped from
EVERY game they appeared in.

Deliberately NOT a full re-scrape. hockey-reference's alphabetical player
index (/players/<letter>/) lists every player who's ever appeared in the
NHL/WHA, one page per starting letter of last name -- 26 pages total. That's
a bounded, cheap scan, versus re-fetching thousands of box scores just to
find a rare id format.

Output: prints (and optionally writes to a small CSV) every affected
player's id, display name, and the season range they played -- this is the
input list for nhl_backfill_period_slug_players.py, not a full backfill by
itself.

SAFETY
------
- Same rate-limiting discipline as the other scrapers: a delay between the
  26 index-page requests.
- No DB writes at all. This script only reads from hockey-reference and
  writes a local CSV -- there's nothing here that needs DRY_RUN.
"""

import os
import re
import csv
import time
import string
import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0"}
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "3"))
OUTPUT_CSV = os.environ.get("OUTPUT_CSV", "period_slug_players.csv")

# Same widened pattern as the fixed extract_player_id() in the two backfill
# scripts -- this is deliberately the ONLY id format we're hunting for here.
PLAYER_LINK_RE = re.compile(r"^/players/([a-z])/([a-zA-Z0-9.]+)\.html$")


def fetch_index_page(letter):
    url = f"https://www.hockey-reference.com/players/{letter}/"
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return BeautifulSoup(resp.content, "html.parser")


def find_period_slug_players(soup, letter):
    """The player index table lists <a href="/players/x/slug01.html">Name</a>
    rows, each followed by parenthetical active-season info in the same
    <p> (e.g. "(2017-2026)"). Grab both the slug and that season range."""
    found = []
    for p in soup.find_all("p"):
        a = p.find("a", href=True)
        if a is None:
            continue
        m = PLAYER_LINK_RE.match(a["href"])
        if m is None:
            continue
        link_letter, slug = m.groups()
        if link_letter != letter or "." not in slug:
            continue  # only care about slugs with an embedded period
        display_name = a.get_text(strip=True)
        # the rest of the <p> text after the link is usually "(YYYY-YYYY)"
        full_text = p.get_text(" ", strip=True)
        season_range_match = re.search(r"\((\d{4})-(\d{4})\)", full_text)
        season_range = season_range_match.group(0) if season_range_match else ""
        found.append({
            "player_id": slug,
            "player_name_display": display_name,
            "season_range": season_range,
        })
    return found


def main():
    all_found = []
    letters = list(string.ascii_lowercase)
    for i, letter in enumerate(letters, 1):
        print(f"[{i}/{len(letters)}] scanning /players/{letter}/ ...")
        try:
            soup = fetch_index_page(letter)
        except Exception as e:
            print(f"  ERROR fetching letter '{letter}': {e}")
            continue
        matches = find_period_slug_players(soup, letter)
        if matches:
            print(f"  found {len(matches)} period-slug player(s): "
                  f"{[m['player_id'] for m in matches]}")
        all_found.extend(matches)
        if i < len(letters):
            time.sleep(REQUEST_DELAY_SECONDS)

    print(f"\nTotal period-slug players found across all 26 letters: {len(all_found)}")
    for row in all_found:
        print(f"  {row['player_id']:15s} {row['player_name_display']:25s} {row['season_range']}")

    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["player_id", "player_name_display", "season_range"])
        writer.writeheader()
        writer.writerows(all_found)
    print(f"\nWrote {OUTPUT_CSV} -- feed this into nhl_backfill_period_slug_players.py")


if __name__ == "__main__":
    main()
