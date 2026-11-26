"""
nhl_generate_dashboard.py -- NHL model dashboard, static HTML for GitHub Pages.

Modeled directly on the EPL dashboard's structure and conventions (same
page sections, same color scheme, same "always recompute live rather than
trust a stale snapshot" philosophy where possible) -- see chat for the
reference script. Two differences from EPL, both deliberate:

1. Connection convention: plain psycopg2 with discrete PGHOST/PGUSER/
   PGPASSWORD/PGDATABASE/PGPORT env vars, matching every other script in
   this repo (not EPL's DATABASE_URL + sqlalchemy).
2. Today's games read a FROZEN decision (bet_signals) instead of
   recomputing live like EPL does -- NHL's pipeline already has a proper
   frozen pre-game decision table (nhl_pregame_signal.py), so the
   dashboard just displays it rather than re-deriving it.

SECTIONS
--------------------------------------------------------------------------
- Today's games: every game scheduled for today, one row per team, joined
  to its frozen bet_signals row if one exists yet. Columns are exactly
  what Nick asked for: Bet Y/N, $ Amount, Model %, Market %, Delta -- this
  IS the rationale (no separate notes field; a static page has nowhere to
  save one).
- Weekly breakdown (one season -- see DETAIL SEASON below): calendar
  weeks since that season's own start date (season_config.season_start_date),
  not a league-defined gameweek (NHL has none). Same metric set as the
  season table, just sliced by week, per Nick's "aggregate performance per
  week (similar to what the season aggregate results show)".
- Team breakdown (same one season): same metric set again, sliced by
  team instead of week -- betting FOR that team specifically.
- Season-by-season summary: every season with settled data, same shape as
  the EPL version's Past Seasons table (bets, record, wagered, profit,
  return, model LogLoss, market LogLoss, delta), plus a Total row.

Deliberately simplified vs EPL in one place: EPL's weekly section also
tracked "model correct / market correct" as a separate match-level
picking-accuracy stat. That's dropped here -- Nick's ask was the same
season-summary metric set applied to week and team slices, not a second,
different stat track. Add it back if wanted.

Not yet ported from the NHL Excel summary tab: the per-Kelly-cushion-tier
breakdown (3 tiers + Total per season) and the Regular Season / Playoffs
split. Flagged as a possible v2, not built now -- see chat.

DETAIL SEASON
--------------------------------------------------------------------------
The single season shown in the Weekly/Team breakdowns is picked
automatically: whichever season has the most recent settled data in
v_kelly_signal. Right now that's 2025-26 (2026-27 has no completed games
yet), and it will switch to 2026-27 on its own the day that season's
first game settles -- no manual toggle to remember. Override with the
DETAIL_SEASON env var if a specific season is ever wanted instead.

DATA SOURCES
--------------------------------------------------------------------------
- Today's games: `games` (today, US/Eastern) LEFT JOIN `bet_signals`.
- Historical performance (season/week/team): v_team_game_perspective
  (already has model_pct, mlpct, logloss, vlogloss -- both loglosses are
  real view columns already, not recomputed here) JOIN v_kelly_signal
  (kf, bets_fire) JOIN v_lambda (lambda) JOIN season_config
  (kelly_fraction) LEFT JOIN v_kelly_bank_theoretical (the dollar
  bankroll in effect on that date, for $-denominated wagered/profit --
  this is the SAME continuously-compounding all-history series used
  throughout the project, not a per-season reset, so it's one consistent
  methodology across the whole table).
- "Current bankroll" in the header, the wallet chart, and today's $ stakes:
  the IN-YEAR bank -- season_config.starting_bankroll for the current season
  ($5,000 for 2026-27) compounded forward with only that season's own
  v_kelly_daily_return rows (see get_season_bank). This is computed here, not
  read from v_kelly_bank_live / v_kelly_bank_theoretical, which stay exactly as
  they are as the historical record of what the continuously-compounding
  theoretical bank would have done (those views are anchored at 2018-19 and
  2025-26 respectively, which is why they disagreed with each other).
  Dollar figures for the current season use the in-year bank; earlier seasons
  keep using v_kelly_bank_theoretical.
- bet_signals.stake_fraction is a FRACTION of bankroll; today's "$ Amount" is
  stake_fraction x the in-year bank, rounded to whole dollars.

SAFETY
--------------------------------------------------------------------------
Read-only against the database -- this script never writes to Supabase.
Its own side effect is purely local: it writes docs/index.html, which the
workflow then commits only if the content actually changed.
"""

import html as html_lib
import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OUTPUT_PATH = "docs/index.html"
SEASONS_DIR = "docs/seasons"  # one json per non-default season, loaded by the season picker

# Right-hand (LogLoss delta) axis of the wallet chart: fixed, because early-season variability otherwise
# stretches the scale and hides the trend. Values outside the range are clipped at the panel edge.
LL_DELTA_AXIS_HALF_RANGE = 0.005
LL_DELTA_AXIS_STEP = 0.001


def get_db_conn():
    if os.environ.get("PGHOST"):
        return psycopg2.connect()
    raise RuntimeError("Set PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT.")


def et_today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return (datetime.now(timezone.utc) - timedelta(hours=5)).date()


def to_et(dt):
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return dt.astimezone(timezone(timedelta(hours=-5)))


def fmt_clock(dt):
    """'2:03 PM' (12-hour, no leading zero), Eastern."""
    return to_et(dt).strftime("%I:%M %p").lstrip("0")


def fmt_scraped(dt):
    """'7:33 AM' if scraped today (ET), else 'Oct 11, 2:15 AM'."""
    e = to_et(dt)
    if e.date() == et_today():
        return fmt_clock(dt)
    return f"{e.strftime('%b')} {e.day}, {fmt_clock(dt)}"


def pick_snap(by_type, signal_snap):
    """Which snapshot a cell shows: the signal's own type if present, else confirmed, else preliminary."""
    return signal_snap if signal_snap in by_type else (
        "confirmed" if "confirmed" in by_type else ("preliminary" if "preliminary" in by_type else None))


def scraped_note(dt, show):
    if dt is None or not show:
        return ""
    return f"<br><span class=\"tag\" style=\"font-size:11px;\">as of {fmt_scraped(dt)}</span>"


# --------------------------- header ---------------------------

def get_current_season(conn):
    """The season in play today: latest season_config row whose start date has arrived."""
    with conn.cursor() as cur:
        cur.execute("select season from season_config where season_start_date <= %s "
                    "order by season_start_date desc limit 1", (et_today(),))
        row = cur.fetchone()
    return row[0] if row else None


def get_season_bank(conn, season):
    """In-year bank for one season: starts at season_config.starting_bankroll and compounds with ONLY that
    season's own v_kelly_daily_return rows. Nothing is written anywhere -- the continuous theoretical bank
    views are untouched. Returns a dict:
      start       -- the season's starting bankroll
      current     -- bank after the last settled day (what today's stakes are sized off)
      start_by_date -- {date: bank at the start of that date}
      days        -- [(date, bank_at_end_of_day, result_dollars)] for every date with settled games"""
    with conn.cursor() as cur:
        cur.execute("select starting_bankroll, season_start_date from season_config where season=%s", (season,))
        start, start_date = cur.fetchone()
        cur.execute("select date, daily_return_pct from v_kelly_daily_return where season=%s order by date",
                    (season,))
        rows = cur.fetchall()
    start = float(start)
    bank = start
    start_by_date, days = {}, []
    for d, r in rows:
        start_by_date[d] = bank
        result = bank * float(r)
        bank += result
        days.append((d, bank, result))
    return {"start": start, "start_date": start_date, "current": bank,
            "start_by_date": start_by_date, "days": days}


def get_season_end_date(conn, season, season_start):
    """Last regular-season game date, for the chart's full-season x axis."""
    with conn.cursor() as cur:
        cur.execute("select max(date) from games where season=%s and playoff=false", (season,))
        row = cur.fetchone()
    return row[0] if row and row[0] else season_start + timedelta(days=190)


# --------------------------- today's games ---------------------------

def get_todays_games(conn):
    today = et_today()
    with conn.cursor() as cur:
        cur.execute("""
            select g.game_id, g.home_team, g.away_team,
                   bs_h.bets_fire, bs_h.stake_fraction, bs_h.model_pct, bs_h.mlpct, bs_h.snapshot_type_used,
                   bs_a.bets_fire, bs_a.stake_fraction, bs_a.model_pct, bs_a.mlpct, bs_a.snapshot_type_used
            from games g
            left join bet_signals bs_h on bs_h.game_id=g.game_id and bs_h.team_code=g.home_team
            left join bet_signals bs_a on bs_a.game_id=g.game_id and bs_a.team_code=g.away_team
            where g.date = %s and g.playoff = false
            order by g.game_id
        """, (today,))
        return cur.fetchall()


def get_todays_moneylines(conn, game_ids):
    """{(game_id, team): american moneyline} actually captured with the frozen signal (bet_signals.moneyline).
    Used for the small line shown under Market % on frozen rows. Games with no frozen row simply have no entry."""
    out = {}
    if not game_ids:
        return out
    with conn.cursor() as cur:
        cur.execute("select game_id, team_code, moneyline from bet_signals "
                    "where game_id = any(%s) and moneyline is not null", (list(game_ids),))
        for gid, team, ml in cur.fetchall():
            out[(gid, team)] = int(ml)
    return out


def get_todays_schedule_and_preview(conn, rows):
    """Game start times plus a PREVIEW of the signal for games with no frozen bet_signals row yet.

    Start times come from ESPN's scoreboard (the games table only has a date). The preview uses the current
    DraftKings line (read live from ESPN, or from the `odds` table if the T-5 capture already wrote it) with
    the same model math the T-5 freeze uses (nhl_pregame_signal.build_signal_rows), fed the best lineup/goalie
    snapshots captured so far. NOTHING is written: the frozen T-5 signal still replaces the preview when it
    arrives. Every step is best-effort -- a failure just means no time / no preview, never a broken page.

    Returns (start_times, preview, odds_asof):
      start_times -- {game_id: tz-aware UTC datetime}
      preview     -- {(game_id, team): {"fire", "stake", "mpct", "mlpct", "model_ok"}}; mpct is None when the
                     lineup/goalie data needed for the model number is not captured yet (market % still shown)
      odds_asof   -- when the live lines were read (datetime) or None"""
    start_times, preview, odds_asof = {}, {}, None
    if not rows:
        return start_times, preview, odds_asof
    try:
        import nhl_odds_ingest as oi
        events = {(e["home"], e["away"]): e for e in oi.fetch_scoreboard(et_today())}
    except Exception as e:  # noqa: BLE001
        print(f"ESPN scoreboard unavailable -- no game times / preview odds this run: {e}")
        return start_times, preview, odds_asof

    for r in rows:
        ev = events.get((r[1], r[2]))
        if ev and ev["start"]:
            start_times[r[0]] = ev["start"]

    try:
        import nhl_pregame_signal as ps
    except Exception as e:  # noqa: BLE001
        print(f"Preview model unavailable -- showing times only: {e}")
        return start_times, preview, odds_asof

    with conn.cursor() as cur:
        cur.execute("select game_id, season from games where game_id = any(%s)", ([r[0] for r in rows],))
        seasons = dict(cur.fetchall())

    for (game_id, home, away, _f1, _s1, h_mpct, _l1, _h_snap, _f2, _s2, a_mpct, _l2, _a_snap) in rows:
        if h_mpct is not None and a_mpct is not None:
            continue  # frozen signal exists -- the real thing is shown, no preview
        try:
            ev = events.get((home, away))
            if ev is None or (ev["final"] or (ev["start"] and ev["start"] <= datetime.now(timezone.utc))):
                continue  # not on ESPN, or already underway/finished -- the preview only covers pre-game
            ml = {}
            db_home, db_away = ps.moneyline_and_vig(conn, game_id, home), ps.moneyline_and_vig(conn, game_id, away)
            if db_home[0] is not None and db_away[0] is not None:
                ml = {home: db_home, away: db_away}
            else:
                home_ml, away_ml = oi.fetch_draftkings_moneylines(ev["event_id"], ev["comp_id"])
                if home_ml is None or away_ml is None:
                    continue
                _vig, flag = oi.vig_from_moneylines(home_ml, away_ml)
                ml = {home: (home_ml, flag), away: (away_ml, flag)}
                odds_asof = datetime.now(timezone.utc)
            season = seasons.get(game_id)
            sig_rows, err = (None, "no season") if not season else \
                ps.build_signal_rows(conn, game_id, home, away, season, et_today(), odds_override=ml)
            if sig_rows is None:
                print(f"  preview {away} @ {home}: market only ({err})")
                for team in (home, away):
                    preview[(game_id, team)] = {"fire": False, "stake": None, "mpct": None,
                                                "mlpct": ps.mlpct_from_moneyline(ml[team][0]), "model_ok": False,
                                                "ml": int(ml[team][0])}
                continue
            for sr in sig_rows:
                preview[(game_id, sr[1])] = {"fire": bool(sr[15]), "stake": float(sr[17]), "mpct": float(sr[9]),
                                             "mlpct": float(sr[10]), "model_ok": True, "ml": int(ml[sr[1]][0])}
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            print(f"  preview {away} @ {home}: skipped ({e})")
    return start_times, preview, odds_asof


# --------------------------- skater check + attention callout ---------------------------

ADD_PLAYER_CMD = "python nhl_add_player.py"


def get_goalie_data(conn, game_ids):
    """Per (game, team): starting goalie name + DailyFaceoff's status label, for both snapshot_types if
    present. Mirrors get_skater_check_data's shape so goalie_cell can pick a snapshot the same way
    skater_check_cell does (prefer the signal's own snapshot_type, else confirmed, else preliminary)."""
    goalies = {}
    if not game_ids:
        return goalies
    with conn.cursor() as cur:
        cur.execute("""
            select gs.game_id, gs.team_code, gs.snapshot_type, p.player_name_display, gs.dfo_status, gs.scraped_at
            from goalie_snapshots gs
            join players p on p.player_id = gs.player_id
            where gs.game_id = any(%s)""", (list(game_ids),))
        for gid, team, stype, name, status, scraped in cur.fetchall():
            goalies.setdefault((gid, team), {})[stype] = {"name": name, "status": status, "scraped": scraped}
    return goalies


def goalie_scraped_at(game_id, team, signal_snap, goalie_data):
    by_type = goalie_data.get((game_id, team), {})
    snap = pick_snap(by_type, signal_snap)
    return by_type[snap]["scraped"] if snap else None


def goalie_cell(game_id, team, signal_snap, goalie_data, show_time=True):
    by_type = goalie_data.get((game_id, team), {})
    snap = pick_snap(by_type, signal_snap)
    if snap is None:
        return "<td class=\"tag\">no goalie yet</td>"
    g = by_type[snap]
    # Exactly what DailyFaceoff publishes (Confirmed / Likely / blank = Unconfirmed). Never our own snapshot type.
    label = (g["status"] or "").strip().title() or "Unconfirmed"
    return f"<td>{g['name']} <span class=\"tag\">({label})</span>{scraped_note(g['scraped'], show_time)}</td>"


def get_skater_check_data(conn, game_ids):
    """Per (game, team, snapshot_type): skater count, how many have a NULL PVAdj (and who), plus the open
    unmatched-player queue by team. Unmatched players are dropped from snapshots, so a short lineup is a
    data problem only if that team has an open queue row (or a NULL PVAdj)."""
    snaps, queue = {}, {}
    if game_ids:
        with conn.cursor() as cur:
            cur.execute("""
                select game_id, team_code, snapshot_type, count(*) as n,
                       count(*) filter (where pvadj is null) as n_null,
                       array_agg(player_name_display order by player_name_display) filter (where pvadj is null)
                from v_pregame_lineup_pvadj_players
                where game_id = any(%s)
                group by 1, 2, 3""", (list(game_ids),))
            for gid, team, stype, n, n_null, names in cur.fetchall():
                snaps.setdefault((gid, team), {})[stype] = {"n": n, "n_null": n_null, "names": names or [],
                                                            "scraped": None}
            cur.execute("""
                select game_id, team_code, snapshot_type, max(scraped_at)
                from lineup_snapshots where game_id = any(%s) group by 1, 2, 3""", (list(game_ids),))
            for gid, team, stype, scraped in cur.fetchall():
                if stype in snaps.get((gid, team), {}):
                    snaps[(gid, team)][stype]["scraped"] = scraped
    with conn.cursor() as cur:
        cur.execute("select team_code, player_name_display, reason from player_id_review_queue where status = 'open'")
        for team, name, reason in cur.fetchall():
            queue.setdefault(team, []).append((name, reason))
    return snaps, queue


def skater_scraped_at(game_id, team, signal_snap, snaps):
    by_type = snaps.get((game_id, team), {})
    snap = pick_snap(by_type, signal_snap)
    return by_type[snap]["scraped"] if snap else None


def skater_check_cell(game_id, team, signal_snap, snaps, queue, show_time=True):
    by_type = snaps.get((game_id, team), {})
    snap = pick_snap(by_type, signal_snap)
    open_q = queue.get(team, [])
    if snap is None:
        if open_q:
            names = ", ".join(n for n, _ in open_q)
            return ("<td style=\"color:#b7791f;\">no lineup captured; unmatched: " + names +
                    f"<br><span class=\"tag\">run {ADD_PLAYER_CMD}</span></td>")
        return "<td class=\"tag\">no lineup yet</td>"
    d = by_type[snap]
    n, issues, fixable = d["n"], [], False
    if open_q:
        fixable = True
        issues.append("unmatched, left out of lineup: " + ", ".join(f"{nm} ({r})" for nm, r in open_q))
    if d["n_null"]:
        fixable = True
        issues.append(f"{d['n_null']} with no PVAdj: " + ", ".join(d["names"]))
    if n != 18 and not issues:
        issues.append(f"DailyFaceoff lists only {n} skaters (no unmatched players, so likely genuine)")
    if signal_snap == "preliminary":
        issues.append("<b>signal frozen without full confirmed data</b>")
    if not issues:
        return (f"<td style=\"color:#1a7f37;\">{n}/18 <span class=\"tag\">({snap})</span>"
                f"{scraped_note(d['scraped'], show_time)}</td>")
    text = (f"{n}/18 <span class=\"tag\">({snap})</span>{scraped_note(d['scraped'], show_time)}<br>"
            + "<br>".join(issues))
    if fixable:
        text += f"<br><span class=\"tag\">run {ADD_PLAYER_CMD}</span>"
    return f"<td style=\"color:#b7791f;\">{text}</td>"


def get_attention_callout(conn):
    """Aggregate flag for the top of the page: results-side players with no stats (their game is no longer
    in today's table), plus lineup-side items in upcoming games that still need fixing."""
    with conn.cursor() as cur:
        cur.execute("select player_name_display from player_review_queue where status = 'open' order by 1")
        results = [r[0] for r in cur.fetchall()]
        cur.execute("select count(*) from player_id_review_queue where status = 'open'")
        unmatched = cur.fetchone()[0]
        # Keep the game filter as "v.game_id = any (array(...))". Written as a join to games, Postgres computes
        # PVAdj for every lineup row ever saved before filtering, which grew past the 2-minute statement timeout.
        cur.execute("""
            select count(distinct v.player_id)
            from v_pregame_lineup_pvadj_players v
            where v.pvadj is null
              and v.game_id = any (array(select game_id from games
                                         where playoff = false and home_goals is null and date >= current_date - 1))""")
        nostats = cur.fetchone()[0]
    return results, unmatched, nostats


def render_attention_callout(results, unmatched, nostats):
    parts = []
    if results:
        parts.append(f"{len(results)} new player(s) from box scores need stats: " + ", ".join(results))
    if unmatched:
        parts.append(f"{unmatched} unmatched player(s) in upcoming lineups")
    if nostats:
        parts.append(f"{nostats} upcoming-lineup player(s) with no stats (PVAdj missing)")
    if not parts:
        return ""
    return ("<div class=\"attn\"><b>Needs attention:</b> " + "; ".join(parts) +
            f".<br>Run <code>{ADD_PLAYER_CMD}</code> locally.</div>")


def render_preliminary_signal_callout(rows):
    """Top-of-page flag for today's games (get_todays_games rows) whose frozen signal used preliminary data: the
    T-5 confirmed capture did not fully complete, for example because DailyFaceoff refused the request."""
    games = []
    for (_gid, home, away, h_fire, _s1, _m1, _l1, h_snap, a_fire, _s2, _m2, _l2, a_snap) in rows:
        if "preliminary" in (h_snap, a_snap):
            games.append(f"{away.upper()} @ {home.upper()}" + (" (BET)" if h_fire or a_fire else ""))
    if not games:
        return ""
    return ("<div class=\"attn\"><b>Signal frozen without full confirmed data:</b> " + ", ".join(games) +
            ".<br>At least one lineup or goalie was still preliminary when the signal froze. Check the current "
            "lineups before relying on it; the game's T-5 run in GitHub Actions shows any refused request.</div>")


# --------------------------- historical instances ---------------------------

def get_all_seasons(conn):
    """Every CONFIGURED season, not just ones with settled games -- so a season with zero games played
    yet (2026-27, before opening night) still shows up in the season table with a zero/dash row."""
    with conn.cursor() as cur:
        cur.execute("select season from season_config order by season desc")
        return [r[0] for r in cur.fetchall()]


def get_season_wallet_returns(conn):
    """Per-season wallet return %: reset to that season's own season_config.starting_bankroll and compound
    forward using ONLY that season's own v_kelly_daily_return rows (i.e. NOT the continuous cross-season
    v_kelly_bank_theoretical series used elsewhere on this page), expressed as a percentage of that
    starting bankroll -- Nick: "$5000 bank, $2396 profit, that's a return of 47%", so wallet_return_pct =
    wallet_return_dollar / starting_bankroll. The dollar figure itself was verified against Nick's own
    reference number: 1819 comes out to exactly $2,396.52 -- matching what he cited -- off a $5,000
    starting bankroll (season_config's actual configured value, not the $2,500 he mentioned once;
    flagged in chat, not silently changed to match)."""
    with conn.cursor() as cur:
        cur.execute("""
            select sc.season, sc.starting_bankroll,
                   sc.starting_bankroll * exp(coalesce(sum(ln(1 + r.daily_return_pct)), 0))
            from season_config sc
            left join v_kelly_daily_return r on r.season = sc.season
            group by sc.season, sc.starting_bankroll
        """)
        rows = cur.fetchall()
    out = {}
    for season, start, end in rows:
        start, end = float(start), float(end)
        wallet_return = end - start
        out[season] = {"starting_bankroll": start, "ending_bank": end, "wallet_return": wallet_return,
                       "wallet_return_pct": (wallet_return / start) if start else None}
    return out


def get_detail_season(conn):
    override = os.environ.get("DETAIL_SEASON", "").strip()
    if override:
        return override
    with conn.cursor() as cur:
        cur.execute("select season from v_kelly_signal order by season desc limit 1")
        row = cur.fetchone()
    return row[0] if row else None


def get_instances(conn, seasons=None, bank_overrides=None):
    """One row per team-game-instance, for the given seasons (or all if None).
    Dollar figures use v_kelly_bank_theoretical's bankroll for that date -- one consistent all-history
    compounding series -- EXCEPT for seasons in bank_overrides ({season: {date: bank_start_of_date}}),
    which use that in-year bank instead (the current season, sized off its own $5k start)."""
    bank_overrides = bank_overrides or {}
    query = """
        select p.game_id, p.team, p.opp, p.season, p.date, p.home, p.win, p.model_pct, p.mlpct,
               p.logloss, p.vlogloss, k.kf, k.bets_fire, sc.kelly_fraction, l.lambda,
               bt.bank_start_of_date, g.home_goals, g.away_goals
        from v_team_game_perspective p
        join v_kelly_signal k on k.game_id=p.game_id and k.team=p.team
        join v_lambda l on l.game_id=p.game_id and l.team=p.team
        join season_config sc on sc.season=p.season
        join games g on g.game_id=p.game_id
        left join v_kelly_bank_theoretical bt on bt.season=p.season and bt.date=p.date
    """
    params = ()
    if seasons:
        query += " where p.season = any(%s)"
        params = (list(seasons),)
    query += " order by p.date, p.game_id, p.team"  # full tiebreak: identical input must render identical output
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    instances = []
    for (game_id, team, opp, season, gdate, home, win, model_pct, mlpct, logloss, vlogloss,
         kf, bets_fire, kelly_fraction, lam, bank, home_goals, away_goals) in rows:
        stake_dollar = profit_dollar = 0.0
        if season in bank_overrides:
            bank = bank_overrides[season].get(gdate)
        if bets_fire and bank is not None:
            stake_dollar = float(kelly_fraction) * float(kf) * float(lam) * float(bank)
            # Net decimal odds (b) isn't selected directly above; recover it from mlpct the same way
            # v_team_kf's own CASE does from ml -- mlpct and ml are a 1:1 monotonic pair, so this is exact.
            b = (1.0 - float(mlpct)) / float(mlpct) if float(mlpct) > 0 else 0.0
            profit_dollar = stake_dollar * (b if win else -1.0)
        instances.append({
            "game_id": game_id, "team": team, "opp": opp, "season": season, "date": gdate, "home": home,
            "win": win, "model_pct": float(model_pct) if model_pct is not None else None,
            "mlpct": float(mlpct) if mlpct is not None else None,
            "logloss": float(logloss) if logloss is not None else None,
            "vlogloss": float(vlogloss) if vlogloss is not None else None,
            "bets_fire": bool(bets_fire), "stake_dollar": stake_dollar, "profit_dollar": profit_dollar,
            "home_goals": home_goals, "away_goals": away_goals,
            # raw sizing inputs, so rebank() can re-price a season off a different bankroll without a re-query
            # (nullable: kf / lambda are NULL for games with no signal, and those rows never bet)
            "kf": float(kf) if kf is not None else None, "lam": float(lam) if lam is not None else None,
            "kelly_fraction": float(kelly_fraction) if kelly_fraction is not None else None,
        })
    return instances


def rebank(instances, start_by_date):
    """Re-price instances off a different bankroll ({date: bank at the start of that date}) using the same
    formulas as get_instances. A season shown on its own (the Season summary / By week / By team views) is always
    priced off its own in-year bank, exactly like the current season, whichever season is picked."""
    out = []
    for x in instances:
        y = dict(x)
        y["stake_dollar"] = y["profit_dollar"] = 0.0
        bank = start_by_date.get(x["date"])
        if (y["bets_fire"] and bank is not None and y["kf"] is not None and y["lam"] is not None
                and y["kelly_fraction"] is not None and y["mlpct"] is not None):
            stake = y["kelly_fraction"] * y["kf"] * y["lam"] * float(bank)
            mk = y["mlpct"]
            b = (1.0 - mk) / mk if mk > 0 else 0.0
            y["stake_dollar"] = stake
            y["profit_dollar"] = stake * (b if y["win"] else -1.0)
        out.append(y)
    return out


def pair_by_game(instances):
    """Groups the per-team-perspective instances back into one row per GAME, away side first, home side
    second, matching how a viewer reads a schedule (Nick: consistent away-then-home ordering, actual score
    shown). Returns a list sorted by date, each item holding both sides' full instance dict under 'away'/'home'."""
    by_game = {}
    for x in instances:
        by_game.setdefault(x["game_id"], {})["home" if x["home"] else "away"] = x
    pairs = []
    for game_id, sides in by_game.items():
        away, home = sides.get("away"), sides.get("home")
        if away is None or home is None:
            continue  # shouldn't happen -- v_team_game_perspective always has both sides for a completed game
        pairs.append({"game_id": game_id, "date": away["date"], "away": away, "home": home})
    return sorted(pairs, key=lambda p: (p["date"], p["game_id"]))


def season_start_dates(conn):
    with conn.cursor() as cur:
        cur.execute("select season, season_start_date from season_config")
        return {r[0]: r[1] for r in cur.fetchall()}


def calendar_week(game_date, season_start):
    return (game_date - season_start).days // 7 + 1


def cumulative_avg_ll_delta_series(instances):
    """(date, running average of logloss-vlogloss over every instance up to and including that date)
    -- a cumulative average, not a per-day one, so it doesn't whipsaw on days with only 1-2 games."""
    by_date = {}
    for x in sorted(instances, key=lambda x: x["date"]):
        if x["logloss"] is not None and x["vlogloss"] is not None:
            by_date.setdefault(x["date"], []).append(x["logloss"] - x["vlogloss"])
    out = []
    total, n = 0.0, 0
    for dt in sorted(by_date):
        for v in by_date[dt]:
            total += v
            n += 1
        out.append((dt, total / n))
    return out


# --------------------------- aggregation ---------------------------

def aggregate(instances):
    """bets/wagered/profit/return are always about the fired bets. LogLoss is reported two ways -- over
    just the fired-bet instances ('bet_*') and over every instance ('all_*') -- per Nick: show LL/MktLL/LL
    for the games actually bet on first, then for all games that week/team."""
    fired = [x for x in instances if x["bets_fire"]]
    wins = sum(1 for x in fired if x["win"])
    losses = len(fired) - wins
    wagered = sum(x["stake_dollar"] for x in fired)
    profit = sum(x["profit_dollar"] for x in fired)

    def ll_pair(pool):
        ll = [x["logloss"] for x in pool if x["logloss"] is not None]
        vll = [x["vlogloss"] for x in pool if x["vlogloss"] is not None]
        return (sum(ll) / len(ll)) if ll else None, (sum(vll) / len(vll)) if vll else None

    bet_ll, bet_vll = ll_pair(fired)
    all_ll, all_vll = ll_pair(instances)
    return {
        "n_instances": len(instances), "bets_placed": len(fired), "wins": wins, "losses": losses,
        "wagered": wagered, "profit": profit,
        "return_pct": (profit / wagered) if wagered else None,
        "bet_model_logloss": bet_ll, "bet_market_logloss": bet_vll,
        "all_model_logloss": all_ll, "all_market_logloss": all_vll,
        # kept for the (unchanged) season table, which still shows one LogLoss pair over all instances
        "model_logloss": all_ll, "market_logloss": all_vll,
    }


def group_by(instances, key_fn):
    groups = {}
    for x in instances:
        groups.setdefault(key_fn(x), []).append(x)
    return groups


# --------------------------- rendering ---------------------------

STYLE = """
:root{--ice:#eef3f8;--sheet:#fff;--ink:#16233b;--muted:#66758c;--faint:#8591a3;--rule:#e3e9f1;--navy:#1b2a49;
--navy-ink:#d6e0f2;--blue:#2f6fd0;--tint:#f4f7fb;--bet:#eaf7ef;--line-soft:#cdd7e6}
*{box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
max-width:1200px;margin:0 auto;padding:0 16px 40px;background:var(--ice);color:var(--ink);
font-variant-numeric:tabular-nums;-webkit-text-size-adjust:100%}
.top{display:flex;align-items:baseline;gap:6px 14px;flex-wrap:wrap;padding:18px 0 8px}
.top h1{font-size:22px;margin:0;font-weight:700;letter-spacing:-.01em;color:var(--navy)}
.stamp{font-size:13px;color:var(--muted)}
.tabbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;position:sticky;top:0;z-index:5;
background:var(--ice);padding:8px 0 10px}
.tabs{display:flex;gap:4px;overflow-x:auto;max-width:100%;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab{appearance:none;border:0;background:transparent;font:inherit;font-size:14px;font-weight:600;color:var(--muted);
padding:8px 14px;border-radius:999px;cursor:pointer;white-space:nowrap}
.tab:hover{color:var(--navy);background:#dfe8f3}
.tab[aria-selected=true]{background:var(--navy);color:#fff}
.tab .sub{font-weight:500;opacity:.8;margin-left:5px;font-size:12px}
.tab:focus-visible,.season-pick select:focus-visible,.nested summary:focus-visible,.block>summary:focus-visible
{outline:2px solid var(--blue);outline-offset:2px}
.season-pick{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted);white-space:nowrap}
.season-pick[hidden]{display:none}
.season-pick select{font:inherit;font-size:14px;font-weight:600;color:var(--navy);background:#fff;
border:1px solid #cfd9e8;border-radius:8px;padding:6px 10px}
.js .panel{display:none}
.js .panel.active{display:block}
h2{font-size:16px;font-weight:650;margin:22px 0 10px;color:var(--ink)}
.panel>h2:first-child,.season-content>h2:first-child{margin-top:6px}
.meta{color:var(--muted);font-size:13px;margin:0 0 10px}
.tag{color:var(--muted);font-size:11px}
.ml{color:var(--faint);font-size:11px;font-style:italic;font-weight:400}
.scroll-wrap{overflow-x:auto;margin-bottom:12px;background:var(--sheet);border-radius:10px;
box-shadow:0 1px 2px rgba(22,35,59,.06),0 4px 14px rgba(22,35,59,.05)}
table{width:100%;border-collapse:collapse;background:var(--sheet)}
th,td{padding:10px 10px;text-align:left;font-size:14px;border-bottom:1px solid var(--rule)}
th{background:var(--navy);color:var(--navy-ink);font-size:12.5px;font-weight:600;white-space:nowrap}
tr:last-child>td{border-bottom:0}
table.today td{vertical-align:middle}
table.today td.matchup{font-weight:600}
table.today tr.team-top td{border-bottom:1px dashed #b9c4d6}
table.today tr.game-end td{border-bottom:3px solid var(--line-soft)}
table.today tr.team-top td.matchup{border-bottom:3px solid var(--line-soft)}
table.today tr.bet td:not(.matchup){background:var(--bet)}
table.today tr:last-child>td{border-bottom:0}
.row-grid{display:grid;gap:4px 10px;align-items:center;padding:10px 14px;font-size:13px;min-width:820px}
.row-grid>div{white-space:nowrap}
.group-head,.col-head{background:var(--navy);color:var(--navy-ink);font-weight:600;font-size:11.5px}
.group-head{padding-bottom:0}
.col-head{padding-top:4px}
.group-label{text-align:center;border-bottom:1px solid rgba(214,224,242,.35);padding-bottom:3px}
.block{background:var(--sheet);border-top:1px solid var(--rule)}
.block:first-of-type{border-top:none}
.block>.row-grid+.row-grid{border-top:1px solid var(--rule)}
.block>summary{cursor:pointer;list-style:none;position:relative}
.block>summary::-webkit-details-marker{display:none}
.block>summary::before{content:'';position:absolute;left:5px;top:50%;margin-top:-4px;border-style:solid;
border-width:4px 0 4px 6px;border-color:transparent transparent transparent var(--faint)}
.block[open]>summary::before{margin-top:-2px;border-width:6px 4px 0 4px;border-color:var(--faint) transparent transparent transparent}
.block>summary .row-grid>div:first-child,.block>summary .row-grid>*:first-child{padding-left:20px}
.block:hover>summary{background:#f8fafd}
.block-title{font-weight:700}
.nested{margin:0 14px 12px;border:1px solid var(--rule);border-radius:8px;overflow:hidden}
.nested summary{padding:9px 12px;cursor:pointer;list-style:none;font-size:13px;font-weight:600;color:var(--ink);
background:var(--tint)}
.nested summary::-webkit-details-marker{display:none}
.nested summary::before{content:'';display:inline-block;vertical-align:middle;margin:-2px 8px 0 0;border-style:solid;
border-width:4px 0 4px 6px;border-color:transparent transparent transparent var(--faint)}
.nested[open] summary::before{border-width:6px 4px 0 4px;border-color:var(--faint) transparent transparent transparent}
.nested .scroll-wrap{box-shadow:none;border-radius:0;margin:0}
table.detail{border-radius:0;margin:0;box-shadow:none}
table.detail th{background:#e9eff8;color:var(--ink)}
table.detail th,table.detail td{padding:8px 10px;white-space:nowrap}
.chart{display:block;width:100%;height:auto;background:#fff;border-radius:10px;margin-bottom:12px;
box-shadow:0 1px 2px rgba(22,35,59,.06),0 4px 14px rgba(22,35,59,.05)}
.attn{border:1px solid #e0b252;background:#fff8e6;padding:9px 14px;margin:2px 0 6px;border-radius:8px;font-size:13.5px}
.day-tip{position:fixed;display:none;z-index:10;background:#16233b;color:#fff;font-size:12px;line-height:1.5;
padding:8px 10px;border-radius:6px;pointer-events:none;max-width:320px;box-shadow:0 2px 10px rgba(0,0,0,.3)}
@media (max-width:480px){th,td{font-size:12.5px;padding:8px 6px}body{padding:0 10px 32px}.top h1{font-size:19px}}
"""


def logloss_delta_bg(delta, scale=0.04):
    if delta is None:
        return ""
    intensity = min(abs(delta) / scale, 1.0)
    alpha = 0.10 + intensity * 0.55
    rgb = "26,127,55" if delta < 0 else "192,57,43"
    return f"background:rgba({rgb},{alpha:.2f});"


def money(v):
    """Signed whole-dollar figure. Rounds first so a tiny negative never renders as '-$0'."""
    r = round(v)
    return f"{'+' if r >= 0 else '-'}${abs(r):,.0f}"


def ml_from_prob(p):
    """American moneyline implied by a win probability (the inverse of mlpct_from_moneyline), rounded to a whole
    number. Used for the model's own implied line; also the fallback for a market line we don't have stored."""
    if p is None or not (0.0 < p < 1.0):
        return None
    return -round(100.0 * p / (1.0 - p)) if p > 0.5 else round(100.0 * (1.0 - p) / p)


def fmt_ml(ml):
    return f"{'+' if ml > 0 else '-' if ml < 0 else ''}{abs(int(ml))}"


def ml_sub(ml, title):
    """Small, light, italic line printed under a probability."""
    if ml is None:
        return ""
    return f"<br><span class=\"ml\" title=\"{title}\">{fmt_ml(ml)}</span>"


def _nice_axis(lo, hi, min_span, n_ticks=5):
    """Expand [lo, hi] to at least min_span, then snap to round tick values (1/2/5 x 10^k steps).
    Returns (axis_lo, axis_hi, ticks, step)."""
    if hi - lo < min_span:
        mid = (hi + lo) / 2
        lo, hi = mid - min_span / 2, mid + min_span / 2
    raw = (hi - lo) / (n_ticks - 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    a_lo = math.floor(lo / step + 1e-9) * step
    a_hi = math.ceil(hi / step - 1e-9) * step
    ticks, t = [], a_lo
    while t <= a_hi + step * 1e-6:
        ticks.append(round(t, 10))
        t += step
    return a_lo, a_hi, ticks, step


def build_day_tips(bank_days, instances):
    """{date: tooltip html} for every settled day on the daily-result chart: the day's record, dollars, return,
    bank, LogLoss (bets placed and all games) and each bet. instances = the detail season's team-game instances."""
    by_date = group_by(instances, lambda x: x["date"])

    def ll_line(label, ll, vll):
        if ll is None or vll is None:
            return f"<div>{label}: -</div>"
        return f"<div>{label}: {ll:.3f} vs mkt {vll:.3f} ({ll - vll:+.3f})</div>"

    tips = {}
    for d, bank_end, result in bank_days:
        xs = by_date.get(d, [])
        s = aggregate(xs)
        n_games = len({x["game_id"] for x in xs})
        color = "#7ee2a0" if result >= 0 else "#ff8a80"
        ret = f" ({s['return_pct']:+.1%} on ${s['wagered']:,.0f})" if s["return_pct"] is not None else ""
        parts = [f"<div><b>{d.strftime('%a, %b')} {d.day}</b></div>",
                 f"<div>Bets: {s['bets_placed']} ({s['wins']}-{s['losses']}) &middot; {n_games} games played</div>",
                 f"<div>Result: <span style=\"color:{color}; font-weight:600;\">{money(result)}</span>{ret}</div>",
                 f"<div>Bank after: ${bank_end:,.0f}</div>",
                 ll_line("LogLoss, bets", s["bet_model_logloss"], s["bet_market_logloss"]),
                 ll_line(f"LogLoss, all {n_games} games", s["all_model_logloss"], s["all_market_logloss"])]
        bets = sorted((x for x in xs if x["bets_fire"]), key=lambda x: (x["team"], x["game_id"]))
        if bets:
            parts.append("<div style=\"margin-top:4px; border-top:1px solid #555; padding-top:4px;\"></div>")
        for x in bets:
            c = "#7ee2a0" if x["win"] else "#ff8a80"
            parts.append(f"<div>{html_lib.escape(x['team'].upper())} ${x['stake_dollar']:,.0f} &mdash; "
                         f"<span style=\"color:{c};\">{'won' if x['win'] else 'lost'} {money(x['profit_dollar'])}</span></div>")
        tips[d] = "".join(parts)
    return tips


SITE_JS = """(function(){
var IDS=['today','yesterday','summary','week','team','past'],SEASON_TABS={summary:1,week:1,team:1};
var tabs=[].slice.call(document.querySelectorAll('.tab')),panels={},pick=document.getElementById('season-pick'),
sel=document.getElementById('season-select'),tip=document.getElementById('day-tip');
IDS.forEach(function(id){panels[id]=document.getElementById('panel-'+id);});
function hideTip(){tip.style.display='none';[].forEach.call(document.querySelectorAll('.day-hl'),
function(h){h.setAttribute('visibility','hidden');});}
function show(id,write){
if(!panels[id])id='today';hideTip();
tabs.forEach(function(t){var on=t.getAttribute('data-tab')===id;t.setAttribute('aria-selected',on?'true':'false');
t.tabIndex=on?0:-1;});
IDS.forEach(function(k){panels[k].classList.toggle('active',k===id);});
if(pick)pick.hidden=!SEASON_TABS[id];
if(write&&history.replaceState)history.replaceState(null,'','#'+id);}
tabs.forEach(function(t,i){
t.addEventListener('click',function(){show(t.getAttribute('data-tab'),true);});
t.addEventListener('keydown',function(e){var d=e.key==='ArrowRight'?1:(e.key==='ArrowLeft'?-1:0);if(!d)return;
var n=tabs[(i+d+tabs.length)%tabs.length];n.focus();show(n.getAttribute('data-tab'),true);e.preventDefault();});});
window.addEventListener('hashchange',function(){show(location.hash.slice(1),false);});
show(location.hash.slice(1),false);
if(sel){
var holders={},cache={};
['summary','week','team'].forEach(function(k){holders[k]=document.querySelector('.season-content[data-part="'+k+'"]');});
cache[sel.getAttribute('data-default')]={summary:holders.summary.innerHTML,week:holders.week.innerHTML,
team:holders.team.innerHTML};
var apply=function(p){hideTip();for(var k in holders)holders[k].innerHTML=p[k];};
var msg=function(t){for(var k in holders)holders[k].innerHTML='<p class="meta">'+t+'</p>';};
sel.addEventListener('change',function(){
var s=sel.value;if(cache[s]){apply(cache[s]);return;}
msg('Loading season…');
fetch(new URL('seasons/'+s+'.json',document.baseURI)).then(function(r){if(!r.ok)throw 0;return r.json();})
.then(function(p){cache[s]=p;if(sel.value===s)apply(p);})
.catch(function(){if(sel.value===s)msg('Could not load that season. Reload the page and try again.');});});}
var pts=new WeakMap();
function info(svg){var d=pts.get(svg);if(!d){try{d={p:JSON.parse(svg.getAttribute('data-points')),
r:parseFloat(svg.getAttribute('data-radius'))||6};}catch(e){d={p:[],r:6};}pts.set(svg,d);}return d;}
function isHit(el){return el&&el.classList&&el.classList.contains('day-hit');}
function showTip(ev){
if(!isHit(ev.target))return;
var svg=ev.target.ownerSVGElement,d=info(svg),m=svg.getScreenCTM();if(!m)return;
var pt=svg.createSVGPoint();pt.x=ev.clientX;pt.y=ev.clientY;
var x=pt.matrixTransform(m.inverse()).x,best=null,bd=1e9;
for(var i=0;i<d.p.length;i++){var dd=Math.abs(d.p[i].x-x);if(dd<bd){bd=dd;best=d.p[i];}}
if(!best||bd>d.r){hideTip();return;}
tip.innerHTML=best.html;tip.style.display='block';
var hl=svg.querySelector('.day-hl');
if(hl){hl.setAttribute('x',best.x-parseFloat(hl.getAttribute('width'))/2);hl.setAttribute('visibility','visible');}
var pad=14,w=tip.offsetWidth,h=tip.offsetHeight,tx=ev.clientX+pad,ty=ev.clientY+pad;
if(tx+w>window.innerWidth-4)tx=ev.clientX-w-pad;
if(ty+h>window.innerHeight-4)ty=ev.clientY-h-pad;
tip.style.left=Math.max(4,tx)+'px';tip.style.top=Math.max(4,ty)+'px';}
document.addEventListener('pointermove',showTip);
document.addEventListener('pointerdown',function(ev){if(isHit(ev.target))showTip(ev);else hideTip();});
document.addEventListener('pointerout',function(ev){if(ev.pointerType==='mouse'&&isHit(ev.target))hideTip();});
})();"""


def render_wallet_chart_svg(bank_days, ll_delta_series, start_bank, season_start, season_end, width=760,
                             day_tips=None):
    """Self-contained inline SVG (no chart library / CDN, so the page stays static on GitHub Pages).

    Two stacked panels sharing one x axis that spans the FULL season (season_start..season_end); lines and
    bars stop at the last settled day.
      Top panel    -- in-year wallet $ (left axis, green; starts at the season's starting bankroll) and
                      cumulative average LogLoss delta (right axis, purple, fixed at -0.005..+0.005 and clipped). The dashed purple line is the
                      LL-delta zero line: below it the model has beaten the market, above it the market has
                      beaten the model.
      Bottom panel -- each day's total $ result (green up / red down).
    bank_days: [(date, bank_at_end_of_day, result_dollars)]; ll_delta_series: [(date, cumulative avg delta)].
    day_tips: optional {date: tooltip html} (see build_day_tips). When given, hovering (or tapping) a daily-result
    bar shows that day's tooltip -- handled by SITE_JS, which reads the data-points / data-radius attributes
    written onto the <svg> and listens on the transparent #day-hit overlay."""
    if not bank_days:
        return "<p class=\"meta\">No settled days yet this season.</p>"

    left, right = 64, 64
    plot_w = width - left - right
    p1_top, p1_h = 34, 210
    p2_top, p2_h = p1_top + p1_h + 34, 84
    x_label_y = p2_top + p2_h + 20
    height = x_label_y + 10
    dmin, dmax = season_start, max(season_end, bank_days[-1][0])
    dspan = max(1, (dmax - dmin).days)

    def x_of(dt):
        return left + (dt - dmin).days / dspan * plot_w

    def y_mapper(top, h, lo, hi):
        return lambda v: top + (1 - (v - lo) / (hi - lo)) * h

    GRID, AXIS = "#ececec", "#b5b5b5"
    svg = [f'<svg class="chart" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
           f'font-family="-apple-system, sans-serif">']

    # ---- x ticks: weekly from season start, shared by both panels ----
    n_weeks = dspan // 7
    label_every = 1 if n_weeks <= 14 else 2
    week_dates = [dmin + timedelta(days=7 * k) for k in range(n_weeks + 1)]
    for top, h in ((p1_top, p1_h), (p2_top, p2_h)):
        for dt in week_dates:
            svg.append(f'<line x1="{x_of(dt):.1f}" x2="{x_of(dt):.1f}" y1="{top}" y2="{top + h}" '
                       f'stroke="{GRID}" stroke-width="1" />')
        svg.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{top + h}" y2="{top + h}" stroke="{AXIS}" />')
        for dt in week_dates:
            svg.append(f'<line x1="{x_of(dt):.1f}" x2="{x_of(dt):.1f}" y1="{top + h}" y2="{top + h + 4}" '
                       f'stroke="{AXIS}" />')
    for k, dt in enumerate(week_dates):
        if k % label_every == 0:
            svg.append(f'<text x="{x_of(dt):.1f}" y="{x_label_y}" font-size="9" fill="#888" '
                       f'text-anchor="middle">{dt.month}/{dt.day}</text>')

    # ---- top panel: wallet (left) ----
    wallet_vals = [start_bank] + [b for _, b, _ in bank_days]
    w_lo, w_hi, w_ticks, _ = _nice_axis(min(wallet_vals), max(wallet_vals), min_span=start_bank * 0.10)
    y_w = y_mapper(p1_top, p1_h, w_lo, w_hi)
    svg.append(f'<line x1="{left}" x2="{left}" y1="{p1_top}" y2="{p1_top + p1_h}" stroke="{AXIS}" />')
    for v in w_ticks:
        y = y_w(v)
        svg.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}" />')
        svg.append(f'<line x1="{left - 4}" x2="{left}" y1="{y:.1f}" y2="{y:.1f}" stroke="#1a7f37" />')
        svg.append(f'<text x="{left - 8}" y="{y:.1f}" font-size="10" fill="#1a7f37" text-anchor="end" '
                   f'dominant-baseline="middle">${v:,.0f}</text>')

    # ---- top panel: LL delta (right) ----
    # Fixed range (see LL_DELTA_AXIS_*): early-season swings would otherwise blow the scale out. Anything outside
    # the range is clipped at the panel edge by the clipPath below.
    d_step = LL_DELTA_AXIS_STEP
    d_lo, d_hi = -LL_DELTA_AXIS_HALF_RANGE, LL_DELTA_AXIS_HALF_RANGE
    n_d = int(round((d_hi - d_lo) / d_step))
    d_ticks = [round(d_lo + i * d_step, 10) for i in range(n_d + 1)]
    y_d = y_mapper(p1_top, p1_h, d_lo, d_hi)
    decimals = 3
    svg.append(f'<defs><clipPath id="ll-clip"><rect x="{left}" y="{p1_top}" width="{plot_w}" height="{p1_h}" />'
               f'</clipPath></defs>')
    svg.append(f'<line x1="{left + plot_w}" x2="{left + plot_w}" y1="{p1_top}" y2="{p1_top + p1_h}" '
               f'stroke="{AXIS}" />')
    for v in d_ticks:
        y = y_d(v)
        is_zero = abs(v) < d_step * 1e-6
        svg.append(f'<line x1="{left + plot_w}" x2="{left + plot_w + 4}" y1="{y:.1f}" y2="{y:.1f}" '
                   f'stroke="#6a3fb5" />')
        label = f"{0:.{decimals}f}" if is_zero else f"{v:+.{decimals}f}"
        weight = ' font-weight="700"' if is_zero else ""
        svg.append(f'<text x="{left + plot_w + 8}" y="{y:.1f}" font-size="10" fill="#6a3fb5"{weight} '
                   f'text-anchor="start" dominant-baseline="middle">{label}</text>')
    svg.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{y_d(0):.1f}" y2="{y_d(0):.1f}" '
               f'stroke="#9d86d4" stroke-width="1.2" stroke-dasharray="5,4" />')

    # lines stop at the last settled day
    wallet_pts = [(season_start, start_bank)] + [(d, b) for d, b, _ in bank_days]
    svg.append('<polyline points="' + " ".join(f"{x_of(d):.1f},{y_w(v):.1f}" for d, v in wallet_pts) +
               '" fill="none" stroke="#1a7f37" stroke-width="2" />')
    if ll_delta_series:
        svg.append('<polyline clip-path="url(#ll-clip)" points="' +
                   " ".join(f"{x_of(d):.1f},{y_d(v):.1f}" for d, v in ll_delta_series) +
                   '" fill="none" stroke="#6a3fb5" stroke-width="2" />')

    # ---- bottom panel: daily result bars ----
    results = [r for _, _, r in bank_days]
    b_lo, b_hi, b_ticks, _ = _nice_axis(min(results + [0.0]), max(results + [0.0]),
                                        min_span=max(start_bank * 0.02, 1.0), n_ticks=4)
    y_b = y_mapper(p2_top, p2_h, b_lo, b_hi)
    svg.append(f'<line x1="{left}" x2="{left}" y1="{p2_top}" y2="{p2_top + p2_h}" stroke="{AXIS}" />')
    for v in b_ticks:
        y = y_b(v)
        svg.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}" />')
        svg.append(f'<line x1="{left - 4}" x2="{left}" y1="{y:.1f}" y2="{y:.1f}" stroke="#555" />')
        svg.append(f'<text x="{left - 8}" y="{y:.1f}" font-size="10" fill="#555" text-anchor="end" '
                   f'dominant-baseline="middle">{"$0" if abs(v) < 1e-9 else money(v)}</text>')
    svg.append(f'<line x1="{left}" x2="{left + plot_w}" y1="{y_b(0):.1f}" y2="{y_b(0):.1f}" stroke="#888" />')
    bar_w = max(2.0, min(14.0, plot_w / dspan * 0.7))
    hover_points = []
    for d, _, r in bank_days:
        if abs(r) < 0.5:
            continue
        y0, y1 = y_b(0), y_b(r)
        svg.append(f'<rect x="{x_of(d) - bar_w / 2:.1f}" y="{min(y0, y1):.1f}" width="{bar_w:.1f}" '
                   f'height="{max(abs(y1 - y0), 1):.1f}" fill="{"#1a7f37" if r >= 0 else "#c0392b"}" />')
        if day_tips and d in day_tips:
            hover_points.append({"x": round(x_of(d), 1), "html": day_tips[d]})
    svg.append(f'<text x="{left + 6}" y="{p2_top - 8}" font-size="11" fill="#444">Daily result ($)</text>')
    if hover_points:
        # Highlight band (moved by SITE_JS) and a transparent overlay over the whole bottom panel, so a bar a few
        # pixels wide is easy to hit: the script picks the nearest bar within data-radius.
        svg.append(f'<rect class="day-hl" x="0" y="{p2_top}" width="{bar_w + 6:.1f}" height="{p2_h}" fill="#333" '
                   f'fill-opacity="0.08" visibility="hidden" pointer-events="none" />')
        svg.append(f'<rect class="day-hit" x="{left}" y="{p2_top}" width="{plot_w}" height="{p2_h}" '
                   f'fill="transparent" style="touch-action:pan-y; cursor:crosshair;" />')
        radius = max(5.0, plot_w / dspan * 1.2)
        data = html_lib.escape(json.dumps(hover_points), quote=True)
        svg[0] = svg[0].replace("<svg ", f'<svg data-points="{data}" data-radius="{radius:.1f}" ', 1)

    # ---- legend ----
    svg.append(
        '<g font-size="11">'
        f'<rect x="{left}" y="10" width="10" height="10" fill="#1a7f37" />'
        f'<text x="{left + 14}" y="19" fill="#444">Wallet ($, left)</text>'
        f'<rect x="{left + 130}" y="10" width="10" height="10" fill="#6a3fb5" />'
        f'<text x="{left + 144}" y="19" fill="#444">Cumulative avg LL &#916; (right)</text>'
        f'<line x1="{left + 330}" x2="{left + 354}" y1="15" y2="15" stroke="#9d86d4" stroke-width="1.5" '
        f'stroke-dasharray="5,4" />'
        f'<text x="{left + 360}" y="19" fill="#444">LL &#916; = 0 (below = model beats market)</text>'
        '</g>'
    )
    svg.append("</svg>")
    return "".join(svg)


def render_todays_games(rows, skater_data=None, goalie_data=None, bankroll=None, start_times=None,
                        preview=None, odds_asof=None, moneylines=None):
    if not rows:
        return "<p class=\"meta\">No games scheduled today.</p>"
    snaps, queue = skater_data if skater_data else ({}, {})
    goalie_data = goalie_data or {}
    # Freshness: per-cell "as of" by default; if every row shares one scrape time (to the minute) it is shown
    # once above the table instead.
    g_times, s_times = [], []
    for (game_id, home, away, _f1, _s1, _m1, _l1, h_snap, _f2, _s2, _m2, _l2, a_snap) in rows:
        for team, snap in ((home, h_snap), (away, a_snap)):
            g_times.append(goalie_scraped_at(game_id, team, snap, goalie_data))
            s_times.append(skater_scraped_at(game_id, team, snap, snaps))

    def uniform(times):
        known = {t.replace(second=0, microsecond=0) for t in times if t is not None}
        return len(known) == 1 and all(t is not None for t in times)
    g_uniform, s_uniform = uniform(g_times), uniform(s_times)
    notes = []
    if g_uniform:
        notes.append(f"Goalies as of {fmt_scraped(g_times[0])}")
    if s_uniform:
        notes.append(f"Lineups as of {fmt_scraped(s_times[0])}")
    top_note = f"<p class=\"meta\">{' &middot; '.join(notes)} (last DailyFaceoff read)</p>" if notes else ""
    start_times, preview, moneylines = start_times or {}, preview or {}, moneylines or {}
    if preview:
        asof = f" Live odds as of {fmt_scraped(odds_asof)}." if odds_asof else ""
        top_note += ("<p class=\"meta\">Rows tagged <b>prelim</b> use the current DraftKings line and the lineups/"
                     "goalies captured so far, and refresh with each page update; the frozen signal replaces "
                     f"them at T-5.{asof}</p>")
    html = ""
    for (game_id, home, away, h_fire, h_stake, h_mpct, h_mlpct, h_snap,
         a_fire, a_stake, a_mpct, a_mlpct, a_snap) in rows:
        # Home row first, away row second. The matchup cell (with the game time beneath) spans both rows, so it
        # is only emitted on the first. A dashed rule separates the two teams; a thick rule closes out the game.
        t0 = start_times.get(game_id)
        matchup_td = (f"<td class=\"matchup\" rowspan=\"2\">{away.upper()} @ {home.upper()}"
                      + (f"<br><span class=\"tag\" style=\"font-size:12px;\">{fmt_clock(t0)}</span>" if t0 else "")
                      + "</td>")
        for team, opp, is_home, fire, stake, mpct, mlpct, snap in (
            (home, away, True, h_fire, h_stake, h_mpct, h_mlpct, h_snap),
            (away, home, False, a_fire, a_stake, a_mpct, a_mlpct, a_snap),
        ):
            row_cls = ("team-top" if is_home else "game-end") + (" bet" if fire else "")
            lead_td = matchup_td if is_home else ""
            check_td = skater_check_cell(game_id, team, snap, snaps, queue, show_time=not s_uniform)
            goalie_td = goalie_cell(game_id, team, snap, goalie_data, show_time=not g_uniform)
            pv = preview.get((game_id, team)) if mpct is None else None
            if mpct is None and pv is None:
                html += (
                    f"<tr class=\"{row_cls}\">{lead_td}"
                    f"<td>{team.upper()} {'(H)' if is_home else '(A)'}</td>"
                    "<td colspan=\"5\" class=\"tag\">awaiting odds/lineup data -- no signal yet</td>"
                    f"{goalie_td}{check_td}</tr>"
                )
                continue
            is_prelim = pv is not None
            if is_prelim:
                fire, stake, mpct, mlpct = pv["fire"], pv["stake"], pv["mpct"], pv["mlpct"]
                row_cls = ("team-top" if is_home else "game-end") + (" bet" if fire and mpct is not None else "")
                snap_tag = "prelim odds"
                prelim_tag = " <span class=\"tag\">(prelim)</span>"
            else:
                snap_tag, prelim_tag = snap, ""
            mlpct = float(mlpct)
            # Market line: the actual captured/live moneyline (stored with the frozen signal, or the live DraftKings
            # line for a preview row); only if neither is known is it backed out of Market %. Model line: implied
            # by the model's own win probability.
            market_ml = pv["ml"] if is_prelim else moneylines.get((game_id, team))
            if market_ml is None:
                market_ml = ml_from_prob(mlpct)
            if mpct is None:  # preview with market line only (lineup/goalie not captured yet)
                model_str, delta_str = "-", "-"
                delta_color = "#666"
            else:
                mpct = float(mpct)
                delta = mpct - mlpct
                delta_color = "#1a7f37" if delta > 0 else ("#c0392b" if delta < 0 else "#666")
                model_str, delta_str = f"{mpct:.1%}", f"{delta:+.1%}"
            model_ml = ml_from_prob(mpct) if mpct is not None else None
            if is_prelim and mpct is None:
                bet_html = "<span class=\"tag\">-</span>"
            else:
                bet_str = "BET" if fire else "no"
                bet_color = "#1a7f37" if fire else "#666"
                bet_html = f"<span style=\"color:{bet_color}; font-weight:600;\">{bet_str}</span>{prelim_tag}"
            # stake is a FRACTION of bankroll (bet_signals.stake_fraction) -> dollars off the in-year bank
            stake_str = (f"${float(stake) * bankroll:,.0f}"
                         if fire and stake is not None and bankroll is not None else "-")
            html += (
                f"<tr class=\"{row_cls}\">{lead_td}"
                f"<td>{team.upper()} {'(H)' if is_home else '(A)'} "
                f"<span class=\"tag\">({snap_tag})</span></td>"
                f"<td>{bet_html}</td>"
                f"<td>{stake_str}</td>"
                f"<td>{model_str}{ml_sub(model_ml, 'Model-implied moneyline')}</td>"
                f"<td>{mlpct:.1%}{ml_sub(market_ml, 'Actual moneyline')}</td>"
                f"<td style=\"color:{delta_color}; font-weight:600;\">{delta_str}</td>"
                f"{goalie_td}"
                f"{check_td}"
                "</tr>"
            )
    return top_note + (
        "<div class=\"scroll-wrap\"><table class=\"today\"><tr><th>Matchup</th><th>Team</th><th>Bet?</th>"
        "<th>$ Amount</th><th>Model %</th><th>Market %</th><th>Delta</th><th>Goalie</th><th>Skater check</th></tr>"
        + html + "</table></div>"
    )


def team_label(code, model_pct, mktpct, fired):
    """Bold if the model's own win% for this side beats the market's implied win% for this side
    (model_pct > mktpct) -- NOT a >50% threshold. Bold+blue if a bet actually fired on this side. Since
    each side's comparison is independent (mktpct for both sides need not sum to 1, thanks to vig), it's
    possible for both sides, one, or neither to be bold."""
    if fired:
        style = "font-weight:700; color:#1450c9;"
    elif model_pct is not None and mktpct is not None and model_pct > mktpct:
        style = "font-weight:700;"
    else:
        style = ""
    return f"<span style=\"{style}\">{code.upper()}</span>"


def game_row_html(pair):
    away, home = pair["away"], pair["home"]
    away_label = team_label(away["team"], away["model_pct"], away["mlpct"], away["bets_fire"])
    home_label = team_label(home["team"], home["model_pct"], home["mlpct"], home["bets_fire"])
    a_mpct = f"{away['model_pct']:.1%}" if away["model_pct"] is not None else "-"
    h_mpct = f"{home['model_pct']:.1%}" if home["model_pct"] is not None else "-"
    a_mktpct = f"{away['mlpct']:.1%}" if away["mlpct"] is not None else "-"
    h_mktpct = f"{home['mlpct']:.1%}" if home["mlpct"] is not None else "-"
    if away["away_goals"] is not None and away["home_goals"] is not None:
        score = f"{away['away_goals']}-{away['home_goals']}"
    else:
        score = "-"
    wagers = []
    for side in (away, home):
        if side["bets_fire"]:
            result_str = "WON" if side["win"] else "lost"
            result_color = "#1a7f37" if side["win"] else "#c0392b"
            wagers.append(f"{side['team'].upper()} ${side['stake_dollar']:,.0f} &mdash; "
                          f"<span style=\"color:{result_color}; font-weight:600;\">{result_str}</span>")
    wager_html = "; ".join(wagers) if wagers else "<span class=\"tag\">-</span>"
    return (
        "<tr>"
        f"<td>{pair['date']}</td>"
        f"<td>{away_label} @ {home_label}</td>"
        f"<td>{a_mpct} / {h_mpct}</td>"
        f"<td>{a_mktpct} / {h_mktpct}</td>"
        f"<td>{score}</td>"
        f"<td>{wager_html}</td>"
        "</tr>"
    )


def games_table_html(pairs):
    if not pairs:
        return "<p class=\"meta\">None.</p>"
    rows = "".join(game_row_html(p) for p in pairs)
    return (
        "<div class=\"scroll-wrap\">"
        "<table class=\"detail\"><tr><th>Date</th><th>Matchup (Away @ Home)</th>"
        "<th>Model % (A/H)</th><th>Market % (A/H)</th><th>Score (A-H)</th><th>Wager &amp; Result</th></tr>"
        f"{rows}</table></div>"
    )


ROW_GRID_COLS = "1.3fr 0.6fr 0.9fr 0.9fr 0.8fr 0.7fr 0.6fr 0.6fr 0.6fr 0.6fr 0.6fr 0.6fr"
# Season tables add a Wallet Return column (a bankroll concept, so it has no week/team equivalent).
ROW_GRID_COLS_WALLET = "1.3fr 0.6fr 0.9fr 0.9fr 0.8fr 0.7fr 0.8fr 0.6fr 0.6fr 0.6fr 0.6fr 0.6fr 0.6fr"
ROW_GRID_MIN_W = {False: 820, True: 1000}


def group_header_row(first_label="", wallet=False):
    """The shared, non-collapsible two-tier header sitting above a week/team/season block list -- printed once,
    with each row (see grid_row_html) using the same grid so it lines up."""
    cols = ROW_GRID_COLS_WALLET if wallet else ROW_GRID_COLS
    min_w = f"min-width:{ROW_GRID_MIN_W[wallet]}px;" if wallet else ""  # non-wallet width is the .row-grid CSS default
    n_blank = 6 + (1 if wallet else 0)
    return (
        f"<div class=\"row-grid group-head\" style=\"grid-template-columns:{cols};{min_w}\">"
        + "<div></div>" * n_blank +
        "<div class=\"group-label\" style=\"grid-column: span 3;\">LogLoss &mdash; Bets Placed</div>"
        "<div class=\"group-label\" style=\"grid-column: span 3;\">LogLoss &mdash; All Games</div>"
        "</div>"
        f"<div class=\"row-grid col-head\" style=\"grid-template-columns:{cols};{min_w}\">"
        f"<div>{first_label}</div><div>Bets</div><div>Record</div><div>Wagered</div><div>Profit</div><div>Return</div>"
        + ("<div>Wallet Return</div>" if wallet else "") +
        "<div>Model</div><div>Market</div><div>&Delta;</div>"
        "<div>Model</div><div>Market</div><div>&Delta;</div>"
        "</div>"
    )


def ll_cells(ll, vll):
    delta = (ll - vll) if (ll is not None and vll is not None) else None
    return (
        f"<div>{f'{ll:.3f}' if ll is not None else '-'}</div>"
        f"<div>{f'{vll:.3f}' if vll is not None else '-'}</div>"
        f"<div style=\"{logloss_delta_bg(delta)}\">{f'{delta:+.3f}' if delta is not None else '-'}</div>"
    )


def grid_row_html(label, s, wallet_pct=None, wallet=False, bold=False, pct_in_record=False):
    """One summary row in the shared grid -- used by the week/team/yesterday blocks and by the season tables, so
    all of them share one set of font sizes, spacing and column order."""
    win_pct = f"{s['wins']}-{s['losses']}" if s["bets_placed"] else "0-0"
    if pct_in_record and s["bets_placed"]:
        win_pct += f" ({s['wins'] / s['bets_placed']:.0%})"
    profit_color = "#1a7f37" if s["profit"] >= 0 else "#c0392b"
    return_str = f"{s['return_pct']:+.1%}" if s["return_pct"] is not None else "-"
    wallet_cell = ""
    if wallet:
        if wallet_pct is not None:
            wr_color = "#1a7f37" if wallet_pct >= 0 else "#c0392b"
            wallet_cell = f"<div style=\"color:{wr_color}; font-weight:600;\">{wallet_pct:+.1%}</div>"
        else:
            wallet_cell = "<div>-</div>"
    cols = ROW_GRID_COLS_WALLET if wallet else ROW_GRID_COLS
    extra = f"min-width:{ROW_GRID_MIN_W[wallet]}px;" if wallet else ""
    if bold:
        extra += " font-weight:700; border-top:2px solid #ccc;"
    return (
        f"<div class=\"row-grid\" style=\"grid-template-columns:{cols};{extra}\">"
        f"<div class=\"block-title\">{label}</div>"
        f"<div>{s['bets_placed']}</div><div>{win_pct}</div>"
        f"<div>${s['wagered']:,.0f}</div>"
        f"<div style=\"color:{profit_color}; font-weight:600;\">{money(s['profit'])}</div>"
        f"<div>{return_str}</div>"
        f"{wallet_cell}"
        f"{ll_cells(s['bet_model_logloss'], s['bet_market_logloss'])}"
        f"{ll_cells(s['all_model_logloss'], s['all_market_logloss'])}"
        "</div>"
    )


def season_grid_html(first_label, rows):
    """Season-level table (the at-a-glance row and the past-seasons list) in the same grid as the week/team
    blocks. rows: [(label, summary, wallet_return_pct, bold)]."""
    body = "".join(grid_row_html(lbl, s, wallet_pct=wr, wallet=True, bold=bold, pct_in_record=True)
                   for lbl, s, wr, bold in rows)
    return (f"<div class=\"scroll-wrap\">{group_header_row(first_label, wallet=True)}"
            f"<div class=\"block\">{body}</div></div>")


def render_group_block(label, instances, pairs, open_all=False):
    s = aggregate(instances)
    summary_row = grid_row_html(label, s)
    bet_pairs = [p for p in pairs if p["away"]["bets_fire"] or p["home"]["bets_fire"]]
    op = " open" if open_all else ""
    return (
        f"<details class=\"block\"{op}><summary>" + summary_row + "</summary>"
        f"<details class=\"nested\"{op}><summary>Bets placed ({len(bet_pairs)})</summary>"
        f"{games_table_html(bet_pairs)}</details>"
        f"<details class=\"nested\"{op}><summary>All games ({len(pairs)})</summary>"
        f"{games_table_html(pairs)}</details>"
        "</details>"
    )


def render_nested_drilldown(groups_sorted, label_fn, all_pairs_by_game_id, open_all=False):
    """groups_sorted: list of (key, instances) already in display order. all_pairs_by_game_id maps
    game_id -> pair (see pair_by_game), used to pull each group's games without re-pairing repeatedly."""
    if not groups_sorted:
        return ""
    blocks = ""
    for key, instances in groups_sorted:
        game_ids_in_group = {x["game_id"] for x in instances}
        pairs = sorted((all_pairs_by_game_id[gid] for gid in game_ids_in_group), key=lambda p: (p["date"], p["game_id"]))
        blocks += render_group_block(label_fn(key), instances, pairs, open_all)
    return f"<div class=\"scroll-wrap\">{group_header_row()}{blocks}</div>"


def build_season_parts(season, instances, bank, season_start, season_end, wallet_returns):
    """The three season-scoped views (Season summary, By week, By team) for ONE season, as html strings.
    instances must already be priced off that season's own in-year bank (see rebank). Returns
    (parts, pairs_by_game_id); the current season's parts go straight into index.html, every other season's are
    written to docs/seasons/<season>.json and loaded by the season picker."""
    disp = f"{season[:2]}-{season[2:]}"
    pairs = pair_by_game(instances)
    pairs_by_game_id = {p["game_id"]: p for p in pairs}
    week_groups = sorted(group_by(instances, lambda x: calendar_week(x["date"], season_start)).items(),
                         key=lambda kv: kv[0], reverse=True)
    team_groups = sorted(group_by(instances, lambda x: x["team"]).items(), key=lambda kv: kv[0])
    week_html = render_nested_drilldown(week_groups, lambda k: f"Week {k}", pairs_by_game_id)
    team_html = render_nested_drilldown(team_groups, lambda k: k.upper(), pairs_by_game_id)
    glance = season_grid_html("Season", [(disp, aggregate(instances),
                                          wallet_returns.get(season, {}).get("wallet_return_pct"), False)])
    chart_svg = render_wallet_chart_svg(bank["days"], cumulative_avg_ll_delta_series(instances), bank["start"],
                                        season_start, season_end, day_tips=build_day_tips(bank["days"], instances))
    none = "<p class=\"meta\">No settled games yet this season.</p>"
    parts = {
        "summary": (f"<h2>{disp} at a glance</h2>{glance}"
                    f"<h2>{disp} &mdash; wallet &amp; LogLoss over time</h2>{chart_svg}"),
        "week": f"<h2>{disp} &mdash; by week</h2>{week_html or none}",
        "team": f"<h2>{disp} &mdash; by team</h2>{team_html or none}",
    }
    return parts, pairs_by_game_id


def render_html(bankroll, todays_rows, season_options, default_season, default_parts, season_summaries,
                wallet_returns, total_summary, skater_data=None, callout_html="", goalie_data=None,
                yesterday_html="", yesterday_short="", start_times=None, preview=None, odds_asof=None,
                moneylines=None):
    # Past seasons: one row per configured season plus a Total row. Total LogLoss is pooled over every instance
    # (total_summary), not an average of the per-season averages, so a short season does not count as much as a
    # full one. Total wallet return % = total dollar wallet return / total starting bankroll across seasons.
    season_rows = []
    total_wallet_return_dollar = 0.0
    total_starting_bankroll = 0.0
    for season, s in season_summaries:
        disp = f"{season[:2]}-{season[2:]}"
        season_wr = wallet_returns.get(season, {})
        season_rows.append((disp, s, season_wr.get("wallet_return_pct"), False))
        if season_wr.get("wallet_return") is not None:
            total_wallet_return_dollar += season_wr["wallet_return"]
            total_starting_bankroll += season_wr["starting_bankroll"]
    if season_summaries and total_summary is not None:
        total_wr_pct = (total_wallet_return_dollar / total_starting_bankroll) if total_starting_bankroll else None
        season_rows.append(("Total", total_summary, total_wr_pct, True))

    tab_defs = [("today", "Today", ""), ("yesterday", "Yesterday", yesterday_short),
                ("summary", "Season summary", ""), ("week", "By week", ""), ("team", "By team", ""),
                ("past", "Past seasons", "")]
    tabs_html = ""
    for tid, label, sub in tab_defs:
        sub_html = "<span class=\"sub\">" + sub + "</span>" if sub else ""
        selected = "true" if tid == "today" else "false"
        tabs_html += (f"<button class=\"tab\" role=\"tab\" data-tab=\"{tid}\" aria-selected=\"{selected}\">"
                      f"{label}{sub_html}</button>")
    options_html = "".join(
        f"<option value=\"{code}\"{' selected' if code == default_season else ''}>{code[:2]}-{code[2:]}</option>"
        for code in season_options)
    picker_html = (
        "<label class=\"season-pick\" id=\"season-pick\" hidden>Season "
        f"<select id=\"season-select\" data-default=\"{default_season}\">{options_html}</select></label>"
        if len(season_options) > 1 else "")

    def season_panel(tab_id, part):
        return (f"<section class=\"panel\" id=\"panel-{tab_id}\" role=\"tabpanel\">"
                f"<div class=\"season-content\" data-part=\"{part}\">{default_parts[part]}</div></section>")

    yesterday_h2 = f"Yesterday's games{f' ({yesterday_short})' if yesterday_short else ''}"
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>NHL Model Dashboard</title>"
        "<script>document.documentElement.className+=' js'</script>"
        f"<style>{STYLE}</style></head><body>"
        "<header class=\"top\"><h1>NHL Model Dashboard</h1>"
        f"<span class=\"stamp\">Page rendered {fmt_clock(datetime.now(timezone.utc))} ET</span></header>"
        f"{callout_html}"
        f"<nav class=\"tabbar\"><div class=\"tabs\" role=\"tablist\">{tabs_html}</div>{picker_html}</nav>"
        "<section class=\"panel active\" id=\"panel-today\" role=\"tabpanel\">"
        "<h2>Today's games</h2>"
        f"{render_todays_games(todays_rows, skater_data, goalie_data, bankroll, start_times, preview, odds_asof, moneylines)}"
        "</section>"
        "<section class=\"panel\" id=\"panel-yesterday\" role=\"tabpanel\">"
        f"<h2>{yesterday_h2}</h2>{yesterday_html}</section>"
        f"{season_panel('summary', 'summary')}"
        f"{season_panel('week', 'week')}"
        f"{season_panel('team', 'team')}"
        "<section class=\"panel\" id=\"panel-past\" role=\"tabpanel\">"
        f"<h2>Past seasons</h2>{season_grid_html('Season', season_rows)}</section>"
        "<div id=\"day-tip\" class=\"day-tip\"></div>"
        f"<script>{SITE_JS}</script>"
        "</body></html>"
    )


def write_if_changed(path, text):
    """Write only when the content differs, so unchanged historical-season files never show up as a git change."""
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


def main():
    conn = get_db_conn()
    try:
        starts = season_start_dates(conn)

        # In-year bank for the current season (starts at season_config.starting_bankroll, $5k for 2026-27).
        # Drives today's $ stakes, the wallet chart, and the current season's dollar figures.
        current_season = get_current_season(conn)
        season_bank = get_season_bank(conn, current_season) if current_season else None
        bankroll = season_bank["current"] if season_bank else None
        bank_overrides = {current_season: season_bank["start_by_date"]} if season_bank else {}

        todays_rows = get_todays_games(conn)
        print(f"Today's games: {len(todays_rows)}")
        start_times, preview, odds_asof = get_todays_schedule_and_preview(conn, todays_rows)
        # Earliest puck drop first (game_id order otherwise); games with no known time keep game_id order at the end.
        _far = datetime.max.replace(tzinfo=timezone.utc)
        todays_rows = sorted(todays_rows, key=lambda r: (start_times.get(r[0], _far), r[0]))
        moneylines = get_todays_moneylines(conn, [r[0] for r in todays_rows])
        skater_data = get_skater_check_data(conn, [r[0] for r in todays_rows])
        goalie_data = get_goalie_data(conn, [r[0] for r in todays_rows])
        callout_html = (render_preliminary_signal_callout(todays_rows)
                        + render_attention_callout(*get_attention_callout(conn)))

        detail_season = get_detail_season(conn)
        print(f"Detail season: {detail_season}")

        all_seasons = get_all_seasons(conn)
        all_instances = get_instances(conn, all_seasons, bank_overrides)
        by_season = group_by(all_instances, lambda x: x["season"])
        wallet_returns = get_season_wallet_returns(conn)
        # Every configured season gets a Past-seasons row, even one with zero games played (2627 before opening
        # night) -- by_season only has keys for seasons that actually returned instances.
        season_summaries = [(s, aggregate(by_season.get(s, []))) for s in sorted(all_seasons)]
        total_summary = aggregate(all_instances)

        def season_view(season):
            """(parts, pairs_by_game_id, instances) for one season, always priced off its own in-year bank."""
            bank = season_bank if season == current_season else get_season_bank(conn, season)
            instances = by_season.get(season, []) if season == current_season \
                else rebank(by_season.get(season, []), bank["start_by_date"])
            end = get_season_end_date(conn, season, starts[season])
            parts, pbg = build_season_parts(season, instances, bank, starts[season], end, wallet_returns)
            return parts, pbg, instances

        # The default view (inline in index.html) plus one json file per other season with games, which the season
        # picker loads on demand. Historical seasons are static, so their files rarely change (and are only written
        # when they do).
        season_options = sorted({s for s in all_seasons if by_season.get(s)} | ({detail_season} if detail_season else set()),
                                reverse=True)
        default_season = detail_season if detail_season in season_options else (season_options[0] if season_options else None)
        none = "<p class=\"meta\">No settled games yet this season.</p>"
        default_parts = {"summary": none, "week": none, "team": none}
        pairs_by_game_id, detail_instances = {}, []
        for season in season_options:
            parts, pbg, instances = season_view(season)
            if season == default_season:
                default_parts, pairs_by_game_id, detail_instances = parts, pbg, instances
            else:
                changed = write_if_changed(f"{SEASONS_DIR}/{season}.json",
                                           json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
                print(f"Season {season}: {'written' if changed else 'unchanged'}")

        # Yesterday (ET): same block as a week row in "by week" -- summary line plus Bets placed / All games --
        # shown expanded.
        yesterday = et_today() - timedelta(days=1)
        yesterday_short = f"{yesterday.strftime('%b')} {yesterday.day}"
        yesterday_instances = [x for x in detail_instances if x["date"] == yesterday]
        if yesterday_instances:
            yesterday_html = render_nested_drilldown([(yesterday, yesterday_instances)], lambda k: yesterday_short,
                                                     pairs_by_game_id, open_all=True)
        else:
            yesterday_html = "<p class=\"meta\">No settled games for yesterday.</p>"
    finally:
        conn.close()

    html = render_html(bankroll, todays_rows, season_options, default_season, default_parts, season_summaries,
                       wallet_returns, total_summary, skater_data, callout_html, goalie_data, yesterday_html,
                       yesterday_short, start_times, preview, odds_asof, moneylines)
    write_if_changed(OUTPUT_PATH, html)
    print(f"Dashboard written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
