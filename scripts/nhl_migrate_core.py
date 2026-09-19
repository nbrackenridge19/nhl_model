"""
migrate_core.py — Phase 1 NHL model migration

Migrates ONE season (2025-26 / "2526") of core, team-level data from the
Excel source of record into Supabase. Deliberately excludes player-level
data (players, appearances, PVAdj) — that's Phase 2/3, once these tables
are verified.

Tables populated: teams, team_seasons, games, team_game_stats, odds,
preseason_points.

Source sheets read:
  - nhl2526.xlsx :: Data   (team-perspective row per team per game)
  - nhl2526.xlsx :: GDD    (raw per-team-per-game rate stats)
  - nhl2526.xlsx :: PrePT  (preseason Vegas point totals, wide by season)

SAFETY
------
- Credentials come ONLY from the DATABASE_URL environment variable.
  Never hardcode a connection string here or commit one to git.
    export DATABASE_URL="postgresql://user:pass@host:port/dbname"
- DRY_RUN defaults to true. You must explicitly set DRY_RUN=false to
  actually write to the database. This is checked at runtime (not just
  assumed) so a stale flag can't silently no-op a "successful" run.
- Run this against 2025-26 ONLY first. Verify in Supabase's Table Editor
  before adapting it into a full historical backfill loop.

ASSUMPTIONS TO CONFIRM WITH NICK BEFORE TRUSTING THIS DATA
-----------------------------------------------------------
1. `odds.sportsbook` is set to 'unspecified_closing' — the Data sheet's
   `ML` column doesn't record which book it came from. Confirm this is
   an acceptable placeholder for now (near-term: one reliable figure).
2. `team_game_stats` sources corsi_for_pct/pp/pk/back_to_back from the
   GDD sheet's raw (non-differential) columns. `faceoff_value` maps to
   GDD's `FOVt`, a rate/value stat, not a literal wins/losses count.
   `sv_pct_above_expected` (renamed from `sv_pct`, GDD's `SvPctR`) is
   goalie save % relative to a shot-quality-adjusted expectation
   (`ExpSv` in the goalies sheets), NOT raw save percentage — confirmed
   with Nick. Raw SV% is deliberately NOT captured at the team-game
   level; it belongs on a future goalie_appearances table (Phase 2/3),
   keyed per goalie per game, not per team per game.
"""

import os
import sys
import pandas as pd
import openpyxl
import psycopg2
from psycopg2.extras import execute_values

# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------

SEASON = "2526"
SEASON_NUM = 2526  # as stored in the Excel 'Season' column
SOURCE_FILE = os.environ.get("NHL_SOURCE_FILE", "nhl2526.xlsx")
GAMELOG_FILE = os.environ.get("NHL_GAMELOG_FILE", "gamelog2526.xlsm")
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"

# Franchise continuity across relocations/renames — extend as historical
# seasons get backfilled later.
FRANCHISE_MAP = {
    "ari": "ari_uta",
    "uta": "ari_uta",
}


def normalize_player_name(raw: str) -> tuple[str, str]:
    """
    Returns (normalized_key, corrected_display_name).
    Fixes the mojibake pattern found in scraped names (e.g. 'TerÃ¤vÃ¤inen'
    -> 'Teräväinen', 'PastrÅˆÃ¡k' -> 'Pastrňák') via a latin1->utf8
    round-trip, then strips + lowercases for the matching key. Both steps
    are required per playbook §6 — strip() alone is not enough.
    """
    if raw is None:
        return "", ""
    try:
        display = raw.encode("latin1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        display = raw  # already clean, or a different corruption we haven't seen yet
    normalized = display.strip().lower()
    return normalized, display.strip()


def franchise_id(team_code: str) -> str:
    return FRANCHISE_MAP.get(team_code, team_code)


def get_db_conn():
    """
    Prefers discrete PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT env vars
    over a single DATABASE_URL, since a password containing special
    characters (%, @, $, :, /) can break URI parsing — and PowerShell's
    double-quoted strings expand '$' for variable substitution, which can
    silently corrupt a password embedded in a DSN string.

    Set these instead (PowerShell):
        $env:PGHOST     = "db.xxxx.supabase.co"
        $env:PGPORT     = "5432"
        $env:PGDATABASE = "postgres"
        $env:PGUSER     = "postgres"
        $env:PGPASSWORD = "your-actual-password"   # no encoding needed
    psycopg2.connect() with no arguments reads these automatically.
    Falls back to DATABASE_URL only if none of the discrete vars are set.
    """
    if os.environ.get("PGHOST"):
        return psycopg2.connect()  # reads PGHOST/PGUSER/PGPASSWORD/etc. automatically
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError(
            "No connection info found. Set PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT "
            "(recommended) or DATABASE_URL."
        )
    return psycopg2.connect(db_url)


# ---------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------

def load_sheet_df(path: str, sheet: str) -> pd.DataFrame:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet]
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    rows = [dict(zip(header, row)) for row in ws.iter_rows(min_row=2, values_only=True)]
    return pd.DataFrame(rows)


def load_prept(path: str) -> pd.DataFrame:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["PrePT"]
    header = [c.value for c in next(ws.iter_rows(min_row=2, max_row=2))]  # row 2 has season labels
    rows = [dict(zip(header, row)) for row in ws.iter_rows(min_row=3, values_only=True)]
    df = pd.DataFrame(rows)
    return df[["Team", SEASON_NUM]].rename(columns={"Team": "team_code", SEASON_NUM: "vegas_point_total"})


def load_prior_season_stats(gamelog_path: str) -> pd.DataFrame:
    """skaters2425 sheet — last season's final per-player PV/FOV, used as
    the blending baseline for this season's PVAdj formula."""
    df = load_sheet_df(gamelog_path, "skaters2425")
    df = df[["Player", "Pos", "GP", "TP", "TOI", "FW", "FL", "PPG", "ATOI", "PV", "FOV"]].copy()
    df = df.dropna(subset=["Player"])
    keys = df["Player"].apply(normalize_player_name)
    df["player_name_normalized"] = keys.apply(lambda t: t[0])
    df["player_name_display"] = keys.apply(lambda t: t[1])
    df["season"] = "2425"  # this sheet is always "last season" relative to SEASON
    df = df.rename(columns={
        "Pos": "pos", "GP": "gp", "TP": "tp", "TOI": "toi", "FW": "fw", "FL": "fl",
        "PPG": "ppg", "ATOI": "atoi", "PV": "pv", "FOV": "fov",
    }).drop(columns=["Player"])
    return df


def load_rookie_projections(gamelog_path: str) -> pd.DataFrame:
    """rookies2526 sheet, primary block only (Player/Pos/Round) — PV/FOV are
    NOT stored here; they're derived via v_rookie_projections from the
    round/position lookup tables, so editing draft_round later
    automatically recalculates them instead of silently going stale."""
    wb = openpyxl.load_workbook(gamelog_path, data_only=True)
    ws = wb["rookies2526"]
    rows = []
    for row in ws.iter_rows(min_row=2, max_col=3, values_only=True):
        rows.append(row)
    df = pd.DataFrame(rows, columns=["Player", "Pos", "Round"])
    df = df.dropna(subset=["Player"])
    keys = df["Player"].apply(normalize_player_name)
    df["player_name_normalized"] = keys.apply(lambda t: t[0])
    df["player_name_display"] = keys.apply(lambda t: t[1])
    df["season"] = SEASON
    df = df.rename(columns={"Pos": "pos", "Round": "draft_round"}).drop(columns=["Player"])
    df["draft_round"] = df["draft_round"].astype(str)

    dupe_mask = df.duplicated(subset=["player_name_normalized", "season"], keep=False)
    if dupe_mask.any():
        conflicting = df[dupe_mask].groupby("player_name_normalized").filter(
            lambda g: g[["pos", "draft_round"]].nunique().gt(1).any()
        )
        if not conflicting.empty:
            print("WARNING: conflicting duplicate rookie rows in source sheet "
                  "(kept the 'U' / aged-out row where one exists, dropped the rest):")
            print(conflicting.sort_values("player_name_normalized").to_string(index=False))
        # Prefer the 'U' (aged-out/undrafted) row over a stale numbered-round
        # row when both exist for the same player — matches the real-world
        # process of resetting a player to undrafted status at age 25.
        df["_sort_key"] = (df["draft_round"] != "U").astype(int)
        df = df.sort_values(["player_name_normalized", "_sort_key"])
        df = df.drop_duplicates(subset=["player_name_normalized", "season"], keep="first")
        df = df.drop(columns=["_sort_key"]).reset_index(drop=True)

    return df


def load_rookie_pv_lookup(gamelog_path: str) -> pd.DataFrame:
    """H1:O9 (defensemen) and Q1:X9 (forwards) round->PV base tables."""
    wb = openpyxl.load_workbook(gamelog_path, data_only=True)
    ws = wb["rookies2526"]
    rows = []
    for group, col_start in [("D", 8), ("F", 17)]:
        for r in range(2, 10):
            round_ = ws.cell(row=r, column=col_start).value      # Round column
            pv_base = ws.cell(row=r, column=col_start + 7).value  # PV column, 8th in block
            if round_ is not None and pv_base is not None:
                rows.append({"draft_round": str(round_), "position_group": group, "pv_base": pv_base})
    return pd.DataFrame(rows).drop_duplicates(subset=["draft_round", "position_group"])


def load_rookie_fov_lookup(gamelog_path: str) -> pd.DataFrame:
    """H11:L15 position->FOpct table."""
    wb = openpyxl.load_workbook(gamelog_path, data_only=True)
    ws = wb["rookies2526"]
    rows = []
    for r in range(12, 16):
        pos = ws.cell(row=r, column=8).value    # Pos column
        fov_pct = ws.cell(row=r, column=12).value  # FOpct column, 5th in block
        if pos is not None and fov_pct is not None:
            rows.append({"pos": pos, "fov_pct": fov_pct})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------
# Transformation
# ---------------------------------------------------------------------

def build_tables(data_df: pd.DataFrame, gdd_df: pd.DataFrame, prept_df: pd.DataFrame):
    data_df = data_df[data_df["Season"] == SEASON_NUM].copy()
    gdd_df = gdd_df[gdd_df["Season"] == SEASON_NUM].copy()

    # --- teams ---
    team_codes = sorted(set(data_df["Team"]) | set(data_df["Opp"]))
    teams = pd.DataFrame({"team_code": team_codes, "team_name": team_codes})  # placeholder full names

    # --- team_seasons ---
    team_seasons = pd.DataFrame({
        "team_code": team_codes,
        "season": SEASON,
        "franchise_id": [franchise_id(t) for t in team_codes],
    })

    # --- games (dedupe the two team-perspective rows per LGame) ---
    home_rows = data_df[data_df["Home"] == 1]
    games = pd.DataFrame({
        "game_id": SEASON + "_" + home_rows["LGame"].astype(str),
        "season": SEASON,
        "date": home_rows["Date"],
        "home_team": home_rows["Team"],
        "away_team": home_rows["Opp"],
        "playoff": home_rows["Playoff"].astype(bool),
        "home_goals": home_rows["GF"],
        "away_goals": home_rows["GA"],
    })

    # --- team_game_stats (join Data + GDD on TeamGame) ---
    merged = data_df.merge(
        gdd_df[["TeamGame", "SvPctR", "CFPct", "PP", "PK", "Back", "FOVt", "PVAdjSum"]],
        on="TeamGame", how="left",
    )
    team_game_stats = pd.DataFrame({
        "game_id": SEASON + "_" + merged["LGame"].astype(str),
        "team_code": merged["Team"],
        "home": merged["Home"].astype(bool),
        "attendance": merged["Att"],
        "goalie": merged["Goalie"],
        "corsi_for_pct": merged["CFPct"],
        "pp": merged["PP"],
        "pk": merged["PK"],
        "back_to_back": merged["Back"].astype(bool),
        "faceoff_value": merged["FOVt"],
        "sv_pct_above_expected": merged["SvPctR"],
        "pvadj_sum_snapshot": merged["PVAdjSum"],
    })

    # --- odds ---
    odds = pd.DataFrame({
        "game_id": SEASON + "_" + data_df["LGame"].astype(str),
        "team_code": data_df["Team"],
        "moneyline": data_df["ML"],
        "sportsbook": "unspecified_closing",
    })
    odds = odds[pd.to_numeric(odds["moneyline"], errors="coerce").notna()]

    # --- preseason_points ---
    preseason_points = prept_df.copy()
    preseason_points["season"] = SEASON

    return teams, team_seasons, games, team_game_stats, odds, preseason_points


def build_player_reference_tables(gamelog_path: str):
    prior_season_stats = load_prior_season_stats(gamelog_path)
    rookie_projections = load_rookie_projections(gamelog_path)
    rookie_pv_lookup = load_rookie_pv_lookup(gamelog_path)
    rookie_fov_lookup = load_rookie_fov_lookup(gamelog_path)
    return prior_season_stats, rookie_projections, rookie_pv_lookup, rookie_fov_lookup


# ---------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------

def upsert(conn, table: str, df: pd.DataFrame, conflict_cols: list[str]):
    if df.empty:
        print(f"  [skip] {table}: no rows")
        return
    cols = list(df.columns)
    values = [tuple(row) for row in df.itertuples(index=False, name=None)]
    update_cols = [c for c in cols if c not in conflict_cols]
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols) if update_cols else None

    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s "
    sql += f"ON CONFLICT ({', '.join(conflict_cols)}) "
    sql += f"DO UPDATE SET {set_clause}" if set_clause else "DO NOTHING"

    if DRY_RUN:
        print(f"  [dry-run] {table}: would upsert {len(values)} rows")
        return

    with conn.cursor() as cur:
        execute_values(cur, sql, values)
    conn.commit()
    print(f"  [live] {table}: upserted {len(values)} rows")


def main():
    print(f"NHL core migration — season {SEASON} — DRY_RUN={DRY_RUN}")
    if DRY_RUN:
        print("Running in dry-run mode. Set DRY_RUN=false to actually write to Supabase.\n")

    if not os.path.exists(SOURCE_FILE):
        print(f"ERROR: source file not found: {SOURCE_FILE}", file=sys.stderr)
        sys.exit(1)

    print(f"Reading {SOURCE_FILE} ...")
    data_df = load_sheet_df(SOURCE_FILE, "Data")
    gdd_df = load_sheet_df(SOURCE_FILE, "GDD")
    prept_df = load_prept(SOURCE_FILE)

    teams, team_seasons, games, team_game_stats, odds, preseason_points = build_tables(
        data_df, gdd_df, prept_df
    )

    if not os.path.exists(GAMELOG_FILE):
        print(f"ERROR: gamelog file not found: {GAMELOG_FILE}", file=sys.stderr)
        sys.exit(1)
    prior_season_stats, rookie_projections, rookie_pv_lookup, rookie_fov_lookup = build_player_reference_tables(GAMELOG_FILE)

    print(f"teams: {len(teams)}, team_seasons: {len(team_seasons)}, games: {len(games)}, "
          f"team_game_stats: {len(team_game_stats)}, odds: {len(odds)}, "
          f"preseason_points: {len(preseason_points)}, "
          f"prior_season_stats: {len(prior_season_stats)}, "
          f"rookie_projections: {len(rookie_projections)}, "
          f"rookie_pv_lookup: {len(rookie_pv_lookup)}, "
          f"rookie_fov_lookup: {len(rookie_fov_lookup)}")

    conn = get_db_conn() if not DRY_RUN else None

    try:
        print("\nUpserting...")
        upsert(conn, "teams", teams, ["team_code"])
        upsert(conn, "team_seasons", team_seasons, ["team_code", "season"])
        upsert(conn, "games", games, ["game_id"])
        upsert(conn, "team_game_stats", team_game_stats, ["game_id", "team_code"])
        upsert(conn, "odds", odds, ["game_id", "team_code"])
        upsert(conn, "preseason_points", preseason_points, ["team_code", "season"])
        upsert(conn, "prior_season_stats", prior_season_stats, ["player_name_normalized", "season"])
        upsert(conn, "rookie_projections", rookie_projections, ["player_name_normalized", "season"])
        upsert(conn, "rookie_pv_lookup", rookie_pv_lookup, ["draft_round", "position_group"])
        upsert(conn, "rookie_fov_lookup", rookie_fov_lookup, ["pos"])
    finally:
        if conn:
            conn.close()

    print("\nDone. Verify row counts in Supabase's Table Editor before backfilling other seasons.")


if __name__ == "__main__":
    main()
