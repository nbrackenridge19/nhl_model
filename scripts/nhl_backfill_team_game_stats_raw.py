"""
nhl_backfill_team_game_stats_raw.py — one-time backfill

Populates team_game_stats_raw (cf_raw, ca_raw, pp_goals_raw, pp_opp_raw,
pk_ga_raw, pk_opp_raw) for every 2025-26 regular-season game already in
`games`. Required before nhl_daily_results_ingest.py's trailing CF%/PP%/PK%
blend will be correct — without this, the first live day sums against an
empty history and produces wrong values.

Regular season only (playoff = false), matching the existing convention
in nhl_full_historical_backfill.py (Phase 2 scraping there is also
playoff-filtered — playoff games were never scraped at the player-appearance
level either).

RESUME-SAFE: skips any (game_id, team_code) already in team_game_stats_raw,
so an interrupted run can just be re-run.

SAFETY
------
- DB connection: PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT env vars.
- DRY_RUN defaults to true.
- Checkpoints every CHECKPOINT_EVERY games (default 25).
"""

import os
import re
import time
import requests
import psycopg2
from psycopg2.extras import execute_values
from bs4 import BeautifulSoup, Comment

DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "3"))
CHECKPOINT_EVERY = int(os.environ.get("CHECKPOINT_EVERY", "25"))
HEADERS = {"User-Agent": "Mozilla/5.0"}
BOXSCORE_URL = "https://www.hockey-reference.com/boxscores/{code}.html"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary"
SEASON_CODE = "2526"

# ESPN uses different abbreviations than our internal team_code for these 5
# teams (confirmed via live ESPN pages during the spot check) — every other
# team's ESPN abbreviation matches our team_code exactly.
ESPN_TEAM_CODE_MAP = {"tbl": "tb", "sjs": "sj", "njd": "nj", "lak": "la", "veg": "vgk"}
ESPN_TEAM_CODE_MAP_REVERSE = {v: k for k, v in ESPN_TEAM_CODE_MAP.items()}

# Optional: comma-separated team codes to limit this run to (e.g. only
# re-fetching the 5 teams that failed the first pass). Unset = all teams.
TEAM_CODE_FILTER = set(
    c.strip() for c in os.environ.get("TEAM_CODE_FILTER", "").split(",") if c.strip()
) or None


def get_db_conn():
    if os.environ.get("PGHOST"):
        return psycopg2.connect()
    raise RuntimeError("Set PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT.")


def parse_int(val):
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def build_boxscore_code(game_date, home_team):
    return f"{game_date.strftime('%Y%m%d')}0{home_team.upper()}"


def get_soup_table(soup, table_id):
    table = soup.find("table", id=table_id)
    if table is not None:
        return table
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        if table_id in comment:
            comment_soup = BeautifulSoup(comment, "html.parser")
            table = comment_soup.find("table", id=table_id)
            if table is not None:
                return table
    return None


def fetch_cf_ca(game_id, game_date, home_team, away_team):
    code = build_boxscore_code(game_date, home_team)
    url = BOXSCORE_URL.format(code=code)
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    result = {}
    for team_code in (home_team, away_team):
        adv_table = get_soup_table(soup, f"{team_code.upper()}_adv_ALLAll")
        if adv_table is None:
            result[team_code] = (None, None)
            continue
        cf = ca = None
        for tr in adv_table.find("tbody").find_all("tr"):
            cells = tr.find_all(["th", "td"])
            row = {c.get("data-stat"): c.get_text(strip=True) for c in cells}
            if tr.find("td", {"data-stat": "player"}) is None:
                cf = parse_int(row.get("on_Cevents"))
                ca = parse_int(row.get("on_opp_Cevents"))
        result[team_code] = (cf, ca)
    return result


def fetch_pp_pk(game_date, home_team, away_team):
    home_espn = ESPN_TEAM_CODE_MAP.get(home_team, home_team)
    away_espn = ESPN_TEAM_CODE_MAP.get(away_team, away_team)
    try:
        sb = requests.get(ESPN_SCOREBOARD_URL, params={"dates": game_date.strftime("%Y%m%d")},
                           headers=HEADERS, timeout=20).json()
    except Exception:
        return {}
    event_id = None
    for ev in sb.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        names = {c.get("team", {}).get("abbreviation", "").lower() for c in (comp.get("competitors") or [])}
        if {home_espn, away_espn} & names:
            event_id = ev.get("id")
            break
    if event_id is None:
        return {}
    try:
        summary = requests.get(ESPN_SUMMARY_URL, params={"event": event_id}, headers=HEADERS, timeout=20).json()
    except Exception:
        return {}
    raw = {}
    for team_block in summary.get("boxscore", {}).get("teams", []):
        espn_abbr = team_block.get("team", {}).get("abbreviation", "").lower()
        abbr = ESPN_TEAM_CODE_MAP_REVERSE.get(espn_abbr, espn_abbr)
        stats = {s.get("name"): s.get("displayValue") for s in team_block.get("statistics", [])}
        pp = stats.get("powerPlayGoals") or stats.get("powerPlayConversion", "0-0")
        if "-" in str(pp):
            goals, opp = str(pp).split("-")
        else:
            goals, opp = stats.get("powerPlayGoals"), stats.get("powerPlayOpportunities")
        raw[abbr] = {"pp_goals": parse_int(goals), "pp_opp": parse_int(opp)}
    teams = list(raw.keys())
    if len(teams) == 2:
        a, b = teams
        raw[a]["pk_opp"] = raw[b]["pp_opp"]
        raw[a]["pk_ga"] = raw[b]["pp_goals"]
        raw[b]["pk_opp"] = raw[a]["pp_opp"]
        raw[b]["pk_ga"] = raw[a]["pp_goals"]
    return raw


def already_done_keys(conn):
    with conn.cursor() as cur:
        cur.execute(
            "select game_id, team_code from team_game_stats_raw "
            "where cf_raw is not null and pp_goals_raw is not null"
        )
        return set(cur.fetchall())


def games_to_backfill(conn):
    with conn.cursor() as cur:
        cur.execute(
            "select game_id, date, home_team, away_team from games "
            "where season = %s and playoff = false order by date",
            (SEASON_CODE,),
        )
        rows = cur.fetchall()
        if TEAM_CODE_FILTER:
            rows = [r for r in rows if r[2] in TEAM_CODE_FILTER or r[3] in TEAM_CODE_FILTER]
        return rows


def upsert_rows(conn_factory, rows):
    if not rows:
        return
    cols = ["game_id", "team_code", "cf_raw", "ca_raw", "pp_goals_raw", "pp_opp_raw", "pk_ga_raw", "pk_opp_raw"]
    values = [tuple(r.get(c) for c in cols) for r in rows]
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in ("game_id", "team_code"))
    sql = (f"INSERT INTO team_game_stats_raw ({', '.join(cols)}) VALUES %s "
           f"ON CONFLICT (game_id, team_code) DO UPDATE SET {set_clause}")
    if DRY_RUN:
        print(f"  [dry-run] would upsert {len(values)} rows")
        return
    conn = conn_factory()
    try:
        with conn.cursor() as cur:
            execute_values(cur, sql, values)
        conn.commit()
        print(f"  [live] upserted {len(values)} rows")
    finally:
        conn.close()


def main():
    print(f"team_game_stats_raw backfill — DRY_RUN={DRY_RUN} — season {SEASON_CODE}")
    conn = get_db_conn()
    try:
        games = games_to_backfill(conn)
        done = already_done_keys(conn)
        print(f"{len(games)} regular-season games in {SEASON_CODE}; "
              f"{len(done)} (game_id, team_code) pairs already backfilled.")

        batch = []
        processed = 0
        for i, (game_id, game_date, home_team, away_team) in enumerate(games, 1):
            if (game_id, home_team) in done and (game_id, away_team) in done:
                continue
            print(f"[{i}/{len(games)}] {game_id} ({game_date}, {away_team} @ {home_team})")
            try:
                cf_ca = fetch_cf_ca(game_id, game_date, home_team, away_team)
                pp_pk = fetch_pp_pk(game_date, home_team, away_team)
            except Exception as e:
                print(f"  ERROR: {e}")
                continue

            for team_code in (home_team, away_team):
                cf, ca = cf_ca.get(team_code, (None, None))
                pp = pp_pk.get(team_code, {})
                batch.append({
                    "game_id": game_id, "team_code": team_code, "cf_raw": cf, "ca_raw": ca,
                    "pp_goals_raw": pp.get("pp_goals"), "pp_opp_raw": pp.get("pp_opp"),
                    "pk_ga_raw": pp.get("pk_ga"), "pk_opp_raw": pp.get("pk_opp"),
                })
            processed += 1

            if not DRY_RUN and processed % CHECKPOINT_EVERY == 0:
                upsert_rows(get_db_conn, batch)
                batch = []

            time.sleep(REQUEST_DELAY_SECONDS)

        if DRY_RUN:
            print(f"[dry-run] would upsert {len(batch)} remaining rows")
        else:
            upsert_rows(get_db_conn, batch)

    finally:
        conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
