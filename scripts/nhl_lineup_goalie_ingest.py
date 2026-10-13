"""
nhl_lineup_goalie_ingest.py -- captures DailyFaceoff lineups and starting goalies into lineup_snapshots /
goalie_snapshots, and queues any player it can't confidently match into player_id_review_queue instead of
guessing.

SNAPSHOT LIFECYCLE (changed 2026-10-13)
--------------------------------------------------------------------------
A game's snapshot is a LIVE row until the T-5 freeze, then a final row:

  RUN_MODE=preliminary  (lineup-goalie-preliminary.yml, every 30 minutes)
    LIVE refresh. Every run re-reads DailyFaceoff and REPLACES the team-game's 'preliminary' rows (lineup and
    goalie), stamping scraped_at with the time of this read. It keeps doing that until the game is frozen
    ('confirmed' row exists) or puck drop has passed (games.start_time, filled in by nhl_t5_scheduler.py).
    A failed fetch, an empty page, or a goalie DailyFaceoff hasn't listed never wipes an existing row.

  RUN_MODE=confirmed + TARGET_GAME_ID  (t5-trigger.yml, fired by the Cloudflare Worker at T-5)
    FINAL FREEZE for that one game. Reads DailyFaceoff right now and REPLACES the game's 'confirmed' rows with
    whatever DailyFaceoff shows -- whatever its status (Confirmed / Likely / blank); the status is stored in
    goalie_snapshots.dfo_status. Nothing before this moment freezes anything. Refuses to run without
    TARGET_GAME_ID, and skips a game that started more than FREEZE_GRACE_MINUTES ago.

  RUN_MODE=freeze_missed  (lineup-goalie-confirmed.yml, once each morning)
    Safety net for a game whose T-5 freeze never happened (Worker outage, etc.): for any game from the last
    7 days (before today, ET) with live rows but no 'confirmed' rows, copies the latest live rows into
    'confirmed' unchanged, keeping their original scraped_at. Runs before the daily results ingest.

AFTER THE GAME (nhl_daily_results_ingest.py) -- the one definitive overwrite
--------------------------------------------------------------------------
  - Skaters: the 'confirmed' lineup is replaced by the skaters who actually played.
  - Goalie: the frozen 'confirmed' goalie is kept unless he did not appear in the box score at all (or no
    snapshot exists), in which case the goalie with the most ice time replaces him.
  This script never touches a game after its freeze other than through freeze_missed.

DFO_STATUS COLUMN (goalie_snapshots only)
--------------------------------------------------------------------------
DailyFaceoff's own published status for the goalie (homeNewsStrengthName / awayNewsStrengthName): 'Confirmed',
'Likely', or blank when DailyFaceoff has not marked him. Stored verbatim; the dashboard shows exactly this
(blank -> "Unconfirmed") and never substitutes our own snapshot type for it.

PLAYER MATCHING (this is the "prompts him" workflow)
--------------------------------------------------------------------------
For every skater DailyFaceoff lists in a team's 12 forwards / 6 defensemen (the 'ev' category groups f1-f4,
d1-d3) and every starting/likely goalie:
  1. Normalize the name (unaccent, drop punctuation) and look it up against `players`. Zero or more-than-one
     match (after trying to break ties by position) means "not confidently identified."
  2. If matched, compare DailyFaceoff's team to the player's own most recent appearance. A mismatch (a trade
     DailyFaceoff knows about that we don't, or a bad match) is NOT auto-corrected.
  3. Anything from (1) or (2) is written to player_id_review_queue (skipped for this snapshot, not guessed
     at). Everyone else on the same page is still written -- one flagged player doesn't block a team.
Injured/scratched players ('oi'/'ir' categories) are excluded. DailyFaceoff doesn't cover preseason.

SAFETY
--------------------------------------------------------------------------
DRY_RUN defaults to true. RUN_MODE is required. Preliminary looks at games within LOOKAHEAD_DAYS (default 2),
regular season only.
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
if RUN_MODE not in ("preliminary", "confirmed", "freeze_missed"):
    raise SystemExit(f"RUN_MODE must be 'preliminary', 'confirmed' or 'freeze_missed', got {RUN_MODE!r}.")
TARGET_GAME_ID = os.environ.get("TARGET_GAME_ID", "").strip() or None
if RUN_MODE == "confirmed" and not TARGET_GAME_ID:
    raise SystemExit("RUN_MODE=confirmed is the T-5 final freeze and requires TARGET_GAME_ID "
                     "(use RUN_MODE=freeze_missed for the morning safety net).")
if RUN_MODE != "confirmed":
    TARGET_GAME_ID = None
FREEZE_GRACE_MINUTES = int(os.environ.get("FREEZE_GRACE_MINUTES", "15"))

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


def most_recent_team(conn, player_id, game_id):
    """Team of the player's most recent regular-season appearance, looking only at the season of the game
    being processed and the season before it (so nothing is hard-coded to one year). A player with no
    appearances in that window returns None and isn't team-checked. In season this means a mid-year trade is
    flagged once, and stops being flagged after his first game for the new team."""
    with conn.cursor() as cur:
        cur.execute(
            "select a.team_code from player_game_appearances a "
            "join games g using(game_id) "
            "join games cur_g on cur_g.game_id = %s "
            "where a.player_id = %s and g.playoff = false "
            "and g.season in (cur_g.season, ((cur_g.season::int - 101)::text)) "
            "order by g.date desc limit 1", (game_id, player_id))
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
    seen_team = most_recent_team(conn, player_id, game_id)
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


# --------------------------- freshness guards ---------------------------

def game_has_started(conn, game_id, grace_minutes=0):
    """True once puck drop (+ grace) has passed. games.start_time is filled in by nhl_t5_scheduler.py; when it
    isn't known yet this returns False and callers rely on their date filter."""
    with conn.cursor() as cur:
        cur.execute("select start_time from games where game_id = %s", (game_id,))
        row = cur.fetchone()
    if not row or row[0] is None:
        return False
    return datetime.now(timezone.utc) >= row[0] + timedelta(minutes=grace_minutes)


def has_confirmed(conn, table, game_id, team_code):
    with conn.cursor() as cur:
        cur.execute(f"select 1 from {table} where game_id = %s and team_code = %s and snapshot_type = 'confirmed' limit 1",
                    (game_id, team_code))
        return cur.fetchone() is not None


# --------------------------- lineups ---------------------------

def lineup_snapshot_pass(conn, today, queue_rows):
    """-> (rows, replace_keys). replace_keys = (game_id, team_code, snapshot_type) whose existing rows get replaced."""
    print(f"== lineup snapshots ({RUN_MODE}) ==")
    already_queued = open_review_dfo_ids(conn, "dailyfaceoff_lines")
    rows, replace_keys = [], []
    snap_type = "confirmed" if RUN_MODE == "confirmed" else "preliminary"
    slug_by_code = {v: k for k, v in TEAM_SLUGS.items()}

    targets = []  # (slug, team_code, game_id)
    if RUN_MODE == "confirmed":
        info = game_by_id(conn, TARGET_GAME_ID)
        if info is None:
            print(f"  TARGET_GAME_ID={TARGET_GAME_ID}: not found in games table -- nothing to do")
            return rows, replace_keys
        _, home, away = info
        targets = [(slug_by_code[c], c, TARGET_GAME_ID) for c in (home, away) if c in slug_by_code]
    else:
        for slug, code in TEAM_SLUGS.items():
            game = upcoming_games_for_team(conn, code, today, LOOKAHEAD_DAYS)
            if game is not None:
                targets.append((slug, code, game[0]))

    for slug, expected_code, game_id in targets:
        if RUN_MODE == "confirmed":
            if game_has_started(conn, game_id, FREEZE_GRACE_MINUTES):
                print(f"  {expected_code} ({game_id}): started more than {FREEZE_GRACE_MINUTES} min ago -- not freezing")
                continue
        else:
            if has_confirmed(conn, "lineup_snapshots", game_id, expected_code) or game_has_started(conn, game_id):
                continue  # already frozen, or puck has dropped -- live refresh stops
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
        if not resolved:
            print(f"  {expected_code} -> {game_id}: no players matched -- leaving existing {snap_type} snapshot untouched")
            time.sleep(REQUEST_DELAY_SECONDS)
            continue
        print(f"  {expected_code} -> {game_id}: {len(resolved)}/{len(players)} matched, writing {snap_type}")
        now = datetime.now(timezone.utc)
        rows += [(game_id, expected_code, pid, snap_type, now) for pid in resolved]
        replace_keys.append((game_id, expected_code, snap_type))
        time.sleep(REQUEST_DELAY_SECONDS)
    return rows, replace_keys


# --------------------------- goalies ---------------------------

def goalie_snapshot_pass(conn, today, queue_rows):
    print(f"== goalie snapshots ({RUN_MODE}) ==")
    already_queued = open_review_dfo_ids(conn, "dailyfaceoff_goalies")
    rows = []
    snap_type = "confirmed" if RUN_MODE == "confirmed" else "preliminary"

    target_home_away = None
    if RUN_MODE == "confirmed":
        info = game_by_id(conn, TARGET_GAME_ID)
        if info is None:
            print(f"  TARGET_GAME_ID={TARGET_GAME_ID}: not found in games table -- nothing to do")
            return rows
        gdate, home, away = info
        dates = [gdate]
        target_home_away = (home, away)
    else:
        dates = [today + timedelta(days=i) for i in range(LOOKAHEAD_DAYS + 1)]

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
            if RUN_MODE == "confirmed":
                if game_has_started(conn, game_id, FREEZE_GRACE_MINUTES):
                    print(f"  {game_id}: started more than {FREEZE_GRACE_MINUTES} min ago -- not freezing")
                    continue
            elif game_has_started(conn, game_id):
                continue
            for team_code, gl in ((g["home"], g["home_goalie"]), (g["away"], g["away_goalie"])):
                if gl is None:
                    print(f"  {team_code} ({game_id}): no goalie listed yet -- existing snapshot untouched")
                    continue
                if RUN_MODE == "preliminary" and has_confirmed(conn, "goalie_snapshots", game_id, team_code):
                    continue  # frozen
                pid = resolve_player(conn, "dailyfaceoff_goalies", gl["name"], gl["dfo_player_id"], team_code, "G",
                                     game_id, already_queued, queue_rows)
                status = gl.get("strength")
                print(f"  {team_code} ({game_id}): {gl['name']!r} [{status or 'unconfirmed'}] -> "
                      f"{'matched ' + pid if pid else 'QUEUED for review'}, writing {snap_type if pid else '(nothing)'}")
                if pid:
                    rows.append((game_id, team_code, pid, snap_type, datetime.now(timezone.utc), status))
        time.sleep(REQUEST_DELAY_SECONDS)
    return rows


# --------------------------- write + main ---------------------------

# goalie_snapshots carries one extra column (dfo_status). Both tables are REPLACED, not appended: lineups by
# deleting the team-game's rows of that snapshot_type first, goalies by upserting on the conflict target.
TABLE_SPEC = {
    "lineup_snapshots": {
        "columns": ["game_id", "team_code", "player_id", "snapshot_type", "scraped_at"],
        "conflict": "(game_id, team_code, player_id, snapshot_type) do nothing",
    },
    "goalie_snapshots": {
        "columns": ["game_id", "team_code", "player_id", "snapshot_type", "scraped_at", "dfo_status"],
        "conflict": "(game_id, team_code, snapshot_type) do update set player_id = excluded.player_id, "
                    "scraped_at = excluded.scraped_at, dfo_status = excluded.dfo_status",
    },
}


def write_snapshots(conn_factory, table, rows, replace_keys=None):
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
            for gid, team, stype in (replace_keys or []):
                cur.execute(f"delete from {table} where game_id = %s and team_code = %s and snapshot_type = %s",
                            (gid, team, stype))
            execute_values(
                cur,
                f"insert into {table} ({', '.join(spec['columns'])}) values %s on conflict {spec['conflict']}",
                rows)
        conn.commit()
        print(f"[live] wrote {len(rows)} row(s) to {table}.")
    finally:
        conn.close()


def freeze_missed(conn_factory, today):
    """Morning safety net: copy the latest live rows to 'confirmed' for recent finished games that never got a
    T-5 freeze. Keeps the original scraped_at so the data's age stays honest."""
    cutoff = today - timedelta(days=7)
    lineup_sql = (
        "insert into lineup_snapshots (game_id, team_code, player_id, snapshot_type, scraped_at) "
        "select p.game_id, p.team_code, p.player_id, 'confirmed', p.scraped_at "
        "from lineup_snapshots p join games g using (game_id) "
        "where p.snapshot_type = 'preliminary' and g.playoff = false and g.date < %s and g.date >= %s "
        "and not exists (select 1 from lineup_snapshots c where c.game_id = p.game_id and c.team_code = p.team_code "
        "and c.snapshot_type = 'confirmed') on conflict do nothing")
    goalie_sql = (
        "insert into goalie_snapshots (game_id, team_code, player_id, snapshot_type, scraped_at, dfo_status) "
        "select p.game_id, p.team_code, p.player_id, 'confirmed', p.scraped_at, p.dfo_status "
        "from goalie_snapshots p join games g using (game_id) "
        "where p.snapshot_type = 'preliminary' and g.playoff = false and g.date < %s and g.date >= %s "
        "and not exists (select 1 from goalie_snapshots c where c.game_id = p.game_id and c.team_code = p.team_code "
        "and c.snapshot_type = 'confirmed') on conflict do nothing")
    conn = conn_factory()
    try:
        with conn.cursor() as cur:
            if DRY_RUN:
                for label, table in (("lineup", "lineup_snapshots"), ("goalie", "goalie_snapshots")):
                    cur.execute(
                        f"select count(distinct (p.game_id, p.team_code)) from {table} p join games g using (game_id) "
                        "where p.snapshot_type = 'preliminary' and g.playoff = false and g.date < %s and g.date >= %s "
                        f"and not exists (select 1 from {table} c where c.game_id = p.game_id and "
                        "c.team_code = p.team_code and c.snapshot_type = 'confirmed')", (today, cutoff))
                    print(f"[dry-run] would freeze {cur.fetchone()[0]} team-game(s) of {label} snapshots")
                return
            cur.execute(lineup_sql, (today, cutoff))
            n_l = cur.rowcount
            cur.execute(goalie_sql, (today, cutoff))
            n_g = cur.rowcount
        conn.commit()
        print(f"[live] freeze_missed: copied {n_l} lineup row(s) and {n_g} goalie row(s) to 'confirmed'.")
    finally:
        conn.close()


def main():
    today = et_today()
    print(f"NHL lineup/goalie ingest -- DRY_RUN={DRY_RUN} -- RUN_MODE={RUN_MODE} -- TARGET_GAME_ID={TARGET_GAME_ID} -- "
          f"today (ET) {today} -- lookahead {LOOKAHEAD_DAYS}d (preliminary only)")
    if not os.environ.get("PGHOST"):
        print("No PGHOST set -- nothing to do.")
        return
    if RUN_MODE == "freeze_missed":
        freeze_missed(get_db_conn, today)
        print("Done.")
        return
    conn = get_db_conn()
    try:
        queue_rows = []
        lineup_rows, lineup_replace = lineup_snapshot_pass(conn, today, queue_rows)
        goalie_rows = goalie_snapshot_pass(conn, today, queue_rows)
    finally:
        conn.close()
    write_snapshots(get_db_conn, "lineup_snapshots", lineup_rows, lineup_replace)
    write_snapshots(get_db_conn, "goalie_snapshots", goalie_rows)
    write_review_queue(get_db_conn, queue_rows)
    print("Done.")


if __name__ == "__main__":
    main()
