"""
nhl_odds_ingest.py -- captures DraftKings moneylines close to puck drop (the
"closing line"), with a same-day fallback for any game the near-game capture
missed. Meant to run every 10 minutes via GitHub Actions (odds-ingest.yml).

WHY TWO PATHS (matches the `odds.captured_via` check constraint: 't5_scan' or
'day_after_fallback')
--------------------------------------------------------------------------
- t5_scan: the primary capture. Each run looks at every scheduled game
  starting within CAPTURE_WINDOW_MINUTES_BEFORE of now (default 60) through
  CAPTURE_WINDOW_MINUTES_AFTER after its scheduled start (default 30, in case
  the game actually dropped late or this run itself is a few minutes late)
  that has no odds row yet, and captures it. The 60-minute-before-only window
  is deliberate: odds firm up close to puck drop, and Nick's notes call the
  odds design "closing-lines-only" -- capturing hours early would not be a
  closing line. ASSUMPTION, not yet confirmed with Nick: without the planned
  Cloudflare Worker (not built), GitHub Actions cron is the only trigger, and
  cron timing is unreliable, so "as close to puck drop as the schedule allows"
  is the best this can do. If a tighter or looser window is wanted, change
  CAPTURE_WINDOW_MINUTES_BEFORE/AFTER below.
- day_after_fallback: a backstop. Any PLAYED game (has a final score) from
  the last DAY_AFTER_LOOKBACK_DAYS days that still has no odds row at all --
  because every t5_scan run missed its window, e.g. cron didn't fire --
  gets whatever line ESPN still has for that event, tagged separately so it's
  visible in the data that it is not a genuine closing-line capture.

SOURCES
--------------------------------------------------------------------------
- Game list + start times + ESPN event/competition id: site.web.api.espn.com
  scoreboard. This is NOT the same host as the blocked site.api.espn.com
  scoreboard; both were checked from GitHub Actions and only the former
  answers (see chat: nhl_check_sources.py / nhl_diagnose_espn_event_lookup.py).
- Odds: sports.core.api.espn.com's per-event odds endpoint (DraftKings only,
  matching nhlodds.py's existing TARGET_PROVIDER convention). Confirmed
  reachable from GitHub in the same check.

VIG
--------------------------------------------------------------------------
American-odds implied probability: positive ml -> 100/(ml+100); negative ml
-> -ml/(-ml+100). vig_pct = home_implied + away_implied - 1. Thresholds from
the schema note in the project overview: <=5% ok, 5-6% prelim, >=6% invalid.

TARGET_GAME_ID (T-5 dispatch path)
--------------------------------------------------------------------------
When the Cloudflare Worker fires a game's T-5 alarm, the dispatched GitHub
Actions workflow sets TARGET_GAME_ID to that one game_id. In that mode this
script skips the window scan and day_after_fallback entirely and captures
just that one game directly -- it's already known to be exactly at T-5, so
there's no reason to re-derive that from a time window. Still tagged
captured_via='t5_scan' (it is one, just triggered precisely instead of by
a cron sweep) and still goes through the same never-overwrite insert.

SAFETY
--------------------------------------------------------------------------
- DRY_RUN defaults to true.
- Never overwrites an existing odds row (PK is game_id, team_code, and this
  script always checks before writing) -- a t5_scan capture is never
  replaced by a later t5_scan or by the day_after_fallback pass.
- Playoff games are skipped for now (schedule is regular-season only).
"""

import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import execute_values
import requests

DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
TARGET_GAME_ID = os.environ.get("TARGET_GAME_ID", "").strip() or None
CAPTURE_WINDOW_MINUTES_BEFORE = int(os.environ.get("CAPTURE_WINDOW_MINUTES_BEFORE", "60"))
CAPTURE_WINDOW_MINUTES_AFTER = int(os.environ.get("CAPTURE_WINDOW_MINUTES_AFTER", "30"))
DAY_AFTER_LOOKBACK_DAYS = int(os.environ.get("DAY_AFTER_LOOKBACK_DAYS", "3"))
VIG_PRELIM_PCT = 0.05
VIG_INVALID_PCT = 0.06

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
SCOREBOARD_URL = "https://site.web.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
ODDS_URL_TMPL = "https://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl/events/{event_id}/competitions/{comp_id}/odds"
TARGET_PROVIDER = "draftkings"
REQUEST_DELAY_SECONDS = 0.3

# Feed abbreviation (lower case) -> our team_code. Kept identical to
# nhl_daily_results_ingest.py's FEED_TO_INTERNAL; only VGK differs from ours.
FEED_TO_INTERNAL = {"tb": "tbl", "sj": "sjs", "nj": "njd", "la": "lak", "vgk": "veg", "was": "wsh", "utah": "uta"}


def normalize_team_code(code):
    return FEED_TO_INTERNAL.get(code, code)


def get_db_conn():
    if not os.environ.get("PGHOST"):
        raise RuntimeError("Set PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT.")
    try:
        return psycopg2.connect(connect_timeout=20)
    except psycopg2.OperationalError as e:
        print(f"DATABASE CONNECTION FAILED: {e}")
        raise


def fetch_scoreboard(d):
    r = requests.get(SCOREBOARD_URL, params={"dates": d.strftime("%Y%m%d")}, headers=HEADERS, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Scoreboard returned HTTP {r.status_code} for {d}. First 200 chars: {r.text[:200]!r}")
    js = r.json()
    out = []
    for ev in js.get("events", []) or []:
        comp = (ev.get("competitions") or [{}])[0]
        comps = comp.get("competitors") or []
        home = next((c for c in comps if c.get("homeAway") == "home"), {})
        away = next((c for c in comps if c.get("homeAway") == "away"), {})
        st = (ev.get("status") or {}).get("type") or {}
        start = ev.get("date")
        try:
            start_dt = datetime.strptime(start, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            start_dt = None
        out.append({
            "event_id": ev.get("id"), "comp_id": comp.get("id"), "start": start_dt,
            "home": normalize_team_code(str((home.get("team") or {}).get("abbreviation", "")).lower()),
            "away": normalize_team_code(str((away.get("team") or {}).get("abbreviation", "")).lower()),
            "final": bool(st.get("completed")),
            "season_type": (ev.get("season") or {}).get("type"),
        })
    return out


def fetch_draftkings_moneylines(event_id, comp_id):
    r = requests.get(ODDS_URL_TMPL.format(event_id=event_id, comp_id=comp_id), headers=HEADERS, timeout=20)
    if r.status_code != 200:
        return None, None
    try:
        items = r.json().get("items") or []
    except Exception:  # noqa: BLE001
        return None, None
    for it in items:
        provider = ((it.get("provider") or {}).get("name") or "").strip().lower().replace(" ", "")
        if provider == TARGET_PROVIDER:
            home_ml = (it.get("homeTeamOdds") or {}).get("moneyLine")
            away_ml = (it.get("awayTeamOdds") or {}).get("moneyLine")
            return home_ml, away_ml
    return None, None


def implied_prob(ml):
    if ml is None:
        return None
    ml = float(ml)
    return 100.0 / (ml + 100.0) if ml > 0 else -ml / (-ml + 100.0)


def vig_from_moneylines(home_ml, away_ml):
    """Returns (vig_pct, vig_flag) or (None, None) if either line is missing."""
    hp, ap = implied_prob(home_ml), implied_prob(away_ml)
    if hp is None or ap is None:
        return None, None
    vig = hp + ap - 1.0
    flag = "invalid" if vig >= VIG_INVALID_PCT else "prelim" if vig >= VIG_PRELIM_PCT else "ok"
    return vig, flag


def game_ids_with_odds(conn, game_ids):
    if not game_ids:
        return set()
    with conn.cursor() as cur:
        cur.execute("select distinct game_id from odds where game_id = any(%s)", (list(game_ids),))
        return {r[0] for r in cur.fetchall()}


def resolve_game_id(conn, d, home, away):
    with conn.cursor() as cur:
        cur.execute("select game_id from games where playoff = false and date = %s and home_team = %s "
                    "and away_team = %s", (d, home, away))
        row = cur.fetchone()
        return row[0] if row else None


def game_by_id(conn, game_id):
    """-> (date, home_team, away_team) or None. Used by the TARGET_GAME_ID fast path."""
    with conn.cursor() as cur:
        cur.execute("select date, home_team, away_team from games where game_id = %s and playoff = false",
                     (game_id,))
        return cur.fetchone()


def capture_event(conn, game_id, home, away, event_id, comp_id, captured_via, rows):
    home_ml, away_ml = fetch_draftkings_moneylines(event_id, comp_id)
    if home_ml is None and away_ml is None:
        print(f"  {away} @ {home} ({game_id}): no DraftKings line available yet")
        return False
    vig, flag = vig_from_moneylines(home_ml, away_ml)
    now = datetime.now(timezone.utc)
    if home_ml is not None:
        rows.append((game_id, home, home_ml, "DraftKings", vig, flag, captured_via, now))
    if away_ml is not None:
        rows.append((game_id, away, away_ml, "DraftKings", vig, flag, captured_via, now))
    print(f"  {away} @ {home} ({game_id}): home_ml={home_ml} away_ml={away_ml} vig={vig and round(vig, 4)} "
          f"flag={flag} via={captured_via}")
    return True


def t5_scan(conn, now, rows):
    print("== t5_scan ==")
    lo = now - timedelta(minutes=CAPTURE_WINDOW_MINUTES_AFTER)
    hi = now + timedelta(minutes=CAPTURE_WINDOW_MINUTES_BEFORE)
    dates = sorted({now.date(), (now + timedelta(days=1)).date()})
    candidates = []
    for d in dates:
        for e in fetch_scoreboard(d):
            if e["season_type"] != 2 or e["start"] is None:
                continue
            if not (lo <= e["start"] <= hi):
                continue
            candidates.append((d, e))
        time.sleep(REQUEST_DELAY_SECONDS)
    if not candidates:
        print(f"  no regular-season games starting within [{lo.isoformat()}, {hi.isoformat()}]")
        return
    game_ids = {}
    for d, e in candidates:
        gid = resolve_game_id(conn, d, e["home"], e["away"])
        if gid is None:
            print(f"  {e['away']} @ {e['home']} on {d}: not found in the schedule table")
            continue
        game_ids[(d, e["home"], e["away"])] = gid
    have = game_ids_with_odds(conn, list(game_ids.values()))
    for d, e in candidates:
        gid = game_ids.get((d, e["home"], e["away"]))
        if gid is None or gid in have:
            continue
        capture_event(conn, gid, e["home"], e["away"], e["event_id"], e["comp_id"], "t5_scan", rows)
        time.sleep(REQUEST_DELAY_SECONDS)


def capture_single_game(conn, game_id, rows):
    """T-5 dispatch fast path -- capture exactly one game, already known to be at T-5, skipping the
    window scan entirely. See TARGET_GAME_ID in the module docstring."""
    print(f"== single-game capture (TARGET_GAME_ID={game_id}) ==")
    info = game_by_id(conn, game_id)
    if info is None:
        print(f"  {game_id}: not found in games table (or is a playoff game) -- nothing to do")
        return
    d, home, away = info
    if game_ids_with_odds(conn, [game_id]):
        print(f"  {game_id}: odds already captured -- nothing to do")
        return
    events = {(e["home"], e["away"]): e for e in fetch_scoreboard(d)}
    e = events.get((home, away))
    if e is None:
        print(f"  {away} @ {home} ({game_id}, {d}): not found on ESPN's scoreboard for that date")
        return
    capture_event(conn, game_id, home, away, e["event_id"], e["comp_id"], "t5_scan", rows)


def day_after_fallback(conn, today, rows):
    print("== day_after_fallback ==")
    with conn.cursor() as cur:
        cur.execute(
            "select game_id, date, home_team, away_team from games where playoff = false "
            "and home_goals is not null and date < %s and date >= %s order by date",
            (today, today - timedelta(days=DAY_AFTER_LOOKBACK_DAYS)))
        played = cur.fetchall()
    have = game_ids_with_odds(conn, [r[0] for r in played])
    missing = [r for r in played if r[0] not in have]
    if not missing:
        print(f"  no played games in the last {DAY_AFTER_LOOKBACK_DAYS} day(s) are missing odds")
        return
    by_date = {}
    for gid, d, home, away in missing:
        by_date.setdefault(d, []).append((gid, home, away))
    for d, rows_for_date in by_date.items():
        events = {(e["home"], e["away"]): e for e in fetch_scoreboard(d)}
        time.sleep(REQUEST_DELAY_SECONDS)
        for gid, home, away in rows_for_date:
            e = events.get((home, away))
            if e is None:
                print(f"  {away} @ {home} ({gid}, {d}): not found on ESPN's scoreboard for that date")
                continue
            capture_event(conn, gid, home, away, e["event_id"], e["comp_id"], "day_after_fallback", rows)
            time.sleep(REQUEST_DELAY_SECONDS)


def write_rows(conn_factory, rows):
    if not rows:
        print("Nothing to write.")
        return
    if DRY_RUN:
        print(f"[dry-run] would write {len(rows)} odds row(s):")
        for r in rows:
            print("   ", r)
        return
    conn = conn_factory()
    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                "insert into odds (game_id, team_code, moneyline, sportsbook, vig_pct, vig_flag, captured_via, captured_at) "
                "values %s on conflict (game_id, team_code) do nothing",
                rows)
        conn.commit()
        print(f"[live] wrote {len(rows)} odds row(s).")
    finally:
        conn.close()


def main():
    now = datetime.now(timezone.utc)
    print(f"NHL odds ingest -- DRY_RUN={DRY_RUN} -- TARGET_GAME_ID={TARGET_GAME_ID} -- "
          f"now (UTC) {now.isoformat(timespec='seconds')} -- "
          f"window -{CAPTURE_WINDOW_MINUTES_AFTER}min/+{CAPTURE_WINDOW_MINUTES_BEFORE}min")
    if not os.environ.get("PGHOST"):
        print("No PGHOST set -- nothing to do.")
        return
    conn = get_db_conn()
    rows = []
    try:
        if TARGET_GAME_ID:
            capture_single_game(conn, TARGET_GAME_ID, rows)
        else:
            t5_scan(conn, now, rows)
            day_after_fallback(conn, now.date(), rows)
    finally:
        conn.close()
    write_rows(get_db_conn, rows)
    print("Done.")


if __name__ == "__main__":
    main()
