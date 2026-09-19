"""
nhl_diagnose_advanced_table.py — one-off diagnostic, not part of the pipeline

Prints every table's actual `id` attribute on one box score page, plus every
column's actual `data-stat` attribute for whichever table looks like the
team-level advanced/Corsi table. Run this and paste me the output — the
main scripts' corsi_for_pct extraction was built on a guessed table id/
data-stat scheme that turned out to be wrong (see chat), so this replaces
guessing with ground truth from the real page.

Usage: python nhl_diagnose_advanced_table.py
"""

import requests
from bs4 import BeautifulSoup, Comment

HEADERS = {"User-Agent": "Mozilla/5.0"}
URL = "https://www.hockey-reference.com/boxscores/202603080BUF.html"  # TBL @ BUF, Mar 8 2026


def main():
    resp = requests.get(URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    print("=== All <table> ids found directly in HTML ===")
    for t in soup.find_all("table"):
        print(" ", t.get("id"))

    print()
    print("=== All <table> ids found inside HTML comments (Sports-Reference "
          "often hides secondary tables this way) ===")
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment_soup = BeautifulSoup(comment, "html.parser")
        for t in comment_soup.find_all("table"):
            print(" ", t.get("id"))

    print()
    print("=== Looking specifically for anything with 'adv' or 'TBL' or 'BUF' in the id ===")
    all_tables = list(soup.find_all("table"))
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment_soup = BeautifulSoup(comment, "html.parser")
        all_tables += comment_soup.find_all("table")
    candidates = [t for t in all_tables if t.get("id") and
                  "adv" in t.get("id").lower() and
                  ("all5v5" in t.get("id").lower() or "allall" in t.get("id").lower())]
    for t in candidates:
        print(f"\n--- table id={t.get('id')} ---")
        header_row = t.find("thead")
        if header_row:
            ths = header_row.find_all("th")
            print("  header data-stat values:", [th.get("data-stat") for th in ths])

        tfoot = t.find("tfoot")
        if tfoot:
            print("  TFOOT rows found:")
            for tr in tfoot.find_all("tr"):
                cells = tr.find_all(["th", "td"])
                print("   ", [(c.get("data-stat"), c.get_text(strip=True)) for c in cells])
        else:
            print("  TFOOT: none")

        # also print raw table HTML around the end, in case totals live somewhere else
        body_rows = t.find("tbody").find_all("tr") if t.find("tbody") else []
        print(f"  tbody row count: {len(body_rows)}")
        last = body_rows[-1]
        cells = last.find_all(["th", "td"])
        print("  LAST TBODY ROW (data-stat: text):")
        for c in cells:
            print(f"    {c.get('data-stat')!r}: {c.get_text(strip=True)!r}")


if __name__ == "__main__":
    main()
