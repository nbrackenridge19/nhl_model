"""
nhl_t5_scheduler.py -- keeps the Cloudflare Worker's per-game alarms in sync
with the real NHL schedule, so odds/lineups/goalies get captured at exactly
T-5 minutes before puck drop instead of on a loose cron window.

WHAT THIS DOES
--------------------------------------------------------------------------
The `games` table already has every 2026-27 game seeded (game_id, date,
home_team, away_team, home_goals null) -- there is no separate "schedule"
table. What it does NOT have is a start TIME (just a date), so exact puck
drop has to come from ESPN's scoreboard, exactly the way nhl_odds_ingest.py
and nhl_lineup_goalie_ingest.py already resolve game_id from (date, home,
away). This script:

  1. Pulls today's and tomorrow's events from ESPN (site.web.api.espn.com --
     the host that actually answers from GitHub Actions; site.api.espn.com
     is blocked there).
  2. For each regular-season event with a real start time that hasn't
     happened yet, resolves our internal game_id via the same (date, home,
     away) lookup the other T-5 scripts use.
  3. If ESPN shows a matchup on a date our `games` table doesn't have, that game was probably MOVED
     (postponement / reschedule). The schedule row is re-dated to ESPN's date BEFORE puck drop (see
     MOVED GAMES below), so the alarm, the odds capture and the lineup/goalie capture all find it.
  4. POSTs {game_id, start_time_iso} to the Worker's /schedule endpoint.
     The Worker computes T-5 itself and (re)sets that game's Durable Object
     alarm -- calling this again for the same game just resets the alarm to
     whatever start time ESPN now shows, which is exactly what you want if
     a game gets pushed back or moved up.

This runs every 30 minutes (t5-scheduler.yml, kicked by the Cloudflare Worker's cron trigger, with
GitHub's own cron as a backup) -- it's cheap (no scraping, a few small JSON fetches) and idempotent,
so running it often is fine.

MOVED GAMES
--------------------------------------------------------------------------
nhl_daily_results_ingest.py already re-dates a moved game, but only AFTER it has been played. Until then
`games` keeps the old date, so a moved game would get no alarm and the odds/lineup scripts (which look the
game up by its stored date) could not find it. This script now fixes that up front:

  - An ESPN regular-season event with no `games` row on its date triggers a search among this season's
    UNPLAYED rows for the same (home, away) matchup. A row is the moved one ("orphan") if ESPN has no live
    event for that matchup on the row's own date (no event, or one marked postponed/canceled).
    Many matchups appear more than once in the 84-game schedule (352 of them, checked against the real
    table), so this is what tells the moved row apart from the others.
  - Exactly one orphan -> `update games set date = <ESPN date>` for that game_id (only while it is still
    unplayed). Zero or several -> nothing is changed and the reason is printed, so it can be handled by hand.
  - Events ESPN marks postponed/canceled are never scheduled.

SAFETY
------
- DRY_RUN defaults to true -- prints what it would POST instead of POSTing.
- The ONLY thing this script ever writes is `games.date` for a game it has confirmed was moved (see MOVED
  GAMES), and only for a row that is still unplayed. DRY_RUN prints the change instead of making it.
- Skips any event ESPN doesn't have a real start time for yet, and any
  event that already started (Worker alarms firing on already-passed times
  fire almost immediately, which is a harmless catch-up, but there is no
  reason to spend a request scheduling something already underway).
- Skips playoff games (schedule is regular-season only, matching every
  other T-5 script's convention).
"""

import os
import time
from datetime import date, datetime, timedelta, timezone

import psycopg2
import requests

DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
LOOKAHEAD_DAYS = int(os.environ.get("LOOKAHEAD_DAYS", "3"))  # today + this many days ahead
REQUEST_DELAY_SECONDS = 0.3
REQUEST_TIMEOUT = 20

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
SCOREBOARD_URL = "https://site.web.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"

# ESPN event statuses meaning "this game is not happening on this date".
DEAD_STATUSES = {"STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_RESCHEDULED"}

WORKER_URL = os.environ.get("WORKER_URL", "").rstrip("/")
SCHEDULE_SECRET = os.environ.get("SCHEDULE_SECRET", "")

# Feed abbreviation (lower case) -> our team_code. Kept identical to
# nhl_odds_ingest.py's / nhl_daily_results_ingest.py's FEED_TO_INTERNAL.
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
    r = requests.get(SCOREBOARD_URL, params={"dates": d.strftime("%Y%m%d")}, headers=HEADERS, timeout=REQUEST_TIMEOUT)
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
            "start": start_dt,
            "started": bool(st.get("state") in ("in", "post")),
            "status_name": st.get("name"),
            "home": normalize_team_code(str((home.get("team") or {}).get("abbreviation", "")).lower()),
            "away": normalize_team_code(str((away.get("team") or {}).get("abbreviation", "")).lower()),
            "season_type": (ev.get("season") or {}).get("type"),
        })
    return out


def resolve_game_id(conn, d, home, away):
    with conn.cursor() as cur:
        cur.execute("select game_id from games where playoff = false and date = %s and home_team = %s "
                    "and away_team = %s", (d, home, away))
        row = cur.fetchone()
        return row[0] if row else None


_scoreboard_cache = {}


def scoreboard_cached(d):
    if d not in _scoreboard_cache:
        _scoreboard_cache[d] = fetch_scoreboard(d)
        time.sleep(REQUEST_DELAY_SECONDS)
    return _scoreboard_cache[d]


def latest_season(conn):
    with conn.cursor() as cur:
        cur.execute("select max(season) from games where playoff = false")
        row = cur.fetchone()
        return row[0] if row else None


def unplayed_rows_for_matchup(conn, season, home, away):
    with conn.cursor() as cur:
        cur.execute("select game_id, date from games where playoff = false and season = %s and home_team = %s "
                    "and away_team = %s and home_goals is null order by date", (season, home, away))
        return cur.fetchall()


def find_moved_game(conn, season, home, away):
    """-> (game_id, old_date, None) if exactly one unplayed row for this matchup has no live ESPN event on its
    own date, else (None, None, reason)."""
    rows = unplayed_rows_for_matchup(conn, season, home, away)
    if not rows:
        return None, None, "no unplayed row for this matchup in the schedule"
    orphans = []
    for gid, gdate in rows:
        live = any(e["home"] == home and e["away"] == away and e["status_name"] not in DEAD_STATUSES
                   for e in scoreboard_cached(gdate))
        if not live:
            orphans.append((gid, gdate))
    if len(orphans) == 1:
        return orphans[0][0], orphans[0][1], None
    return None, None, (f"{len(orphans)} of {len(rows)} unplayed rows for this matchup have no live ESPN event on "
                        f"their own date -- can't tell which one moved")


def redate_game(conn, game_id, old_date, new_date):
    if DRY_RUN:
        print(f"  [dry-run] would re-date {game_id}: {old_date} -> {new_date}")
        return True
    with conn.cursor() as cur:
        cur.execute("update games set date = %s where game_id = %s and playoff = false and home_goals is null "
                    "and date = %s", (new_date, game_id, old_date))
        n = cur.rowcount
    conn.commit()
    return n == 1


def schedule_worker_alarm(game_id, start_time_iso):
    """POST {game_id, start_time_iso} to the Worker's /schedule endpoint.
    Returns (ok, detail) -- never raises, so one bad game doesn't stop the rest."""
    url = f"{WORKER_URL}/game/{game_id}/schedule"
    if DRY_RUN:
        print(f"  [dry-run] would POST {url}  body={{'game_id': {game_id!r}, 'start_time_iso': {start_time_iso!r}}}")
        return True, "dry-run"
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {SCHEDULE_SECRET}", "Content-Type": "application/json"},
            json={"game_id": game_id, "start_time_iso": start_time_iso},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"request failed: {e}"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    return True, r.text[:200]


def main():
    now = datetime.now(timezone.utc)
    print(f"NHL T-5 scheduler -- DRY_RUN={DRY_RUN} -- now (UTC) {now.isoformat(timespec='seconds')} -- "
          f"lookahead {LOOKAHEAD_DAYS}d")

    if not os.environ.get("PGHOST"):
        print("No PGHOST set -- nothing to do.")
        return
    if not WORKER_URL:
        print("No WORKER_URL set -- nothing to do.")
        return
    if not SCHEDULE_SECRET and not DRY_RUN:
        print("No SCHEDULE_SECRET set -- nothing to do.")
        return

    conn = get_db_conn()
    try:
        dates = [date.today() + timedelta(days=i) for i in range(LOOKAHEAD_DAYS + 1)]
        events = []
        for d in dates:
            events += [(d, e) for e in fetch_scoreboard(d)]
            time.sleep(REQUEST_DELAY_SECONDS)

        season = latest_season(conn)
        scheduled, skipped, redated = 0, 0, 0
        for d, e in events:
            if e["season_type"] != 2:
                continue  # preseason/playoffs -- regular season only, matching the other T-5 scripts
            if e["start"] is None or e["started"] or e["status_name"] in DEAD_STATUSES:
                skipped += 1
                continue
            game_id = resolve_game_id(conn, d, e["home"], e["away"])
            if game_id is None:
                moved_id, old_date, reason = find_moved_game(conn, season, e["home"], e["away"])
                if moved_id is None:
                    print(f"  {e['away']} @ {e['home']} on {d}: not in games table and not a clear move ({reason}) "
                          f"-- skipping, NEEDS MANUAL CHECK")
                    skipped += 1
                    continue
                print(f"  MOVED: {e['away']} @ {e['home']} ({moved_id}) {old_date} -> {d}")
                if not redate_game(conn, moved_id, old_date, d):
                    print(f"  re-date of {moved_id} changed no row (already played or already moved?) -- skipping")
                    skipped += 1
                    continue
                redated += 1
                game_id = moved_id
            start_iso = e["start"].isoformat().replace("+00:00", "Z")
            ok, detail = schedule_worker_alarm(game_id, start_iso)
            status = "OK" if ok else "FAILED"
            print(f"  {e['away']} @ {e['home']} ({game_id}) start={start_iso}: {status} -- {detail}")
            if ok:
                scheduled += 1
            time.sleep(REQUEST_DELAY_SECONDS)

        print(f"Done. scheduled={scheduled} skipped={skipped} redated={redated}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
