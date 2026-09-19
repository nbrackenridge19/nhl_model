"""
nhl_full_historical_backfill.py

Single-run historical backfill covering both:
  Phase 1 — core tables (teams, team_seasons, games, team_game_stats,
            odds, preseason_points) migrated from each season's Excel file.
  Phase 2 — per-game player/goalie/advanced-stat scraping from
            hockey-reference, checkpointed every CHECKPOINT_EVERY games.

Runs for all 8 historical seasons (2017-18 through 2024-25) in one
execution, season by season, in order. 2025-26 is intentionally excluded —
it was already fully migrated and scraped in an earlier session; re-running
it here would just cost ~1.3 hours for no new data.

RESUME-SAFETY
-------------
If this script is interrupted (network drop, laptop sleep, etc.) and
re-run, it:
  - Always re-runs Phase 1 for every season (cheap — a few seconds each,
    fully idempotent, no harm in repeating).
  - SKIPS Phase 2 scraping for any season where the number of distinct
    games already in player_game_appearances for that season already
    covers every game in that season's `games` table — so a crash on,
    say, season 5 of 8 does not force re-scraping seasons 1-4 (~1.3 hrs
    each) when you restart.
  - Within the season it was interrupted on, all games get re-scraped
    (there's no per-game resume within a season) — this is a deliberate
    simplicity trade-off; upserts make it harmless, just slower.

SAFETY
------
- DB connection: PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT env vars only
  (same convention as before) — falls back to DATABASE_URL if PGHOST unset.
- DRY_RUN defaults to true. Set DRY_RUN=false to actually write anything.
- Player names get the mojibake fix (encode('latin1').decode('utf-8')).
- Point NHL_DATA_DIR at the folder containing all 8 season files if
  they're not in the current directory.
"""

import os
import time
import pandas as pd
import openpyxl
import psycopg2
from psycopg2.extras import execute_values
from bs4 import BeautifulSoup, Comment
import re
import requests

DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "3"))
CHECKPOINT_EVERY = int(os.environ.get("CHECKPOINT_EVERY", "50"))
DATA_DIR = os.environ.get("NHL_DATA_DIR", ".")
BASE_URL = "https://www.hockey-reference.com/boxscores/{game_code}.html"
HEADERS = {"User-Agent": "Mozilla/5.0"}

SEASONS = [
    ("1718", 1718, "nhl1718.xlsx", "gamelog1718.xlsm", "rookies1718", "1617"),
    ("1819", 1819, "nhl1819.xlsx", "gamelog1819.xlsm", "rookies1819", "1718"),
    ("1920", 1920, "nhl1920.xlsx", "gamelog1920.xlsm", "rookies1920", "1819"),
    ("2021", 2021, "nhl2021.xlsx", "gamelog2021.xlsm", "rookies2021", "1920"),
    ("2122", 2122, "nhl2122.xlsx", "gamelog2122.xlsm", "rookies2122", "2021"),
    ("2223", 2223, "nhl2223.xlsm", "gamelog2223.xlsm", "rookies2223", "2122"),
    ("2324", 2324, "nhl2324.xlsm", "gamelog2324.xlsm", "rookies2324", "2223"),
    ("2425", 2425, "nhl2425.xlsm", "gamelog2425.xlsm", "rookies2425", "2324"),
]

FRANCHISE_MAP = {"ari": "ari_uta", "uta": "ari_uta"}

# Team code spelling changed partway through the historical files —
# 1718 through 2122 use 'vgk'/'was'; 2223 onward use 'veg'/'wsh' (already
# the convention used in Supabase from the 2425/2526 migrations). Without
# normalizing this, the same franchise would silently split into two
# different team_code identities across seasons.
TEAM_CODE_NORMALIZE = {"vgk": "veg", "was": "wsh"}


def normalize_team_code(code):
    return TEAM_CODE_NORMALIZE.get(code, code)


def franchise_id(team_code):
    return FRANCHISE_MAP.get(team_code, team_code)


def normalize_player_name(raw):
    if raw is None:
        return "", ""
    try:
        display = raw.encode("latin1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        display = raw
    return display.strip().lower(), display.strip()


def load_prior_season_stats(gamelog_path, prior_season_code):
    """allplayers<PRIOR_SEASON> sheet — last season's final per-player
    PV/FOV, used as the blending baseline for the current season's PVAdj
    formula. Column order and FW/FL vs FOW/FOL naming both drift across
    historical files; selected by name, not position, to handle this."""
    df = load_sheet_df(gamelog_path, f"allplayers{prior_season_code}")
    rename_map = {}
    if "FOW" in df.columns:
        rename_map["FOW"] = "FW"
    if "FOL" in df.columns:
        rename_map["FOL"] = "FL"
    df = df.rename(columns=rename_map)
    keep = [c for c in ["Player", "Pos", "GP", "TP", "TOI", "FW", "FL", "PPG", "ATOI", "PV", "FOV"]
            if c in df.columns]
    df = df[keep].dropna(subset=["Player"])
    keys = df["Player"].apply(normalize_player_name)
    df["player_name_normalized"] = keys.apply(lambda t: t[0])
    df["player_name_display"] = keys.apply(lambda t: t[1])
    df["season"] = prior_season_code
    df = df.rename(columns={
        "Pos": "pos", "GP": "gp", "TP": "tp", "TOI": "toi", "FW": "fw", "FL": "fl",
        "PPG": "ppg", "ATOI": "atoi", "PV": "pv", "FOV": "fov",
    }).drop(columns=["Player"])

    dupe_mask = df.duplicated(subset=["player_name_normalized", "season"], keep=False)
    if dupe_mask.any():
        merged_rows = []
        collision_rows = []
        for key, group in df[dupe_mask].groupby(["player_name_normalized", "season"]):
            if group["pos"].nunique() == 1:
                # Same person, split across a mid-season trade (multiple team
                # stints) — combine into one full-season row and recompute
                # PV/FOV the same way it's computed for any established
                # player elsewhere, rather than keeping one incomplete stint.
                gp = group["gp"].sum()
                tp = group["tp"].sum()
                toi = group["toi"].sum()
                # FW/FL are stored as an already-combined season total,
                # repeated identically on every stint row (unlike GP/TP/TOI,
                # which are genuinely split per stint) — confirmed via
                # Paul Stastny's 1718 rows showing identical 832/683 on
                # both. Take one copy, not a sum, or it silently doubles.
                fw = group["fw"].max() if "fw" in group else 0
                fl = group["fl"].max() if "fl" in group else 0
                atoi = toi / gp if gp else 0
                ppg = tp / gp if gp else 0
                merged_rows.append({
                    "pos": group["pos"].iloc[0], "gp": gp, "tp": tp, "toi": toi,
                    "fw": fw, "fl": fl, "ppg": ppg, "atoi": atoi,
                    "pv": atoi + atoi * ppg,
                    "fov": ((fw - fl) / gp) if gp and (fw or fl) else 0,
                    "player_name_normalized": key[0],
                    "player_name_display": group["player_name_display"].iloc[0],
                    "season": key[1],
                })
            else:
                # Genuinely two different real people sharing a name (found:
                # 1718's Sebastian Aho — a defenseman and the Carolina
                # forward are different players). No safe way to merge;
                # arbitrarily keeping the first means whichever real player
                # isn't kept will get wrong data if ever looked up by name.
                collision_rows.append(group)

        if collision_rows:
            print(f"    WARNING: name collision between different real players (not a data error) "
                  f"in {gamelog_path} — only one will be usable via name-based lookup:")
            print(pd.concat(collision_rows).sort_values("player_name_normalized").to_string(index=False))

        df = df[~dupe_mask]
        if merged_rows:
            df = pd.concat([df, pd.DataFrame(merged_rows)], ignore_index=True)
        if collision_rows:
            df = pd.concat([df, pd.concat(collision_rows).drop_duplicates(
                subset=["player_name_normalized", "season"], keep="first")], ignore_index=True)

    return df


def load_rookie_projections(gamelog_path, rookie_sheet, season_code):
    """Main rookie list (Player, Pos, Round, [FOV if present]) — PV/FOV
    are NOT stored; they're derived via v_rookie_projections from the
    (season-independent, empirically confirmed identical across every
    historical file) round/position lookup tables already in Supabase.
    Some early seasons (1718-1920) have no FOV column at all."""
    wb = openpyxl.load_workbook(gamelog_path, data_only=True)
    ws = wb[rookie_sheet]
    header = [ws.cell(row=1, column=c).value for c in range(1, 6)]
    has_fov = "FOV" in header
    max_col = 5 if has_fov else 4
    rows = []
    for row in ws.iter_rows(min_row=2, max_col=max_col, values_only=True):
        rows.append(row)
    df = pd.DataFrame([(r[0], r[1], r[2]) for r in rows], columns=["Player", "Pos", "Round"])
    df = df.dropna(subset=["Player"])
    keys = df["Player"].apply(normalize_player_name)
    df["player_name_normalized"] = keys.apply(lambda t: t[0])
    df["player_name_display"] = keys.apply(lambda t: t[1])
    df["season"] = season_code
    df = df.rename(columns={"Pos": "pos", "Round": "draft_round"}).drop(columns=["Player"])
    df["draft_round"] = df["draft_round"].astype(str)

    dupe_mask = df.duplicated(subset=["player_name_normalized", "season"], keep=False)
    if dupe_mask.any():
        conflicting = df[dupe_mask].groupby("player_name_normalized").filter(
            lambda g: g[["pos", "draft_round"]].nunique().gt(1).any()
        )
        if not conflicting.empty:
            print(f"    WARNING: conflicting duplicate rookie rows in {rookie_sheet} "
                  f"(kept the 'U'/aged-out row where one exists, dropped the rest):")
            print(conflicting.sort_values("player_name_normalized").to_string(index=False))
        df["_sort_key"] = (df["draft_round"] != "U").astype(int)
        df = df.sort_values(["player_name_normalized", "_sort_key"])
        df = df.drop_duplicates(subset=["player_name_normalized", "season"], keep="first")
        df = df.drop(columns=["_sort_key"]).reset_index(drop=True)

    return df


def get_db_conn():
    if os.environ.get("PGHOST"):
        return psycopg2.connect()
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("No connection info found. Set PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT.")
    return psycopg2.connect(db_url)


def upsert(conn_factory, table, rows, conflict_cols, all_cols):
    if not rows:
        print(f"    [skip] {table}: no rows")
        return
    values = [tuple(r.get(c) for c in all_cols) for r in rows]
    update_cols = [c for c in all_cols if c not in conflict_cols]
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols) if update_cols else None
    sql = f"INSERT INTO {table} ({', '.join(all_cols)}) VALUES %s ON CONFLICT ({', '.join(conflict_cols)}) "
    sql += f"DO UPDATE SET {set_clause}" if set_clause else "DO NOTHING"
    if DRY_RUN:
        print(f"    [dry-run] {table}: would upsert {len(values)} rows")
        return

    # Fresh, short-lived connection per operation — a single connection held
    # open across a multi-hour run will eventually get force-closed by
    # Supabase's pooler regardless of activity. One retry on top of that in
    # case of a genuinely transient drop.
    for attempt in range(2):
        try:
            conn = conn_factory()
            try:
                with conn.cursor() as cur:
                    execute_values(cur, sql, values)
                conn.commit()
                print(f"    [live] {table}: upserted {len(values)} rows")
                return
            finally:
                conn.close()
        except psycopg2.OperationalError as e:
            if attempt == 0:
                print(f"    WARNING: connection issue on {table} upsert ({e}); retrying once...")
                time.sleep(2)
            else:
                raise


# --------------------------- Phase 1: core tables ---------------------------

def load_sheet_df(path, sheet):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet]
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    rows = [dict(zip(header, row)) for row in ws.iter_rows(min_row=2, values_only=True)]
    return pd.DataFrame(rows)


def load_prept(path, season_num):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["PrePT"]
    header = [c.value for c in next(ws.iter_rows(min_row=2, max_row=2))]
    rows = [dict(zip(header, row)) for row in ws.iter_rows(min_row=3, values_only=True)]
    df = pd.DataFrame(rows)
    if season_num not in df.columns:
        return pd.DataFrame(columns=["team_code", "vegas_point_total"])
    df = df[["Team", season_num]].rename(columns={"Team": "team_code", season_num: "vegas_point_total"})
    df = df.dropna(subset=["team_code"])  # drop blank trailing rows in the source sheet
    df["team_code"] = df["team_code"].map(normalize_team_code)
    return df


def migrate_core_for_season(season_code, season_num, source_file):
    print(f"  Phase 1: migrating core tables from {source_file} ...")
    data_df = load_sheet_df(source_file, "Data")
    gdd_df = load_sheet_df(source_file, "GDD")
    prept_df = load_prept(source_file, season_num)

    data_df = data_df[data_df["Season"] == season_num].copy()
    gdd_df = gdd_df[gdd_df["Season"] == season_num].copy()
    data_df["Team"] = data_df["Team"].map(normalize_team_code)

    # Guard against duplicate TeamGame rows in GDD (found in 2324: some
    # playoff rows were duplicated with one copy left as an all-zero blank
    # template). Keep whichever copy actually has real data rather than an
    # arbitrary one.
    if gdd_df.duplicated(subset=["TeamGame"]).any():
        dup_teamgames = sorted(gdd_df[gdd_df.duplicated(subset=["TeamGame"], keep=False)]["TeamGame"].unique())
        print(f"    WARNING: {len(dup_teamgames)} TeamGame value(s) have duplicate rows in GDD "
              f"(keeping the non-blank variant): {dup_teamgames}")
        gdd_df = gdd_df.reindex(gdd_df["GDD"].abs().sort_values(ascending=False).index)
        gdd_df = gdd_df.drop_duplicates(subset=["TeamGame"], keep="first")

    team_codes = sorted(set(data_df["Team"]))
    teams = [{"team_code": t, "team_name": t} for t in team_codes]
    team_seasons = [{"team_code": t, "season": season_code, "franchise_id": franchise_id(t)} for t in team_codes]

    # Derive opponent by pairing each LGame's two team-rows — 'Opp' doesn't
    # exist as a column in most historical files (only 2425/2526 have it).
    home_rows = data_df[data_df["Home"] == 1]
    away_rows = data_df[data_df["Home"] == 0]

    # Guard against duplicate LGame values (found in 2324: a data-entry bug
    # assigned the same playoff game number to two different real games).
    # Rather than guess which row is correct, skip the ambiguous ones and
    # flag them clearly — this is a source-sheet fix, not a code fix.
    dup_lgames = set(home_rows[home_rows.duplicated(subset=["LGame"], keep=False)]["LGame"]) | \
                 set(away_rows[away_rows.duplicated(subset=["LGame"], keep=False)]["LGame"])
    if dup_lgames:
        print(f"    WARNING: {len(dup_lgames)} LGame value(s) have duplicate rows in the source "
              f"sheet and will be SKIPPED (needs manual fix in the sheet): {sorted(dup_lgames)}")
        home_rows = home_rows[~home_rows["LGame"].isin(dup_lgames)]
        away_rows = away_rows[~away_rows["LGame"].isin(dup_lgames)]

    away_rows_indexed = away_rows.set_index("LGame")
    games = []
    for _, hr in home_rows.iterrows():
        lgame = hr["LGame"]
        away_team = away_rows_indexed.loc[lgame, "Team"] if lgame in away_rows_indexed.index else None
        games.append({
            "game_id": f"{season_code}_{lgame}", "season": season_code, "date": hr["Date"],
            "home_team": hr["Team"], "away_team": away_team,
            "playoff": bool(hr["Playoff"]), "home_goals": hr["GF"], "away_goals": hr["GA"],
        })

    merged = data_df.merge(
        gdd_df[["TeamGame", "SvPctR", "CFPct", "PP", "PK", "Back", "FOVt", "PVAdjSum"]],
        on="TeamGame", how="left",
    )
    if dup_lgames:
        merged = merged[~merged["LGame"].isin(dup_lgames)]
    team_game_stats = [{
        "game_id": f"{season_code}_{r.LGame}", "team_code": r.Team, "home": bool(r.Home),
        "attendance": r.Att, "goalie": r.Goalie, "corsi_for_pct": r.CFPct,
        "sv_pct_above_expected": r.SvPctR, "pp": r.PP, "pk": r.PK, "back_to_back": bool(r.Back),
        "faceoff_value": r.FOVt, "pvadj_sum_snapshot": r.PVAdjSum,
    } for r in merged.itertuples()]

    odds_df = data_df[pd.to_numeric(data_df["ML"], errors="coerce").notna()]
    if dup_lgames:
        odds_df = odds_df[~odds_df["LGame"].isin(dup_lgames)]
    odds = [{"game_id": f"{season_code}_{r.LGame}", "team_code": r.Team,
             "moneyline": r.ML, "sportsbook": "unspecified_closing"} for r in odds_df.itertuples()]

    preseason_points = [{"team_code": r.team_code, "season": season_code,
                          "vegas_point_total": r.vegas_point_total} for r in prept_df.itertuples()]

    print(f"    teams={len(teams)} games={len(games)} team_game_stats={len(team_game_stats)} "
          f"odds={len(odds)} preseason_points={len(preseason_points)}")

    upsert(get_db_conn, "teams", teams, ["team_code"], ["team_code", "team_name"])
    upsert(get_db_conn, "team_seasons", team_seasons, ["team_code", "season"], ["team_code", "season", "franchise_id"])
    upsert(get_db_conn, "games", games, ["game_id"],
           ["game_id", "season", "date", "home_team", "away_team", "playoff", "home_goals", "away_goals"])
    upsert(get_db_conn, "team_game_stats", team_game_stats, ["game_id", "team_code"],
           ["game_id", "team_code", "home", "attendance", "goalie", "corsi_for_pct",
            "sv_pct_above_expected", "pp", "pk", "back_to_back", "faceoff_value", "pvadj_sum_snapshot"])
    upsert(get_db_conn, "odds", odds, ["game_id", "team_code"], ["game_id", "team_code", "moneyline", "sportsbook"])
    upsert(get_db_conn, "preseason_points", preseason_points, ["team_code", "season"],
           ["team_code", "season", "vegas_point_total"])


# --------------------------- Phase 2: player scraping ---------------------------

def season_already_scraped(season_code):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("select count(*) from games where season = %s and playoff = false", (season_code,))
            total_games = cur.fetchone()[0]
            cur.execute(
                "select count(distinct pga.game_id) from player_game_appearances pga "
                "join games g on g.game_id = pga.game_id where g.season = %s and g.playoff = false",
                (season_code,),
            )
            scraped_games = cur.fetchone()[0]
        return total_games > 0 and scraped_games >= total_games
    finally:
        conn.close()


def fetch_game_list_for_season(season_code):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select game_id, date, home_team, away_team from games "
                "where season = %s and playoff = false order by date",
                (season_code,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def build_boxscore_code(date, home_team):
    return f"{date.strftime('%Y%m%d')}0{home_team.upper()}"


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


def extract_player_id(cell):
    a = cell.find("a")
    if a is None or "href" not in a.attrs:
        return None, normalize_player_name(cell.get_text(strip=True))[1]
    href = a["href"]
    # [a-zA-Z0-9.]+ (not just [a-zA-Z0-9]+): a small number of hockey-reference
    # player-id slugs contain a literal period (e.g. J.T. Compher -> comphj.01).
    # The old alphanumeric-only class couldn't match those hrefs at all, so
    # re.search returned None and the player was silently dropped everywhere
    # they appeared -- not just one game, every game, across every historical
    # season this script backfills. Greedy backtracking on the wider class
    # still finds the correct split (slug vs trailing .html).
    m = re.search(r"/players/[a-z]/([a-zA-Z0-9.]+)\.html", href)
    player_id = m.group(1) if m else None
    return player_id, normalize_player_name(a.get_text(strip=True))[1]


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
        if not cells:
            continue
        player_cell = tr.find("td", {"data-stat": "player"})
        if player_cell is None:
            continue
        player_id, player_name = extract_player_id(player_cell)
        if player_id is None:
            continue
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
            continue
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


def scrape_season(season_code):
    if season_already_scraped(season_code):
        print(f"  Phase 2: season {season_code} already fully scraped — skipping.")
        return

    games = fetch_game_list_for_season(season_code)
    test_limit = os.environ.get("NHL_TEST_GAME_LIMIT")
    if test_limit:
        games = games[: int(test_limit)]
        print(f"  Phase 2: NHL_TEST_GAME_LIMIT set — scraping only {len(games)} games for season {season_code} ...")
    else:
        print(f"  Phase 2: scraping {len(games)} games for season {season_code} ...")

    batch_players, batch_skaters, batch_goalies, batch_advanced = {}, [], [], []
    totals = {"skaters": 0, "goalies": 0, "advanced": 0}
    start = time.time()

    def flush(label):
        players_rows = [{"player_id": pid, "player_name_display": name, "position": None}
                         for pid, name in batch_players.items()]
        print(f"    --- checkpoint ({label}) ---")
        upsert(get_db_conn, "players", players_rows, ["player_id"],
               ["player_id", "player_name_display", "position"])
        upsert(get_db_conn, "player_game_appearances", batch_skaters, ["game_id", "player_id"],
               ["game_id", "player_id", "team_code", "goals", "assists", "points", "plus_minus",
                "pim", "ev_goals", "pp_goals", "sh_goals", "gw_goals", "ev_assists", "pp_assists",
                "sh_assists", "shots", "shot_pct", "shifts", "toi_seconds"])
        upsert(get_db_conn, "goalie_game_appearances", batch_goalies, ["game_id", "player_id"],
               ["game_id", "player_id", "team_code", "decision", "goals_against", "shots_against",
                "saves", "sv_pct", "shutout", "toi_seconds"])
        upsert(get_db_conn, "player_advanced_game_appearances", batch_advanced, ["game_id", "player_id"],
               ["game_id", "player_id", "team_code", "icf", "sat_for", "sat_against", "cf_pct",
                "crel_pct", "zone_start_off", "zone_start_def", "off_zone_start_pct", "hits", "blocks"])

    for i, (game_id, date, home_team, away_team) in enumerate(games, 1):
        print(f"    [{i}/{len(games)}] {game_id} ({date}, {away_team} @ {home_team})")
        try:
            skaters, goalies, advanced = scrape_game(game_id, date, home_team, away_team)
        except Exception as e:
            print(f"      ERROR scraping {game_id}: {e}")
            continue

        for r in skaters + goalies:
            batch_players[r["player_id"]] = r["player_name_display"]
        batch_skaters += skaters
        batch_goalies += goalies
        batch_advanced += advanced
        totals["skaters"] += len(skaters)
        totals["goalies"] += len(goalies)
        totals["advanced"] += len(advanced)

        if not DRY_RUN and i % CHECKPOINT_EVERY == 0:
            flush(f"game {i}/{len(games)}")
            batch_players, batch_skaters, batch_goalies, batch_advanced = {}, [], [], []

        if i < len(games):
            time.sleep(REQUEST_DELAY_SECONDS)

    if DRY_RUN:
        print(f"    [dry-run] would flush remaining rows — skaters={totals['skaters']}, "
              f"goalies={totals['goalies']}, advanced={totals['advanced']}")
    else:
        flush("final")

    elapsed = time.time() - start
    print(f"  Season {season_code} done: {len(games)} games in {elapsed:.1f}s "
          f"({elapsed/max(len(games),1):.2f}s/game).")


def refresh_materialized_views():
    """Refreshes the two materialized views (v_player_cumulative_stats,
    v_pvadj) so newly added games are reflected without a manual step.
    Uses a fresh connection, same pattern as everything else, since this
    can take a real moment on the full dataset."""
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
    print(f"Full historical backfill — DRY_RUN={DRY_RUN} — CHECKPOINT_EVERY={CHECKPOINT_EVERY}\n")

    season_filter = os.environ.get("NHL_SEASONS_FILTER")
    seasons_to_run = SEASONS
    if season_filter:
        wanted = {s.strip() for s in season_filter.split(",")}
        seasons_to_run = [s for s in SEASONS if s[0] in wanted]
        print(f"NHL_SEASONS_FILTER set — running only: {[s[0] for s in seasons_to_run]}\n")

    for season_code, season_num, filename, gamelog_filename, rookie_sheet, prior_season_code in seasons_to_run:
        source_file = os.path.join(DATA_DIR, filename)
        gamelog_file = os.path.join(DATA_DIR, gamelog_filename)
        print(f"=== Season {season_code} ({source_file}) ===")
        if not os.path.exists(source_file):
            print(f"  ERROR: file not found, skipping season: {source_file}")
            continue

        migrate_core_for_season(season_code, season_num, source_file)

        if not os.path.exists(gamelog_file):
            print(f"  WARNING: gamelog file not found ({gamelog_file}) — skipping "
                  f"prior_season_stats/rookie_projections for this season.")
        else:
            print(f"  Migrating prior_season_stats/rookie_projections from {gamelog_file} ...")
            pss = load_prior_season_stats(gamelog_file, prior_season_code)
            rp = load_rookie_projections(gamelog_file, rookie_sheet, season_code)
            print(f"    prior_season_stats={len(pss)} rookie_projections={len(rp)}")
            upsert(get_db_conn, "prior_season_stats", pss.to_dict("records"),
                   ["player_name_normalized", "season"],
                   ["player_name_normalized", "player_name_display", "season", "pos", "gp", "tp",
                    "toi", "fw", "fl", "ppg", "atoi", "pv", "fov"])
            upsert(get_db_conn, "rookie_projections", rp.to_dict("records"),
                   ["player_name_normalized", "season"],
                   ["player_name_normalized", "player_name_display", "season", "pos", "draft_round"])

        if os.environ.get("PGHOST"):
            scrape_season(season_code)
        else:
            print("  Phase 2: no DB connection available (pure dry-run with no PGHOST set) — skipping scrape step.")

        print()

    if not DRY_RUN and os.environ.get("PGHOST"):
        refresh_materialized_views()

    print("All seasons processed.")


if __name__ == "__main__":
    main()
