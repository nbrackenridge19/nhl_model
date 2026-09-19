"""
nhl_backfill_player_appearances.py — Phase 2 player-level backfill

Scrapes hockey-reference boxscore pages for each game already in Supabase's
`games` table, and populates: players, player_game_appearances,
goalie_game_appearances, player_advanced_game_appearances.

Reads the game list FROM Supabase (game_id, date, home_team, away_team) —
Phase 1 already migrated this, so no schedule re-scraping is needed. The
boxscore URL is built directly from that data:
    https://www.hockey-reference.com/boxscores/YYYYMMDD0<HOME_TEAM_UPPER>.html

SAMPLE MODE
-----------
Set LIMIT to test against a small number of games first and get a real
runtime estimate before committing to a full-season or historical backfill.
This is deliberate per the migration playbook: never run an unverified
scraper against the full historical range in one shot.

RATE LIMITING
-------------
A delay between requests is mandatory — hockey-reference will rate-limit or
block aggressive scraping. REQUEST_DELAY_SECONDS defaults to a conservative
3 seconds; only lower it if you've confirmed a faster rate doesn't trip
blocking.

SAFETY
------
- Same DATABASE connection convention as nhl_migrate_core.py: discrete
  PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT env vars, no DATABASE_URL.
- DRY_RUN defaults to true. Set DRY_RUN=false to actually write to Supabase.
- Player names get the same mojibake fix used in Phase 1's rookie/prior-
  season ingestion (encode('latin1').decode('utf-8')).
"""

import os
import re
import sys
import time
import requests
import psycopg2
from psycopg2.extras import execute_values
from bs4 import BeautifulSoup, Comment

SEASON = os.environ.get("NHL_SEASON", "2526")
LIMIT = int(os.environ.get("LIMIT", "20"))
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "3"))
BASE_URL = "https://www.hockey-reference.com/boxscores/{game_code}.html"

HEADERS = {"User-Agent": "Mozilla/5.0"}


def get_db_conn():
    if os.environ.get("PGHOST"):
        return psycopg2.connect()
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("No connection info found. Set PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT.")
    return psycopg2.connect(db_url)


def normalize_player_name(raw: str) -> str:
    if raw is None:
        return ""
    try:
        return raw.encode("latin1").decode("utf-8").strip()
    except (UnicodeDecodeError, UnicodeEncodeError):
        return raw.strip()


def fetch_game_list(limit):
    """Pulls (game_id, date, home_team, away_team) from Supabase's games table."""
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select game_id, date, home_team, away_team from games "
                "where season = %s and playoff = false order by date limit %s",
                (SEASON, limit),
            )
            return cur.fetchall()
    finally:
        conn.close()


def build_boxscore_code(date, home_team):
    return f"{date.strftime('%Y%m%d')}0{home_team.upper()}"


def get_soup_table(soup, table_id):
    """Finds a table by id, checking both live DOM and HTML-comment-hidden
    tables (a common hockey-reference pattern for secondary tables)."""
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


def extract_player_id(cell):
    a = cell.find("a")
    if a is None or "href" not in a.attrs:
        return None, normalize_player_name(cell.get_text(strip=True))
    href = a["href"]
    # [a-zA-Z0-9.]+ (not just [a-zA-Z0-9]+): a small number of hockey-reference
    # player-id slugs contain a literal period (e.g. J.T. Compher -> comphj.01).
    # The old alphanumeric-only class couldn't match those hrefs at all, so
    # re.search returned None and the player was silently dropped everywhere
    # they appeared -- not just one game, every game. Greedy backtracking on
    # the wider class still finds the correct split (slug vs trailing .html).
    m = re.search(r"/players/[a-z]/([a-zA-Z0-9.]+)\.html", href)
    player_id = m.group(1) if m else None
    return player_id, normalize_player_name(a.get_text(strip=True))


def parse_toi_seconds(toi_str):
    if not toi_str or ":" not in toi_str:
        return None
    minutes, seconds = toi_str.split(":")
    return int(minutes) * 60 + int(seconds)


def parse_int(val):
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def parse_float(val):
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def parse_skater_table(table, game_id, team_code):
    rows = []
    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells or cells[0].get_text(strip=True) == "":
            continue
        stat = lambda i: cells[i].get_text(strip=True) if i < len(cells) else None
        player_cell = tr.find("td", {"data-stat": "player"}) or (cells[1] if len(cells) > 1 else None)
        if player_cell is None:
            continue
        player_id, player_name = extract_player_id(player_cell)
        if player_id is None:
            continue  # skip TOTAL row / rows without a player link
        row = {c.get("data-stat"): c.get_text(strip=True) for c in cells}
        rows.append({
            "game_id": game_id, "player_id": player_id, "team_code": team_code,
            "player_name_display": player_name,
            "goals": parse_int(row.get("goals")), "assists": parse_int(row.get("assists")),
            "points": parse_int(row.get("points")), "plus_minus": parse_int(row.get("plus_minus")),
            "pim": parse_int(row.get("pen_min")),
            "ev_goals": parse_int(row.get("goals_ev")), "pp_goals": parse_int(row.get("goals_pp")),
            "sh_goals": parse_int(row.get("goals_sh")), "gw_goals": parse_int(row.get("goals_gw")),
            "ev_assists": parse_int(row.get("assists_ev")), "pp_assists": parse_int(row.get("assists_pp")),
            "sh_assists": parse_int(row.get("assists_sh")),
            "shots": parse_int(row.get("shots")), "shot_pct": parse_float(row.get("shot_pct")),
            "shifts": parse_int(row.get("shifts")),
            "toi_seconds": parse_toi_seconds(row.get("time_on_ice")),
        })
    return rows


def parse_goalie_table(table, game_id, team_code):
    rows = []
    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all(["th", "td"])
        row = {c.get("data-stat"): c.get_text(strip=True) for c in cells}
        player_cell = tr.find("td", {"data-stat": "player"})
        if player_cell is None:
            continue
        player_id, player_name = extract_player_id(player_cell)
        if player_id is None:
            continue  # e.g. "Empty Net" rows have no player link — skip
        rows.append({
            "game_id": game_id, "player_id": player_id, "team_code": team_code,
            "player_name_display": player_name,
            "decision": row.get("decision") or None,
            "goals_against": parse_int(row.get("goals_against")),
            "shots_against": parse_int(row.get("shots_against")),
            "saves": parse_int(row.get("saves")),
            "sv_pct": parse_float(row.get("save_pct")),
            "shutout": parse_int(row.get("shutouts")) == 1 if row.get("shutouts") else False,
            "toi_seconds": parse_toi_seconds(row.get("time_on_ice")),
        })
    return rows


def parse_advanced_table(table, game_id, team_code):
    """Only the first ('All Situations') advanced table per team — the
    page also has PP/SH/Close/5v5 situational splits which are skipped."""
    rows = []
    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all(["th", "td"])
        row = {c.get("data-stat"): c.get_text(strip=True) for c in cells}
        player_cell = tr.find("td", {"data-stat": "player"}) or (cells[0] if cells else None)
        if player_cell is None:
            continue
        player_id, _ = extract_player_id(player_cell)
        if player_id is None:
            continue
        rows.append({
            "game_id": game_id, "player_id": player_id, "team_code": team_code,
            "icf": parse_int(row.get("Cevents")),
            "sat_for": parse_int(row.get("on_Cevents")), "sat_against": parse_int(row.get("on_opp_Cevents")),
            "cf_pct": parse_float(row.get("corsi_for")), "crel_pct": parse_float(row.get("corsi_rel")),
            "zone_start_off": parse_int(row.get("zs_off")),
            "zone_start_def": parse_int(row.get("zs_def")),
            "off_zone_start_pct": parse_float(row.get("ozs_pct")),
            "hits": parse_int(row.get("hits")), "blocks": parse_int(row.get("blocks")),
        })
    return rows


def scrape_game(game_id, date, home_team, away_team):
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
            skaters += parse_skater_table(sk_table, game_id, team_code)
        if go_table is not None:
            goalies += parse_goalie_table(go_table, game_id, team_code)
        if adv_table is not None:
            advanced += parse_advanced_table(adv_table, game_id, team_code)
    return skaters, goalies, advanced


def upsert(conn, table, rows, conflict_cols, all_cols):
    if not rows:
        print(f"  [skip] {table}: no rows")
        return
    cols = all_cols
    values = [tuple(r.get(c) for c in cols) for r in rows]
    update_cols = [c for c in cols if c not in conflict_cols]
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols) if update_cols else None
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s ON CONFLICT ({', '.join(conflict_cols)}) "
    sql += f"DO UPDATE SET {set_clause}" if set_clause else "DO NOTHING"
    if DRY_RUN:
        print(f"  [dry-run] {table}: would upsert {len(values)} rows")
        return
    with conn.cursor() as cur:
        execute_values(cur, sql, values)
    conn.commit()
    print(f"  [live] {table}: upserted {len(values)} rows")


def dump_table_ids(game_id, date, home_team, away_team):
    """Diagnostic: prints every table id found on the page (live DOM and
    inside HTML comments) so the advanced-table id pattern can be fixed
    with certainty instead of guessed again."""
    code = build_boxscore_code(date, home_team)
    url = BASE_URL.format(game_code=code)
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    print(f"URL: {url}\n")
    print("Live DOM table ids:")
    for t in soup.find_all("table"):
        if t.get("id"):
            print(f"  {t['id']}")

    print("\nComment-hidden table ids:")
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment_soup = BeautifulSoup(comment, "html.parser")
        for t in comment_soup.find_all("table"):
            if t.get("id"):
                print(f"  {t['id']}")

    # Also dump the header row (data-stat attributes) of one skater table
    # and one advanced table, if found by any id containing these hints.
    for hint in ["skaters", "adv"]:
        for t in soup.find_all("table"):
            if t.get("id") and hint in t["id"].lower():
                thead = t.find("thead")
                if thead:
                    headers = [th.get("data-stat") for th in thead.find_all("th")]
                    print(f"\nHeaders for table id='{t['id']}':\n  {headers}")
                break


def refresh_materialized_views():
    """Refreshes v_player_cumulative_stats and v_pvadj so newly scraped
    games are reflected without a manual step."""
    print("Refreshing materialized views (v_player_cumulative_stats, v_pvadj)...")
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("refresh materialized view v_player_cumulative_stats")
            cur.execute("refresh materialized view v_pvadj")
            cur.execute("refresh materialized view v_team_cumulative_stats")
            cur.execute("refresh materialized view v_team_game_pvadjsum")
        conn.commit()
        print("  done.")
    finally:
        conn.close()


def main():
    if os.environ.get("DEBUG_DUMP_TABLES"):
        games = fetch_game_list(1)
        dump_table_ids(*games[0])
        return

    print(f"Phase 2 backfill — season {SEASON} — LIMIT={LIMIT} — DRY_RUN={DRY_RUN}")
    games = fetch_game_list(LIMIT)
    print(f"Fetched {len(games)} games from Supabase to scrape.\n")

    all_players, all_skaters, all_goalies, all_advanced = {}, [], [], []
    start = time.time()
    checkpoint_every = int(os.environ.get("CHECKPOINT_EVERY", "50"))
    conn = get_db_conn() if not DRY_RUN else None

    def flush(players_batch, skaters_batch, goalies_batch, advanced_batch, label):
        print(f"  --- checkpoint ({label}) ---")
        upsert(conn, "players", players_batch, ["player_id"],
               ["player_id", "player_name_display", "position"])
        upsert(conn, "player_game_appearances", skaters_batch, ["game_id", "player_id"],
               ["game_id", "player_id", "team_code", "goals", "assists", "points", "plus_minus",
                "pim", "ev_goals", "pp_goals", "sh_goals", "gw_goals", "ev_assists", "pp_assists",
                "sh_assists", "shots", "shot_pct", "shifts", "toi_seconds"])
        upsert(conn, "goalie_game_appearances", goalies_batch, ["game_id", "player_id"],
               ["game_id", "player_id", "team_code", "decision", "goals_against", "shots_against",
                "saves", "sv_pct", "shutout", "toi_seconds"])
        upsert(conn, "player_advanced_game_appearances", advanced_batch, ["game_id", "player_id"],
               ["game_id", "player_id", "team_code", "icf", "sat_for", "sat_against", "cf_pct",
                "crel_pct", "zone_start_off", "zone_start_def", "off_zone_start_pct", "hits", "blocks"])

    batch_players, batch_skaters, batch_goalies, batch_advanced = {}, [], [], []

    for i, (game_id, date, home_team, away_team) in enumerate(games, 1):
        print(f"[{i}/{len(games)}] {game_id} ({date}, {away_team} @ {home_team})")
        try:
            skaters, goalies, advanced = scrape_game(game_id, date, home_team, away_team)
        except Exception as e:
            print(f"  ERROR scraping {game_id}: {e}")
            continue

        for r in skaters + goalies:
            all_players[r["player_id"]] = r["player_name_display"]
            batch_players[r["player_id"]] = r["player_name_display"]
        all_skaters += skaters
        all_goalies += goalies
        all_advanced += advanced
        batch_skaters += skaters
        batch_goalies += goalies
        batch_advanced += advanced

        if not DRY_RUN and i % checkpoint_every == 0:
            players_rows = [{"player_id": pid, "player_name_display": name, "position": None}
                             for pid, name in batch_players.items()]
            flush(players_rows, batch_skaters, batch_goalies, batch_advanced, f"game {i}/{len(games)}")
            batch_players, batch_skaters, batch_goalies, batch_advanced = {}, [], [], []

        if i < len(games):
            time.sleep(REQUEST_DELAY_SECONDS)

    elapsed = time.time() - start
    per_game = elapsed / max(len(games), 1)
    print(f"\nScraped {len(games)} games in {elapsed:.1f}s ({per_game:.2f}s/game).")
    for label, n in [("82-game season (per team, ~1,312 total)", 1312),
                     ("full 2025-26 season (1,394 games)", 1394),
                     ("9-season backfill 2017-18..2025-26 (~11,800 games)", 11800)]:
        est_hours = (per_game * n) / 3600
        print(f"  Extrapolated for {label}: ~{est_hours:.1f} hours")

    players_rows = [{"player_id": pid, "player_name_display": name, "position": None}
                     for pid, name in all_players.items()]

    print(f"\nRow counts: players={len(players_rows)}, skaters={len(all_skaters)}, "
          f"goalies={len(all_goalies)}, advanced={len(all_advanced)}")

    try:
        if not DRY_RUN:
            print("\nFinal flush (rows since last checkpoint)...")
            flush(batch_players and [{"player_id": pid, "player_name_display": name, "position": None}
                                      for pid, name in batch_players.items()] or [],
                  batch_skaters, batch_goalies, batch_advanced, "final")
        else:
            print("\n[dry-run] would flush all rows — no writes performed")
    finally:
        if conn:
            conn.close()

    if not DRY_RUN:
        refresh_materialized_views()

    print("\nDone.")


if __name__ == "__main__":
    main()
