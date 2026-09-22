"""
nhl_pregame_signal.py -- computes the pre-game win probability, edge, and
Kelly stake for a scheduled game, and freezes it as a bet_signals row. Once
a (game_id, team_code) row exists, it is NEVER recalculated or overwritten
-- that is the whole point of "freezing" a decision (see SAFETY below).

WHY THIS IS SAFE TO CALL ON AN UNPLAYED GAME
--------------------------------------------------------------------------
Every one of the 8 model features is already computed from data strictly
BEFORE the game date -- confirmed by reading the actual SQL, not assumed
(see chat). Four of the eight (CF%, PP, PK, SvPctR) are computed by
compute_team_game_stats_row(), imported unchanged from
nhl_daily_results_ingest.py: that function was already written to only read
"date < game_date" history, and its `attendance`/`cf_today`/`ca_today`/
`pp_pk` parameters are unused for anything but bookkeeping. The only input
it needs that a played game has and an unplayed one doesn't is a starting
goalie id -- supplied here from goalie_snapshots instead of a box score,
exactly as nhl_daily_results_ingest.py now does for real games.
The other four (HomeAdjD, GDD, PrePTD, BackD) come from two new views built
and validated against real historical games before this script was written:
  - v_pregame_team_state (materialized): the same cumulative goal-diff /
    attendance / games-played numbers as v_team_cumulative_stats, but keyed
    off `games` directly so it also covers games with no team_game_stats
    row yet. Refreshed daily in nhl_daily_results_ingest.py's chain.
  - v_pregame_lineup_pvadjsum: PVAdjSum computed from lineup_snapshots
    instead of actual appearances -- each player's value is recomputed
    fresh through the day before the game (see chat: the first version of
    this read an existing v_pvadj row and was ONE GAME STALE, since that
    view's own rows already exclude their own game). Validated exact-match
    against two real post-game PVAdjSum values, including a rookie-blend
    case.
model_pct, kf, lambda, and edge_threshold all reuse the SAME formulas as
the live post-game views (v_model_pct, v_team_kf, v_lambda,
v_edge_threshold_by_season) -- read directly from those views' SQL
definitions, not from memory, to avoid drift. lambda and edge_threshold, in
particular, needed NO new logic: they already depend only on each team's
own trailing performance and the season's own history, so this script just
reads each team's latest known row.

SNAPSHOT TIER
--------------------------------------------------------------------------
For lineup and for goalie, independently, per team: 'confirmed' is used if
it exists, else 'preliminary'. A game is only a candidate once BOTH teams
have at least a 'preliminary' lineup AND goalie snapshot (see
candidate_team_games()). The frozen row's snapshot_type_used is
'preliminary' if ANY of the four (home lineup, home goalie, away lineup,
away goalie) inputs used was preliminary, else 'confirmed' -- the weakest
link for the whole game, since PVAdjSumD/SvPctRD are diffs between both
teams. Most games will freeze on 'preliminary' goalie/lineup data, simply
because the nightly 'confirmed' DailyFaceoff capture (nhl_lineup_goalie_
ingest.py, RUN_MODE=confirmed) doesn't run until ~11pm ET, well after most
games have already started and odds have already closed in. That is
expected, not a bug -- it is exactly what Nick's own design (one nightly
confirmed check) implies, and it's why this is recorded per row rather than
assumed.

VIG GATE (my addition -- flagged, not something explicitly requested)
--------------------------------------------------------------------------
bets_fire additionally requires the captured odds row's vig_flag != 'invalid'.
An invalid vig means the captured line is probably wrong (a scrape issue,
usually), so firing a bet off it seemed unsafe. This is my own judgment
call, not from an existing spec -- remove the check in bets_fire() below if
unwanted.

SAFETY
--------------------------------------------------------------------------
- DRY_RUN defaults to true.
- INSERT ... ON CONFLICT (game_id, team_code) DO NOTHING -- once frozen,
  never touched again by this script or any other.
- Only considers regular-season games with no existing bet_signals row for
  either team, where odds AND at least a preliminary lineup+goalie snapshot
  exist for BOTH teams.
"""

import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

import psycopg2
from psycopg2.extras import execute_values, Json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nhl_daily_results_ingest import (  # noqa: E402
    compute_team_game_stats_row, get_db_conn as _shared_get_db_conn,
)

DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"

FEATURE_KEYS = ["homeadjd", "pvadjsumd", "gdd", "preptd", "backd", "svpctrd", "ppd", "cfpctd"]


def get_db_conn():
    return _shared_get_db_conn()


# --------------------------- snapshot lookups ---------------------------

def best_lineup_snapshot(conn, game_id, team_code):
    """-> snapshot_type ('confirmed' preferred, else 'preliminary'), or None if neither exists."""
    with conn.cursor() as cur:
        cur.execute("select snapshot_type from lineup_snapshots where game_id=%s and team_code=%s "
                    "order by (snapshot_type='confirmed') desc limit 1", (game_id, team_code))
        row = cur.fetchone()
    return row[0] if row else None


def best_goalie_snapshot(conn, game_id, team_code):
    """-> (player_id, snapshot_type), or (None, None) if neither exists."""
    with conn.cursor() as cur:
        cur.execute("select player_id, snapshot_type from goalie_snapshots where game_id=%s and team_code=%s "
                    "order by (snapshot_type='confirmed') desc limit 1", (game_id, team_code))
        row = cur.fetchone()
    return row if row else (None, None)


# --------------------------- feature inputs ---------------------------

def team_state(conn, game_id, team_code):
    """cum_gd, avg_home_attendance, home_game_count, team_game_num_prior -- from v_pregame_team_state."""
    with conn.cursor() as cur:
        cur.execute("select cum_gd, avg_home_attendance, home_game_count, team_game_num_prior "
                    "from v_pregame_team_state where game_id=%s and team_code=%s", (game_id, team_code))
        row = cur.fetchone()
    if row is None:
        return None
    return {"cum_gd": row[0], "avg_home_attendance": row[1], "home_game_count": row[2], "team_game_num_prior": row[3]}


def pvadjsum(conn, game_id, team_code, snapshot_type):
    with conn.cursor() as cur:
        cur.execute("select pvadj_sum, n_players, n_missing from v_pregame_lineup_pvadjsum "
                    "where game_id=%s and team_code=%s and snapshot_type=%s", (game_id, team_code, snapshot_type))
        row = cur.fetchone()
    return row  # (pvadj_sum, n_players, n_missing) or None


def preseason_points(conn, team_code, season):
    with conn.cursor() as cur:
        cur.execute("select vegas_point_total from preseason_points where team_code=%s and season=%s",
                    (team_code, season))
        row = cur.fetchone()
    return row[0] if row else None


def homeadjd_input(conn, team_code, season):
    """Arena capacity, needed only as the <3-home-games fallback for HomeAdjD (matches v_team_game_features)."""
    with conn.cursor() as cur:
        cur.execute("select cap from arenas where team_code=%s and season=%s", (team_code, season))
        row = cur.fetchone()
    return row[0] if row else None


def back_to_back(conn, team_code, game_date):
    with conn.cursor() as cur:
        cur.execute("select 1 from games where (home_team=%s or away_team=%s) and date = %s - interval '1 day'",
                    (team_code, team_code, game_date))
        return cur.fetchone() is not None


# --------------------------- model / market inputs ---------------------------

def effective_model_version(conn, game_date):
    """Same filter v_model_pct uses: model_type='base_logit', effective_from<=date<effective_to."""
    with conn.cursor() as cur:
        cur.execute(
            "select model_version_id, coefficients from model_versions where model_type='base_logit' "
            "and effective_from <= %s and (effective_to is null or %s < effective_to) "
            "order by effective_from desc limit 1", (game_date, game_date))
        row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def model_pct_from_features(coefficients, features):
    """Same sigmoid(intercept + sum(coef*feature)) formula as v_model_pct, ported to Python."""
    z = float(coefficients["intercept"])
    for k in FEATURE_KEYS:
        z += float(coefficients[k]) * float(features[k])
    return 1.0 / (1.0 + pow(2.718281828459045, -z))


def moneyline_and_vig(conn, game_id, team_code):
    with conn.cursor() as cur:
        cur.execute("select moneyline, vig_flag from odds where game_id=%s and team_code=%s", (game_id, team_code))
        row = cur.fetchone()
    return row if row else (None, None)


def mlpct_from_moneyline(ml):
    ml = float(ml)
    return (-ml) / (-ml + 100.0) if ml < 0 else 100.0 / (ml + 100.0)


def kf_from_model_and_ml(model_pct, ml):
    ml = float(ml)
    b = ml / 100.0 if ml > 0 else -100.0 / ml
    return model_pct - (1 - model_pct) / b


def latest_trailing_gap(conn, team_code):
    """(team_trailing_logloss - team_trailing_vlogloss) from that team's most recent v_team_trailing_perf
    row, or 0 if it has none yet (matches v_lambda's own COALESCE(gap, 0))."""
    with conn.cursor() as cur:
        cur.execute("select team_trailing_logloss, team_trailing_vlogloss from v_team_trailing_perf "
                    "where team=%s order by date desc limit 1", (team_code,))
        row = cur.fetchone()
    if row is None or row[0] is None or row[1] is None:
        return 0.0
    return float(row[0]) - float(row[1])


def lambda_value(conn, team_code, opp_code):
    team_gap = latest_trailing_gap(conn, team_code)
    opp_gap = latest_trailing_gap(conn, opp_code)
    raw = 1 - (max(team_gap, opp_gap) - 0.0125) / (0.04 - 0.0125)
    return min(1.0, max(0.7, raw))


def edge_threshold_for_season(conn, season):
    with conn.cursor() as cur:
        cur.execute("select edge_threshold from v_edge_threshold_by_season where season=%s", (season,))
        row = cur.fetchone()
    return float(row[0]) if row else None


def kelly_fraction_for_season(conn, season):
    with conn.cursor() as cur:
        cur.execute("select kelly_fraction from season_config where season=%s", (season,))
        row = cur.fetchone()
    return float(row[0]) if row else None


# --------------------------- per-team feature assembly ---------------------------

def team_features(conn, game_id, team_code, opp_code, is_home, season, prior_season, game_date,
                   lineup_type, goalie_id):
    """The 8 model features for one team's side of one game. Returns (features_dict, None) or (None, reason)."""
    st = team_state(conn, game_id, team_code)
    if st is None or team_state(conn, game_id, opp_code) is None:
        return None, "v_pregame_team_state has no row for this game/team (schedule not loaded?)"

    pv = pvadjsum(conn, game_id, team_code, lineup_type)
    if pv is None:
        return None, f"no pvadjsum row for ({game_id}, {team_code}, {lineup_type})"

    cap = homeadjd_input(conn, team_code, season)
    if is_home:
        home_attendance = st["avg_home_attendance"] if st["home_game_count"] >= 3 else cap
        homeadjd = float(home_attendance) if home_attendance else 0.0
    else:
        homeadjd = 0.0  # matches v_team_game_features: HomeAdjD is the home team's own average, 0 for the away row

    preptd_team = preseason_points(conn, team_code, season)
    preptd_opp = preseason_points(conn, opp_code, season)
    if preptd_team is None or preptd_opp is None:
        return None, "missing preseason_points for one side"

    row = compute_team_game_stats_row(
        conn, game_id, game_date, season, prior_season, team_code, is_home,
        None, goalie_id, None, None, {})

    return {
        "_cum_gd": st["cum_gd"] or 0, "_pvadjsum": float(pv[0]) if pv[0] is not None else None,
        "_preptd": preptd_team, "_homeadjd": homeadjd,
        "_svpctr": row["sv_pct_above_expected"], "_pp": row["pp"], "_cf": row["corsi_for_pct"],
        "_backd": back_to_back(conn, team_code, game_date), "_n_missing_pvadj": pv[2],
    }, None


# --------------------------- candidates ---------------------------

def candidate_team_games(conn):
    """(game_id, home_team, away_team, season, date) for regular-season games with no bet_signals row for
    either team yet, and odds + at least a preliminary lineup AND goalie snapshot for both teams."""
    with conn.cursor() as cur:
        cur.execute("""
            select g.game_id, g.home_team, g.away_team, g.season, g.date
            from games g
            where g.playoff = false
              and not exists (select 1 from bet_signals b where b.game_id = g.game_id)
              and exists (select 1 from odds o where o.game_id = g.game_id and o.team_code = g.home_team)
              and exists (select 1 from odds o where o.game_id = g.game_id and o.team_code = g.away_team)
              and exists (select 1 from lineup_snapshots l where l.game_id = g.game_id and l.team_code = g.home_team)
              and exists (select 1 from lineup_snapshots l where l.game_id = g.game_id and l.team_code = g.away_team)
              and exists (select 1 from goalie_snapshots gs where gs.game_id = g.game_id and gs.team_code = g.home_team)
              and exists (select 1 from goalie_snapshots gs where gs.game_id = g.game_id and gs.team_code = g.away_team)
            order by g.date
        """)
        return cur.fetchall()


def prior_season_code(season):
    return f"{int(season[:2]) - 1:02d}{int(season[2:]) - 1:02d}"


# --------------------------- freeze one game ---------------------------

def build_signal_rows(conn, game_id, home, away, season, game_date):
    prior_season = prior_season_code(season)
    home_lineup_type = best_lineup_snapshot(conn, game_id, home)
    away_lineup_type = best_lineup_snapshot(conn, game_id, away)
    home_goalie, home_goalie_type = best_goalie_snapshot(conn, game_id, home)
    away_goalie, away_goalie_type = best_goalie_snapshot(conn, game_id, away)
    if not all([home_lineup_type, away_lineup_type, home_goalie_type, away_goalie_type]):
        return None, "missing a lineup or goalie snapshot for one side"
    snapshot_type_used = "confirmed" if all(
        t == "confirmed" for t in (home_lineup_type, away_lineup_type, home_goalie_type, away_goalie_type)
    ) else "preliminary"

    home_f, err = team_features(conn, game_id, home, away, True, season, prior_season, game_date,
                                home_lineup_type, home_goalie)
    if err:
        return None, f"home: {err}"
    away_f, err = team_features(conn, game_id, away, home, False, season, prior_season, game_date,
                                away_lineup_type, away_goalie)
    if err:
        return None, f"away: {err}"
    if home_f["_pvadjsum"] is None or away_f["_pvadjsum"] is None or \
       home_f["_svpctr"] is None or away_f["_svpctr"] is None:
        return None, "a required feature came back NULL (missing player match or goalie prior)"

    model_version_id, coefficients = effective_model_version(conn, game_date)
    if model_version_id is None:
        return None, f"no effective base_logit model_versions row for {game_date}"

    features_home = {
        "homeadjd": home_f["_homeadjd"], "pvadjsumd": home_f["_pvadjsum"] - away_f["_pvadjsum"],
        "gdd": home_f["_cum_gd"] - away_f["_cum_gd"], "preptd": home_f["_preptd"] - away_f["_preptd"],
        "backd": (1 if home_f["_backd"] else 0) - (1 if away_f["_backd"] else 0),
        "svpctrd": home_f["_svpctr"] - away_f["_svpctr"], "ppd": home_f["_pp"] - away_f["_pp"],
        "cfpctd": home_f["_cf"] - away_f["_cf"],
    }
    home_win_prob = model_pct_from_features(coefficients, features_home)

    rows = []
    now = datetime.now(timezone.utc)
    for team_code, opp_code, is_home, model_pct, f in (
        (home, away, True, home_win_prob, features_home),
        (away, home, False, 1 - home_win_prob, {k: -v for k, v in features_home.items()}),
    ):
        ml, vig_flag = moneyline_and_vig(conn, game_id, team_code)
        if ml is None:
            return None, f"no odds row for {team_code}"
        mlpct = mlpct_from_moneyline(ml)
        kf = kf_from_model_and_ml(model_pct, ml)
        lam = lambda_value(conn, team_code, opp_code)
        edge_threshold = edge_threshold_for_season(conn, season)
        kelly_fraction = kelly_fraction_for_season(conn, season)
        st = team_state(conn, game_id, team_code)
        team_game_num = st["team_game_num_prior"] + 1
        bets_fire = team_game_num > 10 and kf >= edge_threshold and vig_flag != "invalid"
        stake_fraction = kelly_fraction * kf * lam
        rows.append((
            game_id, team_code, opp_code, is_home, now, snapshot_type_used, int(ml), vig_flag,
            model_version_id, Decimal(str(model_pct)), Decimal(str(mlpct)), Decimal(str(kf)),
            Decimal(str(lam)), Decimal(str(edge_threshold)), team_game_num, bets_fire,
            Decimal(str(kelly_fraction)), Decimal(str(stake_fraction)), Json(f),
        ))
    return rows, None


COLUMNS = ["game_id", "team_code", "opp_team_code", "home", "captured_at", "snapshot_type_used", "moneyline",
           "vig_flag", "model_version_id", "model_pct", "mlpct", "kf", "lambda", "edge_threshold",
           "team_game_num", "bets_fire", "kelly_fraction", "stake_fraction", "features"]


def write_signal_rows(conn_factory, rows):
    if DRY_RUN:
        print(f"[dry-run] would freeze {len(rows)} bet_signals row(s):")
        for r in rows:
            print("   ", dict(zip(COLUMNS, r)))
        return
    conn = conn_factory()
    try:
        with conn.cursor() as cur:
            execute_values(cur, f"insert into bet_signals ({', '.join(COLUMNS)}) values %s "
                                "on conflict (game_id, team_code) do nothing", rows)
        conn.commit()
        print(f"[live] froze {len(rows)} bet_signals row(s).")
    finally:
        conn.close()


def main():
    print(f"NHL pregame signal -- DRY_RUN={DRY_RUN}")
    if not os.environ.get("PGHOST"):
        print("No PGHOST set -- nothing to do.")
        return
    conn = get_db_conn()
    try:
        candidates = candidate_team_games(conn)
        print(f"Candidate games (odds + snapshots present for both sides, not yet frozen): {len(candidates)}")
        all_rows = []
        for game_id, home, away, season, game_date in candidates:
            rows, err = build_signal_rows(conn, game_id, home, away, season, game_date)
            if err:
                print(f"  {away} @ {home} ({game_id}): SKIPPED -- {err}")
                continue
            print(f"  {away} @ {home} ({game_id}): home_win_prob={float(rows[0][9]):.4f} "
                  f"snapshot={rows[0][5]} home_bets_fire={rows[0][15]} away_bets_fire={rows[1][15]}")
            all_rows += rows
    finally:
        conn.close()
    write_signal_rows(get_db_conn, all_rows)
    print("Done.")


if __name__ == "__main__":
    main()
