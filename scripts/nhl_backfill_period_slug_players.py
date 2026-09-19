"""
nhl_backfill_period_slug_players.py — targeted backfill

Fixes the specific, narrow gap left by the old extract_player_id() regex
bug: any player whose hockey-reference id slug contains a literal period
(e.g. J.T. Compher -> comphj.01) was silently dropped from EVERY game they
appeared in, across every season already migrated.

This is deliberately NOT a full-season or full-historical re-scrape (that's
what nhl_full_historical_backfill.py / nhl_backfill_player_appearances.py
are for, and re-running either of those for this one narrow bug would mean
re-fetching ~11,800 box scores just to fix a handful of players). Instead:

  1. Read period_slug_players.csv (output of nhl_find_period_slug_players.py).
  2. For each affected player, fetch their hockey-reference player page to
     see which (season, team) pairs they actually played for.
  3. For each such pair that falls inside our migrated season range
     (1718-2526), pull that team's game list for that season FROM SUPABASE
     (already migrated -- no schedule re-scraping needed).
  4. Skip any game where the player already has a row in
     player_game_appearances (means they were captured some other way, or
     this script already ran) -- only re-fetch box scores for games that are
     actually missing the player.
  5. Parse just that player's row out of each affected box score (reusing
     the exact same parsing functions as the fixed main scraper, imported
     directly so there's no risk of the two implementations drifting apart)
     and upsert.

SAFETY
------
- Same DB connection convention: discrete PGHOST/PGUSER/PGPASSWORD/
  PGDATABASE/PGPORT env vars.
- DRY_RUN defaults to true.
- Rate-limited requests, same as the other scrapers.
"""

import os
import re
import csv
import time
import requests
import psycopg2
from bs4 import BeautifulSoup, Comment

# Reuse the exact parsing logic from the fixed main scraper -- no
# duplicated/divergent implementation of extract_player_id, parse tables,
# upsert, etc.
from nhl_backfill_player_appearances import (
    get_db_conn, get_soup_table, parse_skater_table, parse_goalie_table,
    parse_advanced_table, build_boxscore_code, upsert, BASE_URL, HEADERS,
)

INPUT_CSV = os.environ.get("INPUT_CSV", "period_slug_players.csv")
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "3"))

# Confirmed (per Nick, 2026) to have never played in our migrated 1718-2526
# range -- these all hit "no standard-stats table found" on the older
# hockey-reference page template, which is a separate, lower-priority issue
# from the period-slug bug this script exists to fix. Skipping them outright
# avoids re-spending a full request (with retries) on every run for players
# we already know don't matter here. Override with SKIP_IRRELEVANT_PLAYERS=false
# if that ever needs re-checking.
KNOWN_IRRELEVANT_PLAYER_IDS = {
    "anderr.01", "atherp.01", "boydr.01", "corbij.01", "duforj.01",
    "fastt.01", "fentop.01", "jenksa.01", "severc.01", "soucyj.01",
    "st.crmi01", "thelea.01", "wattj.01",
}
SKIP_IRRELEVANT_PLAYERS = os.environ.get("SKIP_IRRELEVANT_PLAYERS", "true").strip().lower() != "false"

# Our migrated season range -- don't bother checking a player's tenure
# outside this window, we have no games table rows there anyway.
MIGRATED_SEASONS = {"1718", "1819", "1920", "2021", "2122", "2223", "2324", "2425", "2526"}
SEASON_CODE_BY_HR_YEAR = {  # hockey-reference "2026" -> our "2526" convention
    int(f"20{s[2:4]}"): s for s in MIGRATED_SEASONS
}


def load_affected_players():
    with open(INPUT_CSV, newline="") as f:
        return list(csv.DictReader(f))


def fetch_player_team_seasons(player_id, retries=3):
    """Fetches the player's HR page and pulls (season_code, team_code) pairs
    from the 'Standard Stats' table, restricted to seasons we've migrated.
    Team codes here are hockey-reference's own (usually matching ours, but
    checked against our own `teams` table before use, not assumed)."""
    url = f"https://www.hockey-reference.com/players/{player_id[0]}/{player_id}.html"

    soup = None
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, "html.parser")
            break
        except Exception as e:
            last_error = e
            print(f"  attempt {attempt}/{retries} failed fetching {player_id}'s "
                  f"page: {e}")
            if attempt < retries:
                time.sleep(REQUEST_DELAY_SECONDS * attempt)  # back off a bit more each time
    if soup is None:
        print(f"  giving up on {player_id} after {retries} attempts ({last_error}) -- skipping")
        return []

    table = get_soup_table(soup, "player_stats")
    if table is None:
        print(f"  WARNING: no standard-stats table found for {player_id} "
              f"(possibly an older page template hockey-reference hasn't "
              f"migrated to the current 'Standard Stats' table id -- "
              f"needs a manual look at this specific page)")
        return []

    pairs = []
    rows_seen = 0
    for tr in table.find("tbody").find_all("tr"):
        rows_seen += 1
        season_cell = tr.find(["th", "td"], {"data-stat": "year_id"})
        team_cell = tr.find(["th", "td"], {"data-stat": "team_name_abbr"})
        if season_cell is None or team_cell is None:
            continue
        season_text = season_cell.get_text(strip=True)  # e.g. "2025-26"
        m = re.match(r"(\d{4})-\d{2}", season_text)
        if not m:
            continue
        hr_end_year = int(m.group(1)) + 1
        season_code = SEASON_CODE_BY_HR_YEAR.get(hr_end_year)
        if season_code is None:
            continue  # outside our migrated range
        team_link = team_cell.find("a")
        if team_link is None:
            continue
        team_code = team_link.get_text(strip=True).lower()
        pairs.append((season_code, team_code))

    if rows_seen > 0 and not pairs:
        # The table exists but nothing we tried matched a single row --
        # almost certainly means the data-stat attribute names or table
        # structure assumed here (season / team_name_abbr) don't match what
        # this page actually uses. Dump the first row's real attributes so
        # this can be fixed precisely instead of guessed at again.
        first_row = table.find("tbody").find("tr")
        print(f"  DIAGNOSTIC for {player_id}: table found with {rows_seen} row(s) "
              f"but extracted 0 valid (season, team) pairs. First row's actual "
              f"cells (name=data-stat, tag, text):")
        for cell in first_row.find_all(["th", "td"]):
            print(f"    data-stat={cell.get('data-stat')!r:30s} "
                  f"tag={cell.name:3s} text={cell.get_text(strip=True)!r}")

    return pairs


def ensure_live_conn(conn):
    """Checks the connection is actually alive and reconnects if not.
    A total network outage (the kind that also produces DNS resolution
    failures on the hockey-reference requests) can leave the DB connection
    dead too -- without this, the next query after such an outage crashes
    the whole run instead of just that one operation."""
    try:
        if conn.closed:
            raise psycopg2.OperationalError("connection reports closed")
        with conn.cursor() as cur:
            cur.execute("select 1")
        return conn
    except Exception:
        print("  DB connection appears dead -- reconnecting...")
        try:
            conn.close()
        except Exception:
            pass
        return get_db_conn()


def fetch_team_season_games(conn, season_code, team_code):
    """Games table already has this -- no schedule re-scraping needed."""
    with conn.cursor() as cur:
        cur.execute(
            "select game_id, date, home_team, away_team from games "
            "where season = %s and playoff = false "
            "and (home_team = %s or away_team = %s) order by date",
            (season_code, team_code, team_code),
        )
        return cur.fetchall()


def player_already_captured(conn, game_id, player_id):
    with conn.cursor() as cur:
        cur.execute(
            "select 1 from player_game_appearances where game_id = %s and player_id = %s "
            "union select 1 from goalie_game_appearances where game_id = %s and player_id = %s",
            (game_id, player_id, game_id, player_id),
        )
        return cur.fetchone() is not None


def scrape_one_player_row(game_id, date, home_team, away_team, target_player_id):
    """Fetches one box score and returns rows (skater/goalie/advanced) for
    JUST the target player, if present -- not the whole game's roster."""
    code = build_boxscore_code(date, home_team)
    url = BASE_URL.format(game_code=code)
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    skaters, goalies, advanced = [], [], []
    for team_code in (home_team, away_team):
        t_upper = team_code.upper()
        sk_table = get_soup_table(soup, f"{t_upper}_skaters")
        go_table = get_soup_table(soup, f"{t_upper}_goalies")
        adv_table = get_soup_table(soup, f"{t_upper}_adv_ALLAll")
        if sk_table is not None:
            skaters += [r for r in parse_skater_table(sk_table, game_id, team_code)
                        if r["player_id"] == target_player_id]
        if go_table is not None:
            goalies += [r for r in parse_goalie_table(go_table, game_id, team_code)
                        if r["player_id"] == target_player_id]
        if adv_table is not None:
            advanced += [r for r in parse_advanced_table(adv_table, game_id, team_code)
                         if r["player_id"] == target_player_id]
    return skaters, goalies, advanced


def refresh_materialized_views(conn):
    print("Refreshing materialized views...")
    with conn.cursor() as cur:
        cur.execute("refresh materialized view v_player_cumulative_stats")
        cur.execute("refresh materialized view v_pvadj")
        cur.execute("refresh materialized view v_team_cumulative_stats")
        cur.execute("refresh materialized view v_team_game_pvadjsum")
    conn.commit()
    print("  done.")


def main():
    players = load_affected_players()
    print(f"Loaded {len(players)} period-slug player(s) from {INPUT_CSV}")
    print(f"DRY_RUN={DRY_RUN}\n")

    conn = get_db_conn()
    total_inserted = 0
    try:
        for p in players:
            player_id = p["player_id"]
            if SKIP_IRRELEVANT_PLAYERS and player_id in KNOWN_IRRELEVANT_PLAYER_IDS:
                print(f"=== {p['player_name_display']} ({player_id}) === "
                      f"skipping -- confirmed not in our 1718-2526 migrated range")
                continue
            print(f"=== {p['player_name_display']} ({player_id}) ===")

            team_seasons = fetch_player_team_seasons(player_id)
            time.sleep(REQUEST_DELAY_SECONDS)
            if not team_seasons:
                print("  no team/season rows found in our migrated range, skipping")
                continue

            players_rows = [{"player_id": player_id,
                              "player_name_display": p["player_name_display"],
                              "position": None}]
            # Insert the player row itself once, up front -- everything else
            # this run does depends on it existing (FK on player_game_appearances).
            conn = ensure_live_conn(conn)
            upsert(conn, "players", players_rows, ["player_id"],
                   ["player_id", "player_name_display", "position"])

            for season_code, team_code in team_seasons:
                conn = ensure_live_conn(conn)
                games = fetch_team_season_games(conn, season_code, team_code)
                missing_games = [
                    g for g in games
                    if not player_already_captured(conn, g[0], player_id)
                ]
                print(f"  {season_code}/{team_code}: {len(games)} games, "
                      f"{len(missing_games)} missing this player")

                season_skaters, season_goalies, season_advanced = [], [], []
                for game_id, date, home_team, away_team in missing_games:
                    try:
                        skaters, goalies, advanced = scrape_one_player_row(
                            game_id, date, home_team, away_team, player_id)
                    except Exception as e:
                        print(f"    ERROR scraping {game_id}: {e}")
                        continue
                    if not (skaters or goalies):
                        print(f"    {game_id}: player not found in this box score "
                              f"(scratched/injured?) -- skipping")
                    season_skaters += skaters
                    season_goalies += goalies
                    season_advanced += advanced
                    time.sleep(REQUEST_DELAY_SECONDS)

                # Commit after every season, not after the player's entire
                # career -- a long multi-season backfill (Compher: 9 seasons)
                # would otherwise save nothing at all until fully finished,
                # and lose everything if interrupted partway through.
                if season_skaters or season_goalies:
                    print(f"  {season_code}/{team_code}: inserting "
                          f"{len(season_skaters)} skater row(s), "
                          f"{len(season_goalies)} goalie row(s), "
                          f"{len(season_advanced)} advanced row(s)")
                    total_inserted += len(season_skaters) + len(season_goalies)
                    conn = ensure_live_conn(conn)
                    upsert(conn, "player_game_appearances", season_skaters, ["game_id", "player_id"],
                           ["game_id", "player_id", "team_code", "goals", "assists", "points", "plus_minus",
                            "pim", "ev_goals", "pp_goals", "sh_goals", "gw_goals", "ev_assists", "pp_assists",
                            "sh_assists", "shots", "shot_pct", "shifts", "toi_seconds"])
                    upsert(conn, "goalie_game_appearances", season_goalies, ["game_id", "player_id"],
                           ["game_id", "player_id", "team_code", "decision", "goals_against", "shots_against",
                            "saves", "sv_pct", "shutout", "toi_seconds"])
                    upsert(conn, "player_advanced_game_appearances", season_advanced, ["game_id", "player_id"],
                           ["game_id", "player_id", "team_code", "icf", "sat_for", "sat_against", "cf_pct",
                            "crel_pct", "zone_start_off", "zone_start_def", "off_zone_start_pct", "hits", "blocks"])
                else:
                    print(f"  {season_code}/{team_code}: nothing to insert")
            print()

        print(f"\nTotal appearance rows inserted across all affected players: {total_inserted}")

        if not DRY_RUN and total_inserted > 0:
            refresh_materialized_views(conn)
        elif DRY_RUN:
            print("[dry-run] no writes performed, materialized views not refreshed")
    finally:
        conn.close()

    print("\nDone.")


if __name__ == "__main__":
    main()
