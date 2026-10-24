"""
fantasy_hr_ingest.py -- pull Hockey-Reference season skater/goalie tables into fantasy.hr_season

These are the same tables the fantasy workbook was built from (Standard skaters,
Advanced skaters, Goalies), so SAA/SAR inputs match the sheet exactly:
    GP G A PIM SOG HIT BLK TAKE  (standard)     CF CA  (advanced; Cor = CF - CA)
    STP = PPG + SHG + PPA + SHA                  (PPA/SHA = the PP / SH assist columns)

Usage:  python scripts/fantasy_hr_ingest.py            # current season, skipped if refreshed < MIN_AGE_HOURS ago
        python scripts/fantasy_hr_ingest.py --force    # always fetch
        python scripts/fantasy_hr_ingest.py --seasons 2526,2627 --dry-run
Env:    PGHOST PGUSER PGPASSWORD PGDATABASE PGPORT (same secrets as the other scripts)

Cost: 3 requests per season (standard, advanced, goalies), ~4 s apart. No ScraperAPI credits.
Past seasons are final: they are fetched once and skipped afterwards unless --force.
"""
import argparse
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup, Comment

HEADERS = {"User-Agent": "Mozilla/5.0"}
SEASON_URL = "https://www.hockey-reference.com/leagues/NHL_{y}_{page}.html"
PAGES = {"std": "skaters", "adv": "skaters-advanced", "gk": "goalies"}
CURRENT_SEASON = os.environ.get("FANTASY_CURRENT_SEASON", "2627")   # HR year = 2000 + last two digits
SEASONS_DEFAULT = ["2526", "2627"]
MIN_AGE_HOURS = float(os.environ.get("MIN_AGE_HOURS", "3"))
REQUEST_GAP_SECONDS = 4.0
MULTI_TEAM = re.compile(r"^(TOT|\d+TM)$")


def hr_year(season):               # '2627' -> 2027
    return 2000 + int(season[2:])


# --------------------------------------------------------------------------- parsing
def _all_tables(soup):
    for t in soup.find_all("table"):
        yield t
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        if "<table" in c:
            for t in BeautifulSoup(c, "html.parser").find_all("table"):
                yield t


def _header(table):
    """Header texts of the last header row (the one that lines up with the data cells)."""
    thead = table.find("thead")
    rows = thead.find_all("tr") if thead else []
    rows = [r for r in rows if len(r.find_all("th")) > 5]
    if not rows:
        return None
    ths = rows[-1].find_all("th")
    out, seen = [], {}
    for th in ths:
        t = th.get_text(strip=True)
        seen[t] = seen.get(t, 0) + 1
        out.append(t if seen[t] == 1 else f"{t}_{seen[t]}")
    return out


def _num(s):
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    if s in ("", "-", "--"):
        return None
    try:
        return float(s) if ("." in s) else int(s)
    except ValueError:
        return None


def _minutes(s):
    """'603:55' -> 604 (whole minutes); plain numbers pass through."""
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    if ":" in s:
        try:
            m, sec = s.split(":")[:2]
            return int(round(int(m) + int(sec) / 60))
        except ValueError:
            return None
    return _num(s)


def parse_table(table):
    hdr = _header(table)
    if not hdr:
        return None, []
    tbody = table.find("tbody")
    rows = []
    for tr in (tbody.find_all("tr") if tbody else []):
        if "thead" in (tr.get("class") or []):
            continue
        cells = tr.find_all(["th", "td"])
        if len(cells) != len(hdr):
            continue
        rec = {h: c.get_text(strip=True) for h, c in zip(hdr, cells)}
        link = tr.find("a", href=re.compile(r"/players/\w/\w+\.html"))
        if not link:
            continue
        rec["_id"] = re.search(r"/players/\w/(\w+)\.html", link["href"]).group(1)
        rows.append(rec)
    return hdr, rows


def find_table(html, required):
    soup = BeautifulSoup(html, "html.parser")
    for t in _all_tables(soup):
        tid = (t.get("id") or "").lower()
        if "post" in tid or "playoff" in tid:         # 2025-26 page also carries a playoff table (player_stats_post)
            continue
        hdr, rows = parse_table(t)
        if hdr and all(r in hdr for r in required) and rows:
            return rows
    return []


def describe_tables(html):
    """Diagnostic: what tables does this page actually contain? (printed when a required table is not found)"""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for t in _all_tables(soup):
        hdr, rows = parse_table(t)
        out.append(f"id={t.get('id')} rows={len(rows)} header={hdr[:40] if hdr else None}")
    return out


def _team_col(rec):
    return rec.get("Team") or rec.get("Tm") or ""


def _collapse(rows):
    """One record per player: the TOT row for traded players, with team = last club listed."""
    by = {}
    order = []
    for r in rows:
        pid = r["_id"]
        if pid not in by:
            by[pid] = []
            order.append(pid)
        by[pid].append(r)
    out = {}
    for pid in order:
        rs = by[pid]
        tot = [r for r in rs if MULTI_TEAM.match(_team_col(r))]
        teams = [_team_col(r) for r in rs if not MULTI_TEAM.match(_team_col(r))]
        base = dict(tot[0] if tot else rs[-1])
        base["_team"] = teams[-1] if teams else _team_col(base)
        out[pid] = base
    return out


def _need(html, required, label):
    rows = find_table(html, required)
    if not rows:
        print(f"  could not find the {label} table (needs columns {required}). Tables on the page:")
        for line in describe_tables(html)[:12]:
            print("   ", line)
    return rows


def build_records(season, std_html, adv_html, gk_html):
    std = _collapse(_need(std_html, ["Player", "GP", "HIT", "BLK", "TAKE", "PIM"], "standard skaters"))
    adv = _collapse(_need(adv_html, ["Player", "GP", "CF", "CA"], "advanced skaters"))
    gk = _collapse(_need(gk_html, ["Player", "GP", "W", "GAA", "SV%"], "goalies"))
    recs = []
    for pid, r in std.items():
        pos = r.get("Pos", "")
        if pos == "G":
            continue
        a = adv.get(pid, {})
        n = lambda k, src=r: _num(src.get(k))
        recs.append(dict(
            season=season, hr_player_id=pid, name=r["Player"], team=r["_team"], pos=pos,
            grp="D" if pos == "D" else "F", age=n("Age"), gp=n("GP") or 0,
            g=n("G"), a=n("A"), pim=n("PIM"),
            ppg=n("PPG"), shg=n("SHG"), ppa=n("PP"), sha=n("SH"),
            sog=n("SOG"), hit=n("HIT"), blk=n("BLK"), tk=n("TAKE"),
            cf=_num(a.get("CF")), ca=_num(a.get("CA")),
            w=None, ga=None, sa=None, sv_pct=None, gaa=None, so=None, toi_min=None))
    for pid, r in gk.items():
        n = lambda k: _num(r.get(k))
        recs.append(dict(
            season=season, hr_player_id=pid, name=r["Player"], team=r["_team"], pos="G", grp="G",
            age=n("Age"), gp=n("GP") or 0, g=None, a=None, pim=None, ppg=None, shg=None, ppa=None,
            sha=None, sog=None, hit=None, blk=None, tk=None, cf=None, ca=None,
            w=n("W"), ga=n("GA"), sa=n("Shots") if r.get("Shots") else n("SA"), sv_pct=n("SV%"), gaa=n("GAA"), so=n("SO"), toi_min=_minutes(r.get("MIN"))))
    return recs


# --------------------------------------------------------------------------- IO
COLS = ["season", "hr_player_id", "name", "team", "pos", "grp", "age", "gp", "g", "a", "pim", "ppg", "shg",
        "ppa", "sha", "sog", "hit", "blk", "tk", "cf", "ca", "w", "ga", "sa", "sv_pct", "gaa", "so", "toi_min"]


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.content.decode("utf-8", errors="replace")


def fetch_season(season):
    pages = {}
    for key, page in PAGES.items():
        pages[key] = fetch(SEASON_URL.format(y=hr_year(season), page=page))
        time.sleep(REQUEST_GAP_SECONDS)
    return build_records(season, pages["std"], pages["adv"], pages["gk"])


def upsert(conn, recs):
    from psycopg2.extras import execute_values
    sql = (f"insert into fantasy.hr_season ({', '.join(COLS)}, updated_at) values %s "
           f"on conflict (season, hr_player_id) do update set "
           + ", ".join(f"{c}=excluded.{c}" for c in COLS[2:]) + ", updated_at=now()")
    with conn.cursor() as cur:
        execute_values(cur, sql, [tuple(r[c] for c in COLS) for r in recs],
                       template="(" + ",".join(["%s"] * len(COLS)) + ", now())")
    conn.commit()


def hours_since_update(conn, season):
    with conn.cursor() as cur:
        cur.execute("select extract(epoch from now()-max(updated_at))/3600, count(*) "
                    "from fantasy.hr_season where season=%s", (season,))
        hrs, n = cur.fetchone()
    return (float(hrs) if hrs is not None else None), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", default=",".join(SEASONS_DEFAULT))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    import psycopg2
    conn = psycopg2.connect(host=os.environ["PGHOST"], user=os.environ["PGUSER"],
                            password=os.environ["PGPASSWORD"], dbname=os.environ["PGDATABASE"],
                            port=os.environ.get("PGPORT", "5432"), sslmode="require")
    failed = False
    for season in args.seasons.split(","):
        hrs, n = hours_since_update(conn, season)
        if not args.force and n:
            if season != CURRENT_SEASON:
                print(f"[{season}] final season already loaded ({n} rows); skipping.")
                continue
            if hrs is not None and hrs < MIN_AGE_HOURS:
                print(f"[{season}] refreshed {hrs:.1f}h ago (< {MIN_AGE_HOURS}h); skipping.")
                continue
        try:
            recs = fetch_season(season)
        except Exception as e:                                  # one bad season must not hide the other
            print(f"[{season}] FAILED: {type(e).__name__}: {str(e)[:200]}")
            failed = True
            continue
        sk = sum(1 for r in recs if r["grp"] != "G")
        print(f"[{season}] parsed {sk} skaters, {len(recs) - sk} goalies"
              f" (with Cor: {sum(1 for r in recs if r['cf'] is not None)}).")
        if sk < 300 or len(recs) - sk < 20:
            print(f"[{season}] suspiciously small parse; NOT writing.")
            failed = True
            continue
        if args.dry_run:
            print(f"[{season}] dry-run: nothing written. Sample: {recs[0]}")
            continue
        upsert(conn, recs)
        print(f"[{season}] upserted {len(recs)} rows.")
        try:                                                    # match Fantrax players to the freshly loaded HR ids
            with conn.cursor() as cur:
                cur.execute("select fantasy.link_players()")
                print(f"[{season}] linked {cur.fetchone()[0]} more Fantrax player(s) to Hockey-Reference ids.")
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[{season}] player linking skipped: {type(e).__name__}: {str(e)[:150]}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
