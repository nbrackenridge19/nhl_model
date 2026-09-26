"""
nhl_lineup_goalie_ingest.py -- captures projected lineups and starting goalies
from DailyFaceoff into lineup_snapshots / goalie_snapshots, and queues any
player it can't confidently match into player_id_review_queue instead of
guessing.

This script runs in one of two modes, set by the RUN_MODE env var, each on
its own GitHub Actions schedule:

  RUN_MODE=preliminary  (lineup-goalie-preliminary.yml, every 30 minutes)
    The first time this script sees a (game_id, team_code) with no
    'preliminary' row yet, it writes whatever DailyFaceoff shows, however
    far out that is. Never overwritten by this script.

  RUN_MODE=confirmed  (lineup-goalie-confirmed.yml, once nightly)
    A single end-of-night pass, scheduled for after every one of that day's
    games has started (Nick: "we should be running one, end of night
    dailyfaceoff check once all games have started ... that's the definitive
    proof" -- by then DailyFaceoff's goalie/line pages reflect who is
    actually playing, not a projection). Scheduled for 04:00 UTC, which is
    11pm-12am ET depending on DST -- always at or after 11pm ET, which is
    after every NHL game's start on a normal schedule (the latest starts,
    West Coast games, go off around 10-10:30pm ET). For every game whose
    date is "today" in US/Eastern (computed with zoneinfo, not the runner's
    UTC clock) and has no 'confirmed' row yet, writes whatever DailyFaceoff
    shows as 'confirmed' -- no per-game start-time check, since the whole
    point of the nightly timing is that every game already qualifies.

CONFIRMED ROWS AND POST-GAME CORRECTION
--------------------------------------------------------------------------
- lineup_snapshots' 'confirmed' row IS later overwritten -- but only by
  nhl_daily_results_ingest.py, once it knows who actually played (see that
  script's overwrite_confirmed_snapshots()). That result is definitive, so it
  replaces whatever DailyFaceoff projected: skaters DailyFaceoff listed who
  did not actually dress are removed, and anyone who played but wasn't
  projected (a call-up, a last-second swap) is added. This script itself
  never touches an existing 'confirmed' row.
- goalie_snapshots' 'confirmed' row is NEVER overwritten by the results
  ingest. Nick: a box score cannot reliably tell you who started -- a
  starter who gets pulled can end up with less ice time than the goalie who
  relieves him, and Hockey-Reference's listing order is not a safe proxy
  either. The nightly DailyFaceoff capture (this script, RUN_MODE=confirmed)
  is goalie_snapshots' only and final word, and nhl_daily_results_ingest.py
  reads it back (rather than deriving anything from the box score) for the
  SvPctR feature's starting-goalie identity.
  Side effect worth knowing: v_lineup_fallback_status's "used_fallback" flag
  (no confirmed row before the game) stops being a reliable historical
  record of pre-game LINEUP data quality once the post-game overwrite always
  adds a confirmed row -- it will read false after the fact even for a game
  whose real pre-game 'confirmed' capture failed. (v_goalie_fallback_status
  is unaffected, since goalie_snapshots is never overwritten.) Flagging
  this; not changing the view without a separate decision.

DFO_STATUS COLUMN (goalie_snapshots only)
--------------------------------------------------------------------------
DailyFaceoff's own confidence label (homeNewsStrengthName / away...) for the
starting goalie -- e.g. 'Confirmed' -- is stored verbatim in the
goalie_snapshots.dfo_status column. Informational only.

PLAYER MATCHING (this is the "prompts him" workflow Nick asked for)
--------------------------------------------------------------------------
For every skater DailyFaceoff lists in a team's 12 forwards / 6 defensemen
(the 'ev' category groups f1-f4, d1-d3) and every starting/likely goalie:
  1. Normalize the name (unaccent, drop punctuation) and look it up against
     `players`. Zero or more-than-one match (after trying to break ties by
     position) means "not confidently identified."
  2. If matched, compare DailyFaceoff's team to the player's own most recent
     2025-26 game appearance. A mismatch (a trade DailyFaceoff knows about
     that we don't, or a bad match) is NOT auto-corrected.
  3. Anything from (1) or (2) is written to player_id_review_queue (skipped
     for this snapshot, not guessed at) instead of lineup_snapshots /
     goalie_snapshots. Everyone else on the same page still gets written
     normally -- one flagged player doesn't block a team's snapshot.
Players with 0 GP in 2025-26 who aren't in the DB at all will always queue
here on first sighting, by design -- that is the point of the workflow.

Injured/scratched players (DailyFaceoff's 'oi'/'ir' category) are excluded
entirely, matching "roster boolean" = presence in the table.

SAFETY
--------------------------------------------------------------------------
- DRY_RUN defaults to true.
- Never overwrites an existing snapshot row of either type (this script
  never touches an existing 'preliminary' or 'confirmed' row -- only
  nhl_daily_results_ingest.py's lineup overwrite does, and only for skaters).
- RUN_MODE is required; the script exits with an error rather than guessing
  which pass to run.
- Preliminary looks at games within LOOKAHEAD_DAYS (default 2) and
  regular-season games only (DailyFaceoff doesn't cover preseason --
  confirmed 2026-09-19/20). Confirmed looks only at "today" (US/Eastern).

TARGET_GAME_ID (T-5 dispatch path, RUN_MODE=confirmed only)
--------------------------------------------------------------------------
When the Cloudflare Worker fires a game's T-5 alarm, the dispatched
workflow runs this script with RUN_MODE=confirmed and TARGET_GAME_ID set to
that one game_id -- capturing DailyFaceoff's confirmed lineup/goalie for
just that game, right at its own puck drop, instead of waiting for the
once-nightly 11pm ET sweep. Both passes then restrict themselves to that
one game's two teams rather than looping every team/date. The nightly
unscoped confirmed run (lineup-goalie-confirmed.yml) keeps running
unchanged as a safety net -- since neither pass overwrites an existing
'confirmed' row, a T-5 capture that already ran for a game just means the
11pm sweep finds nothing left to do for it. Ignored (has no effect) when
RUN_MODE=preliminary.
"""

import os
import re
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import execute_values
import requests

DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
LOOKAHEAD_DAYS = int(os.environ.get("LOOKAHEAD_DAYS", "2"))
REQUEST_DELAY_SECONDS = 1.0
RUN_MODE = os.environ.get("RUN_MODE", "").strip().lower()
if RUN_MODE not in ("preliminary", "confirmed"):
    raise SystemExit(f"RUN_MODE must be 'preliminary' or 'confirmed', got {RUN_MODE!r}.")
TARGET_GAME_ID = os.environ.get("TARGET_GAME_ID", "").strip() or None
if TARGET_GAME_ID and RUN_MODE != "confirmed":
    TARGET_GAME_ID = None  # only meaningful for the T-5 confirmed-mode dispatch path -- see module docstring

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
}
DFO_LINES_URL = "https://www.dailyfaceoff.com/teams/{slug}/line-combinations"
DFO_GOALIES_URL = "https://www.dailyfaceoff.com/starting-goalies/{d}"

FEED_TO_INTERNAL = {"tb": "tbl", "sj": "sjs", "nj": "njd", "la": "lak", "vgk": "veg", "was": "wsh", "utah": "uta"}

TEAM_SLUGS = {
    "anaheim-ducks": "ana", "boston-bruins": "bos", "buffalo-sabres": "buf", "calgary-flames": "cgy",
    "carolina-hurricanes": "car", "chicago-blackhawks": "chi", "colorado-avalanche": "col",
    "columbus-blue-jackets": "cbj", "dallas-stars": "dal", "detroit-red-wings": "det",
    "edmonton-oilers": "edm", "florida-panthers": "fla", "los-angeles-kings": "lak", "minnesota-wild": "min",
    "montreal-canadiens": "mtl", "nashville-predators": "nsh", "new-jersey-devils": "njd",
    "new-york-islanders": "nyi", "new-york-rangers": "nyr", "ottawa-senators": "ott",
    "philadelphia-flyers": "phi", "pittsburgh-penguins": "pit", "san-jose-sharks": "sjs",
    "seattle-kraken": "sea", "st-louis-blues": "stl", "tampa-bay-lightning": "tbl",
    "toronto-maple-leafs": "tor", "utah-mammoth": "uta", "vancouver-canucks": "van",
    "vegas-golden-knights": "veg", "washington-capitals": "wsh", "winnipeg-jets": "wpg",
}


def normalize_team_code(code):
    return FEED_TO_INTERNAL.get(code, code)


def normalize_name(s):
    if s is None:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace(".", "").replace("'", "").replace("\u2019", "").replace("-", " ")
    return " ".join(s.split())


def pos_group(group_identifier):
    """DailyFaceoff groupIdentifier ('f1'-'f4','d1'-'d3','g') -> our players.position ('F'/'D'/'G').
    (positionIdentifier uses 'ld'/'rd' for defensemen rather than a plain 'd', so groupIdentifier's
    leading letter is the reliable signal -- confirmed against the real anaheim-ducks page.)"""
    if not group_identifier:
        return None
    g = group_identifier.lower()
    if g == "g":
        return "G"
    if g.startswith("d"):
        return "D"
    if g.startswith("f"):
        return "F"
    return None


def et_today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return (datetime.now(timezone.utc) - timedelta(hours=5)).date()


def get_db_conn():
    if not os.environ.get("PGHOST"):
        raise RuntimeError("Set PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT.")
    try:
        return psycopg2.connect(connect_timeout=20)
    except psycopg2.OperationalError as e:
        print(f"DATABASE CONNECTION FAILED: {e}")
        raise


# --------------------------- DailyFaceoff fetch + parse ---------------------------

def fetch_next_data(url):
    r = requests.get(url, headers=HEADERS, timeout=25)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        return None, "no __NEXT_DATA__ block found"
    import json
    try:
        return json.loads(m.group(1)), None
    except Exception as e:  # noqa: BLE001
        return None, f"__NEXT_DATA__ present but not valid JSON: {e}"


def fetch_team_lines(slug):
    """-> (team_code, [{'name','pos_group','dfo_player_id'}], error_or_None)."""
    data, err = fetch_next_data(DFO_LINES_URL.format(slug=slug))
    if data is None:
        return None, [], err
    comb = (data.get("props") or {}).get("pageProps", {}).get("combinations") or {}
    team_code = normalize_team_code(str(comb.get("teamAbbreviation", "")).lower())
    out = []
    for p in comb.get("players") or []:
        # 'ev' = the even-strength lines (12F + 6D + 2G); excludes 'oi'/'ir' (injured) and 'pp'/'pk' (repeats
        # of the same ev players in special-teams units). Goalies are excluded here too -- the starting goalie
        # comes from the dedicated DailyFaceoff goalie page/endpoint, not from this depth-chart listing.
        if p.get("categoryIdentifier") != "ev" or p.get("groupIdentifier") == "g":
            continue
        out.append({"name": p.get("name"), "dfo_player_id": p.get("playerId"), "pos_group": pos_group(p.get("groupIdentifier"))})
    return team_code, out, None


def fetch_goalies_for_date(d):
    """-> ([{'date','home','away','home_goalie','away_goalie'}], error_or_None). Each goalie dict:
    {'name','dfo_player_id','strength'} or None if the page has no entry."""
    data, err = fetch_next_data(DFO_GOALIES_URL.format(d=d.isoformat()))
    if data is None:
        return [], err
    rows = (data.get("props") or {}).get("pageProps", {}).get("data")
    if not isinstance(rows, list):
        return [], f"unexpected pageProps.data type: {type(rows)}"
    out = []
    for r in rows:
        home_slug, away_slug = r.get("homeTeamSlug"), r.get("awayTeamSlug")
        home_code, away_code = TEAM_SLUGS.get(home_slug), TEAM_SLUGS.get(away_slug)
        if not (home_code and away_code):
            out.append({"date": r.get("date"), "home": None, "away": None, "home_goalie": None, "away_goalie": None,
                        "note": f"unmapped slug(s): home={home_slug!r} away={away_slug!r}"})
            continue

        def goalie(prefix):
            name = r.get(f"{prefix}GoalieName")
            if not name:
                return None
            return {"name": name, "dfo_player_id": r.get(f"{prefix}GoalieId"), "strength": r.get(f"{prefix}NewsStrengthName")}
        out.append({"date": r.get("date"), "home": home_code, "away": away_code,
                    "home_goalie": goalie("home"), "away_goalie": goalie("away")})
    return out, None


# --------------------------- schedule helpers ---------------------------

def upcoming_games_for_team(conn, team_code, today, lookahead_days):
    """Nearest unplayed game for a team within [today, today+lookahead_days] -- used by the preliminary pass."""
    with conn.cursor() as cur:
        cur.execute(
            "select game_id, date, home_team, away_team from games where playoff = false and home_goals is null "
            "and date >= %s and date <= %s and (home_team = %s or away_team = %s) order by date limit 1",
            (today, today + timedelta(days=lookahead_days), team_code, team_code))
        return cur.fetchone()


def game_today_for_team(conn, team_code, today):
    """This team's game today specifically (or None if it isn't playing today) -- used by the confirmed pass."""
    with conn.cursor() as cur:
        cur.execute(
            "select game_id, date, home_team, away_team from games where playoff = false and date = %s "
            "and (home_team = %s or away_team = %s)", (today, team_code, team_code))
        return cur.fetchone()


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


# --------------------------- player matching + review queue ---------------------------

def match_player(conn, name, expected_pos_group=None):
    """-> (player_id, None) | (None, 'no match') | (None, 'ambiguous: n candidates')."""
    nm = normalize_name(name)
    with conn.cursor() as cur:
        # Provisional rows created by nhl_add_player.py ('dfo_<id>') are excluded: they exist only so a brand-new
        # player can be in snapshots before his real Hockey-Reference id appears. Once the real row exists it wins.
        cur.execute(
            "select player_id, position from players where player_id not like 'dfo\\_%%' and "
            "trim(regexp_replace(replace(regexp_replace(lower(unaccent(player_name_display)), '[.''\u2019]', '', 'g'), '-', ' '), '\\s+', ' ', 'g')) = %s",
            (nm,))
        rows = cur.fetchall()
    if not rows:
        return None, "no match in players"
    if len(rows) == 1:
        return rows[0][0], None
    if expected_pos_group:
        filtered = [r for r in rows if r[1] == expected_pos_group]
        if len(filtered) == 1:
            return filtered[0][0], None
    return None, f"ambiguous: {len(rows)} players share this normalized name"


def most_recent_2526_team(conn, player_id):
    with conn.cursor() as cur:
        cur.execute(
            "select a.team_code from player_game_appearances a join games g using(game_id) "
            "where a.player_id = %s and g.season = '2526' and g.playoff = false "
            "order by g.date desc limit 1", (player_id,))
        row = cur.fetchone()
    return row[0] if row else None


def open_review_dfo_ids(conn, source):
    with conn.cursor() as cur:
        cur.execute("select dfo_player_id from player_id_review_queue where source = %s and status = 'open'", (source,))
        return {r[0] for r in cur.fetchall()}


_RESOLVED_CACHE = {}


def resolved_map(conn, source):
    """dfo_player_id -> player_id for queue rows Nick already resolved with nhl_add_player.py."""
    if source not in _RESOLVED_CACHE:
        with conn.cursor() as cur:
            cur.execute("select dfo_player_id, resolved_player_id from player_id_review_queue "
                        "where source = %s and status = 'resolved' and dfo_player_id is not null "
                        "and resolved_player_id is not null", (source,))
            _RESOLVED_CACHE[source] = {r[0]: r[1] for r in cur.fetchall()}
    return _RESOLVED_CACHE[source]


def resolve_player(conn, source, name, dfo_player_id, team_code, expected_pos_group, game_id, already_queued, queue_rows):
    """-> internal player_id, or None if the player was queued for review instead."""
    if dfo_player_id in already_queued:
        return None  # already flagged from an earlier page/run this session; don't write it and don't re-flag
    # A player Nick already resolved never comes back for review (new player, trade, name collision alike).
    prior = resolved_map(conn, source).get(dfo_player_id)
    if prior:
        if prior.startswith("dfo_"):
            # Provisional id: switch to the real Hockey-Reference row as soon as one matches by name.
            real_id, _ = match_player(conn, name, expected_pos_group)
            return real_id or prior
        return prior
    player_id, err = match_player(conn, name, expected_pos_group)
    if player_id is None:
        queue_rows.append((source, dfo_player_id, name, expected_pos_group, team_code, err, game_id))
        return None
    seen_team = most_recent_2526_team(conn, player_id)
    if seen_team and seen_team != team_code:
        queue_rows.append((source, dfo_player_id, name, expected_pos_group, team_code,
                           f"team mismatch: last seen on {seen_team}, DailyFaceoff shows {team_code}", game_id))
        return None
    return player_id


def write_review_queue(conn_factory, rows):
    if not rows:
        return
    if DRY_RUN:
        print(f"[dry-run] would queue {len(rows)} player(s) for review:")
        for r in rows:
            print("   ", r)
        return
    conn = conn_factory()
    try:
        with conn.cursor() as cur:
            execute_values(
                cur, "insert into player_id_review_queue (source, dfo_player_id, player_name_display, pos, "
                    "team_code, reason, game_id) values %s on conflict (source, dfo_player_id) where status = 'open' "
                    "do nothing", rows)
        conn.commit()
        print(f"[live] queued {len(rows)} player(s) for review.")
    finally:
        conn.close()


# --------------------------- lineups ---------------------------

def lineup_snapshot_pass(conn, today, queue_rows):
    print(f"== lineup snapshots ({RUN_MODE}) ==")
    already_queued = open_review_dfo_ids(conn, "dailyfaceoff_lines")
    rows = []

    team_slugs = TEAM_SLUGS
    if TARGET_GAME_ID:
        info = game_by_id(conn, TARGET_GAME_ID)
        if info is None:
            print(f"  TARGET_GAME_ID={TARGET_GAME_ID}: not found in games table -- nothing to do")
            return rows
        _, home, away = info
        slug_by_code = {v: k for k, v in TEAM_SLUGS.items()}
        team_slugs = {slug_by_code[c]: c for c in (home, away) if c in slug_by_code}

    for slug, expected_code in team_slugs.items():
        if RUN_MODE == "preliminary":
            game = upcoming_games_for_team(conn, expected_code, today, LOOKAHEAD_DAYS)
        else:
            game = game_today_for_team(conn, expected_code, today)
        if game is None:
            continue
        game_id, gdate, home, away = game
        if TARGET_GAME_ID and game_id != TARGET_GAME_ID:
            continue  # defensive -- shouldn't happen given the team_slugs restriction above
        with conn.cursor() as cur:
            cur.execute("select distinct snapshot_type from lineup_snapshots where game_id = %s and team_code = %s",
                        (game_id, expected_code))
            have = {r[0] for r in cur.fetchall()}
        if RUN_MODE in have:  # this pass's type ('preliminary' or 'confirmed') already written
            continue
        team_code, players, err = fetch_team_lines(slug)
        if err:
            print(f"  {expected_code} ({slug}): FAILED ({err})")
            time.sleep(REQUEST_DELAY_SECONDS)
            continue
        if team_code != expected_code:
            print(f"  WARNING {slug}: DailyFaceoff teamAbbreviation resolved to {team_code!r}, expected {expected_code!r}")
        resolved = []
        for p in players:
            pid = resolve_player(conn, "dailyfaceoff_lines", p["name"], p["dfo_player_id"], expected_code,
                                 p["pos_group"], game_id, already_queued, queue_rows)
            if pid:
                resolved.append(pid)
        print(f"  {expected_code} -> {game_id}: {len(resolved)}/{len(players)} matched, writing {RUN_MODE}")
        now = datetime.now(timezone.utc)
        rows += [(game_id, expected_code, pid, RUN_MODE, now) for pid in resolved]
        time.sleep(REQUEST_DELAY_SECONDS)
    return rows


# --------------------------- goalies ---------------------------

def goalie_snapshot_pass(conn, today, queue_rows):
    print(f"== goalie snapshots ({RUN_MODE}) ==")
    already_queued = open_review_dfo_ids(conn, "dailyfaceoff_goalies")
    rows = []

    target_home_away = None
    if TARGET_GAME_ID:
        info = game_by_id(conn, TARGET_GAME_ID)
        if info is None:
            print(f"  TARGET_GAME_ID={TARGET_GAME_ID}: not found in games table -- nothing to do")
            return rows
        gdate, home, away = info
        dates = [gdate]
        target_home_away = (home, away)
    else:
        dates = [today + timedelta(days=i) for i in range(LOOKAHEAD_DAYS + 1)] if RUN_MODE == "preliminary" else [today]

    for d in dates:
        games, err = fetch_goalies_for_date(d)
        if err:
            print(f"  {d}: FAILED ({err})")
            time.sleep(REQUEST_DELAY_SECONDS)
            continue
        if not games:
            print(f"  {d}: no games listed")
            time.sleep(REQUEST_DELAY_SECONDS)
            continue
        for g in games:
            if g.get("home") is None:
                print(f"  {d}: {g.get('note')}")
                continue
            if target_home_away and (g["home"], g["away"]) != target_home_away:
                continue
            game_id = resolve_game_id(conn, d, g["home"], g["away"])
            if game_id is None:
                print(f"  {g['away']} @ {g['home']} ({d}): not on the schedule")
                continue
            for team_code, gl in ((g["home"], g["home_goalie"]), (g["away"], g["away_goalie"])):
                if gl is None:
                    print(f"  {team_code} ({game_id}): no goalie listed yet")
                    continue
                with conn.cursor() as cur:
                    cur.execute("select distinct snapshot_type from goalie_snapshots where game_id = %s and team_code = %s",
                                (game_id, team_code))
                    have = {r[0] for r in cur.fetchall()}
                if RUN_MODE in have:
                    continue
                if TARGET_GAME_ID and str(gl.get("strength") or "").strip().lower() != "confirmed":
                    print(f"  {team_code} ({game_id}): {gl['name']!r} [{gl.get('strength')}] -- not confirmed at T-5, "
                          f"skipping (nightly sweep will capture)")
                    continue
                pid = resolve_player(conn, "dailyfaceoff_goalies", gl["name"], gl["dfo_player_id"], team_code, "G",
                                     game_id, already_queued, queue_rows)
                status = gl.get("strength")
                print(f"  {team_code} ({game_id}): {gl['name']!r} [{status}] -> "
                      f"{'matched ' + pid if pid else 'QUEUED for review'}, writing {RUN_MODE if pid else '(nothing)'}")
                if pid:
                    now = datetime.now(timezone.utc)
                    rows.append((game_id, team_code, pid, RUN_MODE, now, status))
        time.sleep(REQUEST_DELAY_SECONDS)
    return rows


# --------------------------- write + main ---------------------------

# Each table's insert columns (row tuples must be built in this order) and conflict target.
# goalie_snapshots carries one extra column (dfo_status) that lineup_snapshots has no equivalent for.
TABLE_SPEC = {
    "lineup_snapshots": {
        "columns": ["game_id", "team_code", "player_id", "snapshot_type", "scraped_at"],
        "conflict": "(game_id, team_code, player_id, snapshot_type)",
    },
    "goalie_snapshots": {
        "columns": ["game_id", "team_code", "player_id", "snapshot_type", "scraped_at", "dfo_status"],
        "conflict": "(game_id, team_code, snapshot_type)",
    },
}


def write_snapshots(conn_factory, table, rows):
    if not rows:
        print(f"{table}: nothing to write.")
        return
    if DRY_RUN:
        print(f"[dry-run] would write {len(rows)} row(s) to {table}:")
        for r in rows:
            print("   ", r)
        return
    spec = TABLE_SPEC[table]
    conn = conn_factory()
    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                f"insert into {table} ({', '.join(spec['columns'])}) values %s "
                f"on conflict {spec['conflict']} do nothing",
                rows)
        conn.commit()
        print(f"[live] wrote {len(rows)} row(s) to {table}.")
    finally:
        conn.close()


def main():
    today = et_today()
    print(f"NHL lineup/goalie ingest -- DRY_RUN={DRY_RUN} -- RUN_MODE={RUN_MODE} -- TARGET_GAME_ID={TARGET_GAME_ID} -- "
          f"today (ET) {today} -- lookahead {LOOKAHEAD_DAYS}d (preliminary only)")
    if not os.environ.get("PGHOST"):
        print("No PGHOST set -- nothing to do.")
        return
    conn = get_db_conn()
    try:
        queue_rows = []
        lineup_rows = lineup_snapshot_pass(conn, today, queue_rows)
        goalie_rows = goalie_snapshot_pass(conn, today, queue_rows)
    finally:
        conn.close()
    write_snapshots(get_db_conn, "lineup_snapshots", lineup_rows)
    write_snapshots(get_db_conn, "goalie_snapshots", goalie_rows)
    write_review_queue(get_db_conn, queue_rows)
    print("Done.")


if __name__ == "__main__":
    main()
