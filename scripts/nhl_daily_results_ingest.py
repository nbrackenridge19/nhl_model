"""
nhl_daily_results_ingest.py — automated daily results ingestion (Project 3)

Replaces the manual nhl.py workflow. Pulls the previous day's completed
games from Hockey-Reference box scores, writes games / team_game_stats /
player_game_appearances / goalie_game_appearances / player_advanced_game_appearances,
then refreshes the materialized view chain so KResult / bank views pick up
the new games automatically.

FORMULA PROVENANCE (read directly from source files, not inferred — see
project learnings on this discipline)
--------------------------------------------------------------------------
game_id scheme, box score URL, and the skater/goalie/advanced-table parsers
below are adapted near-verbatim from nhl_full_historical_backfill.py, so
new games use the exact same identity scheme as the 9-season historical
backfill.

team_game_stats' four "blended" columns (corsi_for_pct, pp, pk,
sv_pct_above_expected) were, for historical seasons, precomputed by Excel
formulas and simply read in. There is no Excel for live games, so this
script reimplements those formulas directly, traced from
gamelog2526.xlsm's ALL sheet and per-team tabs (e.g. 'CHI'):

  CFPct  = if n < 40: (cf_int + cf_pycf * prior_season_cfpct) * (40-n)/40
                       + (CF_td / (CF_td + CA_td)) * (n/40)
           else: CF_td / (CF_td + CA_td)
  PP     = if n < 40: (pp_int + pp_pypp * prior_season_pp) * (40-n)/40
                       + (PPG_td / PPO_td) * (n/40)
           else: PPG_td / PPO_td
  PK     = if n < 40: (pk_int + pk_pypk * prior_season_pk) * (40-n)/40
                       + (1 - PKGA_td / PKO_td) * (n/40)
           else: 1 - PKGA_td / PKO_td

  where n = this team's completed games so far this season (before this
  game's date), _td = that team's own cumulative total prior to this game,
  and cf_int/cf_pycf/pp_int/pp_pypp/pk_int/pk_pypk are season-level
  regression-to-prior-season-value constants (see REGRESSION_CONSTANTS_BY_SEASON
  below — read directly from gamelog2526.xlsm ALL!Z2:Z7. These are refit
  once per season; if this script is still in use next season, re-pull
  them from that season's gamelog workbook before relying on this file).

  prior_season_cfpct/pp/pk = this team's OWN final-game value in the prior
  season, which (since a season always exceeds 40 games) is already the
  pure season-to-date value — so it's just `SELECT ... FROM team_game_stats
  WHERE team_code=X ORDER BY date DESC LIMIT 1` for the prior season. No
  separate table needed.

  sv_pct_above_expected uses a career-shots-weighted Bayesian blend against
  a league-average baseline (traced from the 'CHI'!$N$54 formula and the
  goaliesH_ind/goaliesH_lg career tables) — the static per-goalie priors
  this needs were extracted once from gamelog2526.xlsm and are now in the
  new `goalie_career_priors` Supabase table (see migration applied earlier
  this session). Formula, per goalie, per game:

    sSA, sSV = this goalie's season-to-date shots-against/saves (before
               this game's date, THIS season only)
    SvPctR = (career_sv_pct_above_expected * career_shots + sSV
              - league_avg_sv_pct_asof_today * sSA)
             / max(career_shots + sSA, 1050)

  "league_avg_sv_pct_asof_today" is itself a league-wide blend (prior-2-
  season league average fading into this season's actual league SV% as
  games accumulate) — see league_avg_sv_pct_today() below, traced from the
  ALL sheet's SSvPct LET-formula.

PP / PK SOURCE
--------------------------------------------------------------------------
Team-level PP goals/opportunities and PK goals-against/opportunities-
against (MUGF/MUOF/MDGA/MDOA in the original workbook) are NOT on the
Hockey-Reference box score page. They come from NHL.com's gamecenter feed
(api-web.nhle.com, the 'powerPlay' team stat, written "goals/opportunities").
Checked on 14 completed preseason games: NHL.com's numbers matched ESPN's on
every team. ESPN is not used because it answers 403 to GitHub's servers.

corsi_for_pct's raw CF/CA inputs ARE confirmed, against the actual
recorded values in gamelog2526.xlsm during the Project 3 backfill
(2026-03-08 TBL @ BUF: TBL 46/40, BUF 42/43): they come from the team's
5v5-specific advanced table's <tfoot> TOTAL row (table id
'{TEAM}_adv_ALL5v5', data-stat 'on_Cevents'/'on_opp_Cevents') — NOT the
All Situations table, and NOT a tbody row (Sports-Reference puts team
totals in <tfoot>, separate from the per-player <tbody> rows). An earlier
version of this script incorrectly read from the All Situations table's
tbody, which produced garbage (no tfoot check, no situational distinction)
— this was caught and fixed during the Project 3 backfill; see chat.

SEASON ROLLOVER CHANGES (2026-27) — see chat for the reasoning behind each
--------------------------------------------------------------------------
1. Games are matched to the pre-loaded schedule rows in `games` on
   (date, home, away) instead of allocating new game_ids by counting. Only
   playoff games (not in the schedule) still get new 'P' ids.
2. Self-healing: every run also processes any schedule game from the last
   LOOKBACK_DAYS (default 7) that still has no score, oldest date first, so
   a missed or failed run is caught up automatically. Already-ingested games
   are skipped, so re-running is safe.
3. Season and prior season come from season_config (no hardcoded 2526).
4. Regression constants are looked up per season; the run aborts if the
   season has no entry rather than silently using last season's.
5. team_game_stats_raw is now written each day (it was only backfilled, so
   the CF/PP/PK blends could never move off the prior-season prediction).
6. New players are inserted into `players` before their appearances (the
   foreign key used to make the whole upsert fail), and any skater without a
   prior-season/rookie row (or goalie without career priors) is written to
   player_review_queue and printed as ACTION NEEDED.
7. The game list, preseason flag and PP/PK now come from NHL.com's API (ESPN blocks
   GitHub's servers). NHL abbreviations (only VGK differs) are converted to our team codes.
8. Preseason and other non-schedule games are skipped (they never match a
   schedule row). Playoffs are handled only if NHL.com marks them game type 3.
9. Refresh chain fixed: it now includes v_team_game_perspective (without it
   new games never reached the bet/result views). The v2/v4 experiment chain
   is refreshed only when REFRESH_EXPERIMENTAL=true. Each view is refreshed
   and committed on its own.

SAFETY
------
- DB connection: PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT env vars.
- DRY_RUN defaults to true. In a dry run nothing is written, so later dates in
  a catch-up run are computed without the earlier dates' rows (approximate).
- INGEST_DATE=YYYY-MM-DD processes just that date (for testing).
- Designed to run once per morning; not the T-5 pre-game path.
"""

import os
import re
import time
from datetime import date, datetime, timedelta, timezone

import requests
import psycopg2
from psycopg2.extras import execute_values
from bs4 import BeautifulSoup, Comment

DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "3"))
HEADERS = {"User-Agent": "Mozilla/5.0"}
BOXSCORE_URL = "https://www.hockey-reference.com/boxscores/{code}.html"
NHL_SCORE_URL = "https://api-web.nhle.com/v1/score/{d}"
NHL_RIGHT_RAIL_URL = "https://api-web.nhle.com/v1/gamecenter/{gid}/right-rail"
NHL_BOXSCORE_URL = "https://api-web.nhle.com/v1/gamecenter/{gid}/boxscore"

# Read from gamelog2526.xlsm ALL!Z2:Z7 this session — refit once per
# season. Re-pull from the current season's gamelog workbook if this
# script is reused next season.
REGRESSION_CONSTANTS_BY_SEASON = {
    # 2025-26: read from gamelog2526.xlsm ALL!Z2:Z7 ("2017-18 to 2024-25").
    "2526": {
        "cf_int": 0.1448593885572126, "cf_pycf": 0.7121483119094089,
        "pp_int": 0.11751272458282204, "pp_pypp": 0.4145979201770057,
        "pk_int": 0.5684377783357512, "pk_pypk": 0.2890725321972133,
    },
    # 2026-27: PROVISIONAL. OLS of each team's end-of-season value on its prior
    # season's, pooled over 2017-18 -> 2025-26 (252 team-season pairs, Arizona
    # mapped to Utah), computed from team_game_stats. My reproduction of the
    # 2025-26 constants this way lands close but not exactly on the Excel
    # values (e.g. cf 0.1424/0.7174 vs 0.1449/0.7121), so replace these with
    # the constants from the original regression when they are available.
    "2627": {
        "cf_int": 0.137568952186152, "cf_pycf": 0.727128564565087,
        "pp_int": 0.124030525955121, "pp_pypp": 0.400801388107046,
        "pk_int": 0.607263925916057, "pk_pypk": 0.234558133237551,
    },
}
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))
REFRESH_EXPERIMENTAL = os.environ.get("REFRESH_EXPERIMENTAL", "false").strip().lower() == "true"


def regression_constants(season):
    if season not in REGRESSION_CONSTANTS_BY_SEASON:
        raise RuntimeError(
            f"No regression constants for season {season}. Add them to REGRESSION_CONSTANTS_BY_SEASON "
            "(refit each season) before running this script for that season.")
    return REGRESSION_CONSTANTS_BY_SEASON[season]
BLEND_GAME_THRESHOLD = 40  # games after which the blend is pure season-to-date
GOALIE_SHOTS_FLOOR = 1050  # min(shots) denominator floor in the SvPctR blend

# NHL.com abbreviation (lower case) -> our team_code. Only VGK differs from ours; the rest
# are kept in case another feed's abbreviations ever come through.
FEED_TO_INTERNAL = {"tb": "tbl", "sj": "sjs", "nj": "njd", "la": "lak", "vgk": "veg", "was": "wsh", "utah": "uta"}

def get_db_conn():
    if os.environ.get("PGHOST"):
        try:
            return psycopg2.connect(connect_timeout=20)
        except psycopg2.OperationalError as e:
            print(f"DATABASE CONNECTION FAILED (host={os.environ.get('PGHOST')!r}, port={os.environ.get('PGPORT')!r}, "
                  f"user={os.environ.get('PGUSER')!r}): {e}")
            print("Hint: Supabase's direct host (db.<ref>.supabase.co) is IPv6-only, which GitHub's runners can't "
                  "reach; use the pooler host from Supabase > Connect instead.")
            raise
    raise RuntimeError("Set PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT.")


def normalize_team_code(code):
    """Feed abbreviation (lower case) -> our team_code."""
    return FEED_TO_INTERNAL.get(code, code)


def normalize_player_name(raw):
    if raw is None:
        return "", ""
    try:
        display = raw.encode("latin1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        display = raw
    return display.strip().lower(), display.strip()


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


def parse_toi_seconds(toi_str):
    if not toi_str or ":" not in toi_str:
        return None
    minutes, seconds = toi_str.split(":")
    return int(minutes) * 60 + int(seconds)


def extract_player_id(cell):
    a = cell.find("a")
    if a is None or "href" not in a.attrs:
        return None, normalize_player_name(cell.get_text(strip=True))[1]
    href = a["href"]
    m = re.search(r"/players/[a-z]/([a-zA-Z0-9.]+)\.html", href)
    player_id = m.group(1) if m else None
    return player_id, normalize_player_name(a.get_text(strip=True))[1]


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


def build_boxscore_code(game_date, home_team):
    return f"{game_date.strftime('%Y%m%d')}0{home_team.upper()}"


# --------------------------- game_id assignment ---------------------------

def next_game_ids(conn, season_code, count, playoff=False):
    """Next sequential game_id(s) for a season, matching the existing
    `{season}_{season}{n}` / `{season}_{season}P{n}` scheme (see
    nhl_full_historical_backfill.py's migrate_core_for_season — game_id is
    literally f"{season_code}_{lgame}" where lgame concatenates the season
    code with a per-season sequential integer, 'P'-infixed for playoffs).
    """
    with conn.cursor() as cur:
        if playoff:
            cur.execute(
                "select game_id from games where season = %s and playoff = true "
                "order by (regexp_replace(game_id, '.*P', ''))::int desc limit 1",
                (season_code,),
            )
        else:
            cur.execute(
                "select game_id from games where season = %s and playoff = false "
                "order by (regexp_replace(game_id, '.*_" + season_code + "', ''))::int desc limit 1",
                (season_code,),
            )
        row = cur.fetchone()
    if row is None:
        last_n = 0
    else:
        last_id = row[0]
        last_n = int(re.search(r"P?(\d+)$", last_id).group(1))
    ids = []
    for i in range(1, count + 1):
        n = last_n + i
        lgame = f"{season_code}P{n}" if playoff else f"{season_code}{n}"
        ids.append(f"{season_code}_{lgame}")
    return ids


# --------------------------- box score scraping ---------------------------

def scrape_game_box(game_id, game_date, home_team, away_team):
    code = build_boxscore_code(game_date, home_team)
    url = BOXSCORE_URL.format(code=code)
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    # Final score + attendance
    home_goals = away_goals = None
    for tag in soup.select("div[class*=scorebox] div"):
        pass  # scorebox parsing below is more robust via the summary tables
    attendance = None
    m = re.search(r"Attendance:\s*([\d,]+)", soup.get_text())
    if m:
        attendance = int(m.group(1).replace(",", ""))

    skaters, goalies, advanced, team_totals = [], [], [], {}
    for team_code in (home_team, away_team):
        t_upper = team_code.upper()
        sk_table = get_soup_table(soup, f"{t_upper}_skaters")
        go_table = get_soup_table(soup, f"{t_upper}_goalies")
        adv_all_table = get_soup_table(soup, f"{t_upper}_adv_ALLAll")
        adv_5v5_table = get_soup_table(soup, f"{t_upper}_adv_ALL5v5")

        team_score = None
        if sk_table is not None:
            rows, total_row_goals = parse_skater_table(sk_table, game_id, team_code)
            skaters += rows
            team_score = total_row_goals
        if go_table is not None:
            goalies += parse_goalie_table(go_table, game_id, team_code)
        if adv_all_table is not None:
            advanced += parse_advanced_table(adv_all_table, game_id, team_code)
        cf = ca = None
        if adv_5v5_table is not None:
            cf, ca = parse_team_cf_ca_5v5(adv_5v5_table)
        team_totals[team_code] = {"cf": cf, "ca": ca, "goals": team_score}

    if home_team in team_totals:
        home_goals = team_totals[home_team]["goals"]
    if away_team in team_totals:
        away_goals = team_totals[away_team]["goals"]

    return {
        "attendance": attendance, "home_goals": home_goals, "away_goals": away_goals,
        "skaters": skaters, "goalies": goalies, "advanced": advanced, "team_totals": team_totals,
    }


def parse_skater_table(table, game_id, team_code):
    rows = []
    total_goals = None
    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        row = {c.get("data-stat"): c.get_text(strip=True) for c in cells}
        player_cell = tr.find("td", {"data-stat": "player"})
        if player_cell is None:
            # TOTAL row has no player link — this is where the team's final
            # goal count for this table lives.
            if row.get("goals"):
                total_goals = parse_int(row.get("goals"))
            continue
        player_id, player_name = extract_player_id(player_cell)
        if player_id is None:
            continue
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
    return rows, total_goals


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
    """Per-player rows come from the All Situations table (unchanged, matches
    the existing Project 1 player_advanced_game_appearances convention)."""
    rows = []
    for tr in table.find("tbody").find_all("tr"):
        cells = tr.find_all(["th", "td"])
        row = {c.get("data-stat"): c.get_text(strip=True) for c in cells}
        player_cell = tr.find("td", {"data-stat": "player"})
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
            "zone_start_off": parse_int(row.get("zs_off")), "zone_start_def": parse_int(row.get("zs_def")),
            "off_zone_start_pct": parse_float(row.get("ozs_pct")),
            "hits": parse_int(row.get("hits")), "blocks": parse_int(row.get("blocks")),
        })
    return rows


def parse_team_cf_ca_5v5(table):
    """Team-level CF/CA for the corsi_for_pct blend formula — traced from the
    5v5 table's <tfoot> TOTAL row (NOT the All Situations table's tbody —
    confirmed against gamelog2526.xlsm's own recorded values during the
    Project 3 backfill: TBL 46/40, BUF 42/43 for the 2026-03-08 game only
    matched the 5v5 table's tfoot, not All Situations)."""
    tfoot = table.find("tfoot")
    if tfoot is None:
        return None, None
    tr = tfoot.find("tr")
    if tr is None:
        return None, None
    cells = tr.find_all(["th", "td"])
    row = {c.get("data-stat"): c.get_text(strip=True) for c in cells}
    return parse_int(row.get("on_Cevents")), parse_int(row.get("on_opp_Cevents"))


# --------------------------- PP/PK lookup (NHL.com) ---------------------------

def find_category(obj, category):
    """Recursively find dicts like {'category': 'powerPlay', 'awayValue': '1/3', 'homeValue': '0/2'}."""
    found = []
    if isinstance(obj, dict):
        if obj.get("category") == category:
            found.append(obj)
        for v in obj.values():
            found += find_category(v, category)
    elif isinstance(obj, list):
        for v in obj:
            found += find_category(v, category)
    return found


def parse_goals_over_opps(value):
    """'1/6' -> (1, 6); None if it cannot be read."""
    try:
        g, o = str(value).split("/")
        return int(g), int(o)
    except (ValueError, TypeError):
        return None


def fetch_pp_pk(game_id, home_team, away_team):
    """Returns {team_code: {'pp_goals':, 'pp_opp':, 'pk_goals_against':, 'pk_opp':}} or {} if not found.
    A team's PK numbers are its opponent's PP numbers."""
    for url in (NHL_RIGHT_RAIL_URL.format(gid=game_id), NHL_BOXSCORE_URL.format(gid=game_id)):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code != 200:
                continue
            cats = find_category(r.json(), "powerPlay")
        except Exception:
            continue
        if not cats:
            continue
        home = parse_goals_over_opps(cats[0].get("homeValue"))
        away = parse_goals_over_opps(cats[0].get("awayValue"))
        if home is None or away is None:
            continue
        return {
            home_team: {"pp_goals": home[0], "pp_opp": home[1], "pk_goals_against": away[0], "pk_opp": away[1]},
            away_team: {"pp_goals": away[0], "pp_opp": away[1], "pk_goals_against": home[0], "pk_opp": home[1]},
        }
    return {}


# --------------------------- trailing blend computations ---------------------------

def games_played_before(conn, team_code, season, before_date):
    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from team_game_stats tgs join games g on g.game_id = tgs.game_id "
            "where tgs.team_code = %s and g.season = %s and g.playoff = false and g.date < %s",
            (team_code, season, before_date),
        )
        return cur.fetchone()[0]


def cumulative_cf_ca(conn, team_code, season, before_date):
    with conn.cursor() as cur:
        cur.execute(
            "select coalesce(sum(cf_raw),0), coalesce(sum(ca_raw),0) from team_game_stats_raw "
            "join games g using(game_id) where team_code=%s and g.season=%s and g.playoff=false and g.date < %s",
            (team_code, season, before_date),
        )
        return cur.fetchone()


def prior_season_value(conn, team_code, prior_season, column):
    with conn.cursor() as cur:
        cur.execute(
            f"select tgs.{column} from team_game_stats tgs join games g on g.game_id = tgs.game_id "
            "where tgs.team_code=%s and g.season=%s order by g.date desc limit 1",
            (team_code, prior_season),
        )
        row = cur.fetchone()
    return row[0] if row else None


def blended_rate(n, regression_pred, cumulative_num, cumulative_den):
    if cumulative_den in (0, None):
        return regression_pred
    actual = cumulative_num / cumulative_den
    if n < BLEND_GAME_THRESHOLD:
        return regression_pred * (BLEND_GAME_THRESHOLD - n) / BLEND_GAME_THRESHOLD + actual * (n / BLEND_GAME_THRESHOLD)
    return actual


def league_avg_sv_pct_today(conn, season, before_date, prior_two_season_avg):
    """Traced from ALL!SSvPct: prior-2-season league avg SV%, fading into
    this season's actual league SV% as league-wide games accumulate (N<=200
    -> pure prior; N>=25% of season's total non-playoff games -> pure
    current; linear in between)."""
    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from games where season=%s and playoff=false and date < %s", (season, before_date))
        n = cur.fetchone()[0]
        cur.execute("select count(*) from games where season=%s and playoff=false", (season,))
        total_season_games = cur.fetchone()[0]
        cur.execute(
            "select coalesce(sum(saves),0), coalesce(sum(shots_against),0) from goalie_game_appearances gga "
            "join games g on g.game_id = gga.game_id where g.season=%s and g.playoff=false and g.date < %s",
            (season, before_date),
        )
        sv, sa = cur.fetchone()
    low, high = 200, int(0.25 * total_season_games) if total_season_games else 200
    if n <= low:
        return prior_two_season_avg
    if sa == 0 or n >= high:
        return sv / sa if sa else prior_two_season_avg
    current = sv / sa
    return prior_two_season_avg * (high - n) / (high - low) + current * (n - low) / (high - low)


def compute_team_game_stats_row(conn, game_id, game_date, season, prior_season, team_code, is_home,
                                 attendance, starting_goalie_id, cf_today, ca_today, pp_pk):
    n = games_played_before(conn, team_code, season, game_date)
    rc = regression_constants(season)
    with conn.cursor() as cur:
        cur.execute(
            "select 1 from games where (home_team=%s or away_team=%s) and date = %s - interval '1 day'",
            (team_code, team_code, game_date),
        )
        back_to_back = cur.fetchone() is not None

    cf_td, ca_td = cumulative_cf_ca(conn, team_code, season, game_date)
    prior_cf = prior_season_value(conn, team_code, prior_season, "corsi_for_pct") or 0.5
    cf_pred = rc["cf_int"] + rc["cf_pycf"] * prior_cf
    corsi_for_pct = blended_rate(n, cf_pred, cf_td, cf_td + ca_td)

    pp_stats = pp_pk.get(team_code, {})
    prior_pp = prior_season_value(conn, team_code, prior_season, "pp") or 0.2
    pp_pred = rc["pp_int"] + rc["pp_pypp"] * prior_pp
    with conn.cursor() as cur:
        cur.execute(
            "select coalesce(sum(pp_goals_raw),0), coalesce(sum(pp_opp_raw),0) from team_game_stats_raw "
            "join games g using(game_id) where team_code=%s and g.season=%s and g.playoff=false and g.date < %s",
            (team_code, season, game_date),
        )
        ppg_td, ppo_td = cur.fetchone()
    pp = blended_rate(n, pp_pred, ppg_td, ppo_td)

    prior_pk = prior_season_value(conn, team_code, prior_season, "pk") or 0.8
    pk_pred = rc["pk_int"] + rc["pk_pypk"] * prior_pk
    with conn.cursor() as cur:
        cur.execute(
            "select coalesce(sum(pk_ga_raw),0), coalesce(sum(pk_opp_raw),0) from team_game_stats_raw "
            "join games g using(game_id) where team_code=%s and g.season=%s and g.playoff=false and g.date < %s",
            (team_code, season, game_date),
        )
        pkga_td, pko_td = cur.fetchone()
    pk_actual_against = (pkga_td / pko_td) if pko_td else (1 - prior_pk)
    pk = blended_rate(n, pk_pred, pko_td - pkga_td, pko_td)  # frame as "kills" for/opportunities to reuse blended_rate

    # Goalie SvPctR (Bayesian blend) — see module docstring
    sv_pct_above_expected = None
    if starting_goalie_id:
        with conn.cursor() as cur:
            cur.execute(
                "select career_shots, career_sv_pct_above_expected, league_avg_sv_pct "
                "from goalie_career_priors where player_id = %s", (starting_goalie_id,))
            prior_row = cur.fetchone()
            if prior_row is None:
                # New goalie with no NHL history in the priors table: career = 0 shots, exactly what the
                # Excel formula does (SUMIFS returns 0), so SvPctR is built from this season alone.
                cur.execute("select league_avg_sv_pct from goalie_career_priors limit 1")
                lg_row = cur.fetchone()
                prior_row = (0, 0.0, lg_row[0]) if lg_row else None
        if prior_row:
            career_shots, career_svae, league_avg = prior_row
            with conn.cursor() as cur:
                cur.execute(
                    "select coalesce(sum(shots_against),0), coalesce(sum(saves),0) from goalie_game_appearances gga "
                    "join games g on g.game_id = gga.game_id where gga.player_id=%s and g.season=%s "
                    "and g.playoff=false and g.date < %s", (starting_goalie_id, season, game_date))
                sSA, sSV = cur.fetchone()
            league_today = league_avg_sv_pct_today(conn, season, game_date, league_avg)
            denom = max(career_shots + sSA, GOALIE_SHOTS_FLOOR)
            sv_pct_above_expected = (career_svae * career_shots + sSV - league_today * sSA) / denom

    return {
        "game_id": game_id, "team_code": team_code, "home": is_home, "attendance": attendance,
        "goalie": starting_goalie_id, "corsi_for_pct": corsi_for_pct,
        "sv_pct_above_expected": sv_pct_above_expected, "pp": pp, "pk": pk,
        "back_to_back": back_to_back,
    }


# --------------------------- main ---------------------------

def upsert(conn_factory, table, rows, conflict_cols, all_cols):
    if not rows:
        print(f"  [skip] {table}: no rows")
        return
    values = [tuple(r.get(c) for c in all_cols) for r in rows]
    update_cols = [c for c in all_cols if c not in conflict_cols]
    set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols) if update_cols else None
    sql = f"INSERT INTO {table} ({', '.join(all_cols)}) VALUES %s ON CONFLICT ({', '.join(conflict_cols)}) "
    sql += f"DO UPDATE SET {set_clause}" if set_clause else "DO NOTHING"
    if DRY_RUN:
        print(f"  [dry-run] {table}: would upsert {len(values)} rows")
        return
    conn = conn_factory()
    try:
        with conn.cursor() as cur:
            execute_values(cur, sql, values)
        conn.commit()
        print(f"  [live] {table}: upserted {len(values)} rows")
    finally:
        conn.close()


# --------------------------- materialized view refresh ---------------------------

# Main chain, in dependency order. v_team_game_perspective is what carries new
# games into the bet/result views (it was missing from the original list).
MAIN_REFRESH_CHAIN = [
    "v_player_cumulative_stats", "v_pvadj", "v_team_cumulative_stats", "v_team_game_pvadjsum",
    "v_team_game_perspective", "v_team_trailing_perf", "v_lambda", "v_team_kf",
    "v_edge_threshold_by_season", "v_kelly_signal", "v_kelly_daily_return",
    "v_kelly_bank_theoretical", "v_kelly_bank_live",
]
# v2/v4 model variants and the expected-goal-difference views they use. Not part
# of the live betting chain; refreshed only when REFRESH_EXPERIMENTAL=true.
EXPERIMENTAL_REFRESH_CHAIN = [
    "v_expgd", "v_team_game_gd", "v_team_trailing_gd_error", "v_errord",
    "v_model_pct_v2", "v_model_pct_v4",
    "v_team_game_perspective_v2", "v_team_game_perspective_v4",
    "v_team_trailing_perf_v2", "v_lambda_v2",
    "v_team_kf_v2", "v_team_kf_v4",
    "v_edge_threshold_by_season_v2", "v_edge_threshold_by_season_v4",
    "v_kelly_signal_v2", "v_kelly_signal_v4",
]


def refresh_materialized_views(conn_factory):
    chain = list(MAIN_REFRESH_CHAIN) + (EXPERIMENTAL_REFRESH_CHAIN if REFRESH_EXPERIMENTAL else [])
    for view in chain:
        t0 = time.time()
        conn = conn_factory()
        try:
            with conn.cursor() as cur:
                cur.execute("set statement_timeout = 0")  # v_team_game_perspective alone takes ~2 minutes
                cur.execute(f"refresh materialized view {view}")
            conn.commit()
            print(f"  refreshed {view} ({time.time() - t0:.0f}s)")
        except Exception as e:  # stop: everything downstream would be stale/inconsistent
            conn.rollback()
            print(f"  FAILED refreshing {view}: {e}")
            raise
        finally:
            conn.close()
    print("Refreshed materialized view chain.")


# --------------------------- season / schedule helpers ---------------------------

def et_today():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return (datetime.utcnow() - timedelta(hours=5)).date()


def prior_season_code(season):
    return f"{int(season[:2]) - 1:02d}{int(season[2:]) - 1:02d}"


def season_for_date(conn, d):
    with conn.cursor() as cur:
        cur.execute("select season from season_config where season_start_date <= %s "
                    "order by season_start_date desc limit 1", (d,))
        row = cur.fetchone()
    if not row:
        raise RuntimeError(f"No season_config row starts on or before {d}.")
    return row[0]


def pending_dates(conn, season, today, lookback_days):
    """Dates in the last `lookback_days` with schedule games that still have no score."""
    with conn.cursor() as cur:
        cur.execute(
            "select distinct date from games where season=%s and playoff=false and home_goals is null "
            "and date < %s and date >= %s order by date", (season, today, today - timedelta(days=lookback_days)))
        return [r[0] for r in cur.fetchall()]


def parse_nhl_score_events(js):
    """NHL.com score feed -> list of {event_id, home, away, final, season_type} (our team codes).
    season_type: 1 preseason, 2 regular season, 3 playoffs (NHL gameType)."""
    games = js.get("games", []) or []
    if games and not any("gameState" in g for g in games):
        raise RuntimeError("NHL.com score feed has no 'gameState' field; keys seen: "
                           + ", ".join(sorted(games[0].keys())))
    out = []
    for g in games:
        home, away = g.get("homeTeam") or {}, g.get("awayTeam") or {}
        if not (home.get("abbrev") and away.get("abbrev")):
            continue
        gid = g.get("id")
        gtype = g.get("gameType")
        if gtype is None and gid:
            gtype = int(str(gid)[4:6])
        out.append({
            "event_id": gid,
            "home": normalize_team_code(str(home["abbrev"]).lower()),
            "away": normalize_team_code(str(away["abbrev"]).lower()),
            "final": str(g.get("gameState", "")).upper() in ("OFF", "FINAL"),
            "season_type": gtype,
        })
    return out


def fetch_scoreboard(d):
    r = requests.get(NHL_SCORE_URL.format(d=d.isoformat()), headers=HEADERS, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"NHL.com score feed returned HTTP {r.status_code} for {d}. First 200 chars: {r.text[:200]!r}")
    return parse_nhl_score_events(r.json())


def resolve_scheduled_game(conn, season, game_date, home, away):
    """Match a played game to its pre-loaded schedule row.
    Returns (game_id, already_ingested, note) or None if it is not on the schedule."""
    with conn.cursor() as cur:
        cur.execute("select game_id, home_goals from games where season=%s and playoff=false and date=%s "
                    "and home_team=%s and away_team=%s", (season, game_date, home, away))
        row = cur.fetchone()
        if row:
            return row[0], row[1] is not None, None
        # Possible reschedule: exactly one unplayed row for this matchup on another date
        cur.execute("select game_id, date from games where season=%s and playoff=false and home_team=%s "
                    "and away_team=%s and home_goals is null order by date", (season, home, away))
        cands = cur.fetchall()
    if len(cands) == 1:
        return cands[0][0], False, f"schedule had {cands[0][1]}, played {game_date} (schedule row will be re-dated)"
    return None


def find_playoff_game(conn, season, game_date, home, away):
    with conn.cursor() as cur:
        cur.execute("select game_id, home_goals from games where season=%s and playoff=true and date=%s "
                    "and home_team=%s and away_team=%s", (season, game_date, home, away))
        return cur.fetchone()


# --------------------------- new players / review queue ---------------------------

SKATER_COVERED_SQL = """
select
  exists (select 1 from prior_season_stats where season = %(prior)s and player_name_normalized = %(nm)s)
  or exists (select 1 from rookie_projections where season = %(season)s and player_name_normalized = %(nm)s)
  or exists (select 1 from player_aliases pa join prior_season_stats s
              on s.player_name_normalized = pa.alias_name_normalized and s.season = %(prior)s
             where pa.player_id = %(pid)s)
  or exists (select 1 from player_aliases pa join rookie_projections r
              on r.player_name_normalized = pa.alias_name_normalized and r.season = %(season)s
             where pa.player_id = %(pid)s)
"""


def existing_player_ids(conn, ids):
    if not ids:
        return set()
    with conn.cursor() as cur:
        cur.execute("select player_id from players where player_id = any(%s)", (list(ids),))
        return {r[0] for r in cur.fetchall()}


def insert_new_players(conn_factory, new_players):
    if not new_players:
        return
    if DRY_RUN:
        print(f"  [dry-run] players: would insert {len(new_players)} new: "
              + ", ".join(f"{n} ({p})" for p, n in list(new_players.items())[:10]))
        return
    conn = conn_factory()
    try:
        with conn.cursor() as cur:
            execute_values(cur, "insert into players (player_id, player_name_display) values %s "
                                "on conflict (player_id) do nothing", list(new_players.items()))
        conn.commit()
        print(f"  [live] players: inserted {len(new_players)} new")
    finally:
        conn.close()


def players_needing_review(conn, season, prior_season, skaters, goalies, new_ids):
    """Skaters with no prior-season stats / rookie row. (Goalies with no career prior need no
    action: SvPctR treats them as zero career shots, as the Excel does.)"""
    out, seen = [], set()
    goalie_ids = {g["player_id"] for g in goalies}
    for r in skaters:
        pid = r["player_id"]
        if pid in seen or pid in goalie_ids:
            continue
        seen.add(pid)
        nm = (r.get("player_name_display") or "").strip().lower()
        with conn.cursor() as cur:
            cur.execute(SKATER_COVERED_SQL, {"pid": pid, "nm": nm, "prior": prior_season, "season": season})
            covered = cur.fetchone()[0]
        if not covered:
            out.append({"player_id": pid, "player_name_display": r.get("player_name_display"),
                        "team_code": r["team_code"], "game_id": r["game_id"],
                        "reason": "no prior-season stats or rookie entry", "is_new": pid in new_ids})
    return out


def report_review(conn_factory, review_rows, game_dates):
    if not review_rows:
        return
    lines = ["ACTION NEEDED - skaters with no prior-season stats or rookie entry:"]
    for r in review_rows:
        lines.append(f"  {r['player_name_display']} ({r['player_id']}, {r['team_code'].upper()}, first seen {r['game_id']}) "
                     f"- {r['reason']}{' [new to database]' if r['is_new'] else ''}")
    text = "\n".join(lines)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### " + lines[0] + "\n\n" + "\n".join("- " + l.strip() for l in lines[1:]) + "\n")
    if DRY_RUN:
        print("  [dry-run] player_review_queue: would record the above")
        return
    values = [(r["player_id"], r["player_name_display"], r["team_code"], r["game_id"], game_dates[r["game_id"]], r["reason"])
              for r in review_rows]
    conn = conn_factory()
    try:
        with conn.cursor() as cur:
            execute_values(cur, "insert into player_review_queue "
                                "(player_id, player_name_display, team_code, first_game_id, first_game_date, reason) "
                                "values %s on conflict (player_id) do nothing", values)
        conn.commit()
    finally:
        conn.close()


# --------------------------- confirmed-snapshot overwrite ---------------------------

def overwrite_confirmed_snapshots(conn_factory, confirmed_lineup_info):
    """Replaces the 'confirmed' lineup_snapshots/goalie_snapshots rows for each played team-game with the
    ACTUAL roster from this game's box score -- the post-game result is definitive, so it overwrites
    whatever DailyFaceoff's own 'confirmed' capture (nhl_lineup_goalie_ingest.py) projected: players who
    were projected but did not dress are removed, and anyone who played but wasn't projected (a call-up,
    a last-second swap) is added. 'preliminary' rows are never touched. See nhl_lineup_goalie_ingest.py's
    docstring for the full reasoning (Nick's instruction: a 'confirmed' snapshot must still be correctable
    once we know who actually played)."""
    if not confirmed_lineup_info:
        return
    if DRY_RUN:
        print(f"[dry-run] would overwrite 'confirmed' lineup/goalie snapshots for {len(confirmed_lineup_info)} team-game(s) "
              "with the actual roster")
        return
    conn = conn_factory()
    try:
        now = datetime.now(timezone.utc)
        with conn.cursor() as cur:
            for info in confirmed_lineup_info:
                game_id, team_code = info["game_id"], info["team_code"]
                cur.execute("delete from lineup_snapshots where game_id = %s and team_code = %s "
                            "and snapshot_type = 'confirmed'", (game_id, team_code))
                if info["skater_ids"]:
                    execute_values(
                        cur, "insert into lineup_snapshots (game_id, team_code, player_id, snapshot_type, scraped_at) "
                            "values %s",
                        [(game_id, team_code, pid, "confirmed", now) for pid in info["skater_ids"]])
                cur.execute("delete from goalie_snapshots where game_id = %s and team_code = %s "
                            "and snapshot_type = 'confirmed'", (game_id, team_code))
                if info["goalie_id"]:
                    cur.execute(
                        "insert into goalie_snapshots (game_id, team_code, player_id, snapshot_type, scraped_at, dfo_status) "
                        "values (%s, %s, %s, 'confirmed', %s, %s)",
                        (game_id, team_code, info["goalie_id"], now, "actual (post-game)"))
        conn.commit()
        print(f"[live] overwrote 'confirmed' lineup/goalie snapshots for {len(confirmed_lineup_info)} team-game(s) "
              "with the actual roster.")
    finally:
        conn.close()


# --------------------------- one date ---------------------------

def process_date(conn, d, season, prior_season):
    """Ingest every completed, scheduled game on date `d` that has no score yet.
    Returns (games_ingested, failures)."""
    events = fetch_scoreboard(d)
    plan, playoff_events, failures = [], [], []
    for e in events:
        if not e["final"]:
            print(f"  skip {e['away']} @ {e['home']}: not final")
            continue
        res = resolve_scheduled_game(conn, season, d, e["home"], e["away"])
        if res:
            game_id, done, note = res
            if done:
                print(f"  skip {e['away']} @ {e['home']}: already ingested ({game_id})")
                continue
            if note:
                print(f"  NOTE {e['away']} @ {e['home']} {game_id}: {note}")
            plan.append({**e, "game_id": game_id, "playoff": False})
        elif e["season_type"] == 3:
            existing = find_playoff_game(conn, season, d, e["home"], e["away"])
            if existing and existing[1] is not None:
                continue
            if existing:
                plan.append({**e, "game_id": existing[0], "playoff": True})
            else:
                playoff_events.append(e)
        else:
            print(f"  skip {e['away']} @ {e['home']}: not on the regular-season schedule (preseason or other)")
    if playoff_events:
        ids = next_game_ids(conn, season, len(playoff_events), playoff=True)
        plan += [{**e, "game_id": gid, "playoff": True} for e, gid in zip(playoff_events, ids)]
    if not plan:
        return 0, failures

    all_skaters, all_goalies, all_advanced, all_games, all_team_stats, all_raw = [], [], [], [], [], []
    confirmed_lineup_info = []  # [{'game_id','team_code','skater_ids','goalie_id'}] -- actual post-game roster,
                                 # used to overwrite the 'confirmed' lineup/goalie snapshot with definitive truth
    for g in plan:
        home_code, away_code, game_id = g["home"], g["away"], g["game_id"]
        print(f"Scraping {away_code} @ {home_code} -> {game_id}")
        try:
            box = scrape_game_box(game_id, d, home_code, away_code)
        except Exception as e:
            print(f"  FAILED {game_id}: {e} (will be retried on the next run)")
            failures.append((d, game_id))
            continue
        pp_pk = fetch_pp_pk(g["event_id"], home_code, away_code)
        if not pp_pk:
            print(f"  WARNING {game_id}: NHL.com PP/PK not found; PP/PK raw values left empty for this game")

        all_games.append({
            "game_id": game_id, "season": season, "date": d, "home_team": home_code, "away_team": away_code,
            "playoff": g["playoff"], "home_goals": box["home_goals"], "away_goals": box["away_goals"],
        })
        all_skaters += box["skaters"]
        all_goalies += box["goalies"]
        all_advanced += box["advanced"]

        for team_code, is_home in ((home_code, True), (away_code, False)):
            starting_goalie = next(
                (gl["player_id"] for gl in box["goalies"]
                 if gl["team_code"] == team_code and gl.get("toi_seconds")
                 and gl["toi_seconds"] == max(
                     (gg["toi_seconds"] or 0) for gg in box["goalies"] if gg["team_code"] == team_code)),
                None,
            )
            team_total = box["team_totals"].get(team_code, {})
            all_team_stats.append(compute_team_game_stats_row(
                conn, game_id, d, season, prior_season, team_code, is_home,
                box["attendance"], starting_goalie, team_total.get("cf"), team_total.get("ca"), pp_pk,
            ))
            pp = pp_pk.get(team_code, {})
            all_raw.append({
                "game_id": game_id, "team_code": team_code,
                "cf_raw": team_total.get("cf"), "ca_raw": team_total.get("ca"),
                "pp_goals_raw": pp.get("pp_goals"), "pp_opp_raw": pp.get("pp_opp"),
                "pk_ga_raw": pp.get("pk_goals_against"), "pk_opp_raw": pp.get("pk_opp"),
            })
            confirmed_lineup_info.append({
                "game_id": game_id, "team_code": team_code,
                "skater_ids": [r["player_id"] for r in box["skaters"] if r["team_code"] == team_code],
                "goalie_id": starting_goalie,
            })
        time.sleep(REQUEST_DELAY_SECONDS)

    if not all_games:
        return 0, failures

    # New players must exist before anything that references them.
    names = {r["player_id"]: r.get("player_name_display") for r in all_skaters + all_goalies}
    known = existing_player_ids(conn, list(names))
    new_players = {pid: nm for pid, nm in names.items() if pid not in known}
    insert_new_players(get_db_conn, new_players)

    upsert(get_db_conn, "games", all_games, ["game_id"],
           ["game_id", "season", "date", "home_team", "away_team", "playoff", "home_goals", "away_goals"])
    upsert(get_db_conn, "team_game_stats", all_team_stats, ["game_id", "team_code"],
           ["game_id", "team_code", "home", "attendance", "goalie", "corsi_for_pct",
            "sv_pct_above_expected", "pp", "pk", "back_to_back"])
    upsert(get_db_conn, "team_game_stats_raw", all_raw, ["game_id", "team_code"],
           ["game_id", "team_code", "cf_raw", "ca_raw", "pp_goals_raw", "pp_opp_raw", "pk_ga_raw", "pk_opp_raw"])
    upsert(get_db_conn, "player_game_appearances", all_skaters, ["game_id", "player_id"],
           ["game_id", "player_id", "team_code", "goals", "assists", "points", "plus_minus", "pim",
            "ev_goals", "pp_goals", "sh_goals", "gw_goals", "ev_assists", "pp_assists", "sh_assists",
            "shots", "shot_pct", "shifts", "toi_seconds"])
    upsert(get_db_conn, "goalie_game_appearances", all_goalies, ["game_id", "player_id"],
           ["game_id", "player_id", "team_code", "decision", "goals_against", "shots_against",
            "saves", "sv_pct", "shutout", "toi_seconds"])
    upsert(get_db_conn, "player_advanced_game_appearances", all_advanced, ["game_id", "player_id"],
           ["game_id", "player_id", "team_code", "icf", "sat_for", "sat_against", "cf_pct",
            "crel_pct", "zone_start_off", "zone_start_def", "off_zone_start_pct", "hits", "blocks"])

    overwrite_confirmed_snapshots(get_db_conn, confirmed_lineup_info)

    review = players_needing_review(conn, season, prior_season, all_skaters, all_goalies, set(new_players))
    report_review(get_db_conn, review, {g["game_id"]: g["date"] for g in all_games})
    return len(all_games), failures


# --------------------------- main ---------------------------

def main():
    today = et_today()
    explicit = os.environ.get("INGEST_DATE")
    print(f"NHL daily results ingest — DRY_RUN={DRY_RUN} — today (ET) {today}")

    if not os.environ.get("PGHOST"):
        print("No PGHOST set — nothing to do.")
        return

    conn = get_db_conn()
    total, failures = 0, []
    try:
        if explicit:
            dates = [date.fromisoformat(explicit)]
        else:
            yesterday = today - timedelta(days=1)
            season_now = season_for_date(conn, yesterday)
            dates = sorted(set(pending_dates(conn, season_now, today, LOOKBACK_DAYS)) | {yesterday})
        print("Dates to process: " + ", ".join(str(x) for x in dates))

        for d in dates:
            season = season_for_date(conn, d)
            prior_season = prior_season_code(season)
            print(f"== {d} (season {season}, prior {prior_season}) ==")
            n, fails = process_date(conn, d, season, prior_season)
            total += n
            failures += fails
            print(f"  {n} game(s) ingested for {d}")

        if total and not DRY_RUN:
            refresh_materialized_views(get_db_conn)
    finally:
        conn.close()

    if failures:
        print("WARNING - games that could not be ingested (retried automatically while inside the lookback window):")
        for d, gid in failures:
            print(f"  {d} {gid}")
        if any(d <= today - timedelta(days=2) for d, _ in failures):
            print("A failure is more than 2 days old - exiting with an error so it is not missed.")
            raise SystemExit(1)
    print("Done.")


if __name__ == "__main__":
    main()
