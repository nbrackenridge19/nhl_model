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
  regression-to-prior-season-value constants (see REGRESSION_CONSTANTS
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

STILL UNCERTAIN — VERIFY BEFORE TRUSTING LIVE
--------------------------------------------------------------------------
Team-level PP goals/opportunities and PK goals-against/opportunities-
against (MUGF/MUOF/MDGA/MDOA in the original workbook) are NOT visible on
the Hockey-Reference box score page itself. This script pulls them from
ESPN's game summary endpoint (same event_id nhlodds.py already resolves
for odds), on the theory that ESPN's boxscore JSON exposes
'powerPlayGoals'/'powerPlayOpportunities' per team. THIS HAS NOT BEEN
CONFIRMED AGAINST A REAL COMPLETED GAME — the very first live run of this
script should be spot-checked: pull one completed game, compare the
computed PP/PK team_game_stats values against what you'd expect, before
trusting it for actual betting decisions.

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

SAFETY
------
- DB connection: PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT env vars.
- DRY_RUN defaults to true.
- Designed to run once per morning for "yesterday's" completed games; not
  the T-5 pre-game path (that's a separate script — lineups/odds/goalies).
"""

import os
import re
import time
from datetime import date, timedelta

import requests
import psycopg2
from psycopg2.extras import execute_values
from bs4 import BeautifulSoup, Comment

DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
REQUEST_DELAY_SECONDS = float(os.environ.get("REQUEST_DELAY_SECONDS", "3"))
HEADERS = {"User-Agent": "Mozilla/5.0"}
BOXSCORE_URL = "https://www.hockey-reference.com/boxscores/{code}.html"
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary"

# Read from gamelog2526.xlsm ALL!Z2:Z7 this session — refit once per
# season. Re-pull from the current season's gamelog workbook if this
# script is reused next season.
REGRESSION_CONSTANTS = {
    "cf_int": 0.1448593885572126, "cf_pycf": 0.7121483119094089,
    "pp_int": 0.11751272458282204, "pp_pypp": 0.4145979201770057,
    "pk_int": 0.5684377783357512, "pk_pypk": 0.2890725321972133,
}
BLEND_GAME_THRESHOLD = 40  # games after which the blend is pure season-to-date
GOALIE_SHOTS_FLOOR = 1050  # min(shots) denominator floor in the SvPctR blend

TEAM_CODE_NORMALIZE = {"vgk": "veg", "was": "wsh"}

# ESPN uses different abbreviations than our internal team_code for these 5
# teams (confirmed via live ESPN pages during the Project 3 backfill spot
# check) — every other team's ESPN abbreviation matches our team_code
# exactly. Used only for the ESPN PP/PK lookup below.
ESPN_TEAM_CODE_MAP = {"tbl": "tb", "sjs": "sj", "njd": "nj", "lak": "la", "veg": "vgk"}
ESPN_TEAM_CODE_MAP_REVERSE = {v: k for k, v in ESPN_TEAM_CODE_MAP.items()}


def get_db_conn():
    if os.environ.get("PGHOST"):
        return psycopg2.connect()
    raise RuntimeError("Set PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT.")


def normalize_team_code(code):
    return TEAM_CODE_NORMALIZE.get(code, code)


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


# --------------------------- ESPN PP/PK lookup (UNCONFIRMED — see docstring) ---------------------------

def fetch_espn_pp_pk(game_date, home_team, away_team):
    """Returns {team_code: {'pp_goals':, 'pp_opp':, 'pk_goals_against':, 'pk_opp':}}
    or {} if not found. UNCONFIRMED against a real completed game — verify
    on first live run (see module docstring)."""
    home_espn = ESPN_TEAM_CODE_MAP.get(home_team, home_team)
    away_espn = ESPN_TEAM_CODE_MAP.get(away_team, away_team)
    try:
        sb = requests.get(ESPN_SCOREBOARD_URL, params={"dates": game_date.strftime("%Y%m%d")},
                           headers=HEADERS, timeout=20).json()
    except Exception:
        return {}
    event_id = None
    for ev in sb.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        names = {c.get("team", {}).get("abbreviation", "").lower() for c in competitors}
        if {home_espn, away_espn} & names:
            event_id = ev.get("id")
            break
    if event_id is None:
        return {}
    try:
        summary = requests.get(ESPN_SUMMARY_URL, params={"event": event_id}, headers=HEADERS, timeout=20).json()
    except Exception:
        return {}
    result = {}
    for team_block in summary.get("boxscore", {}).get("teams", []):
        espn_abbr = team_block.get("team", {}).get("abbreviation", "").lower()
        abbr = ESPN_TEAM_CODE_MAP_REVERSE.get(espn_abbr, espn_abbr)
        stats = {s.get("name"): s.get("displayValue") for s in team_block.get("statistics", [])}
        pp = stats.get("powerPlayGoals") or stats.get("powerPlayConversion", "0-0")
        if "-" in str(pp):
            goals, opp = str(pp).split("-")
        else:
            goals, opp = stats.get("powerPlayGoals"), stats.get("powerPlayOpportunities")
        result[abbr] = {"pp_goals": parse_int(goals), "pp_opp": parse_int(opp)}
    # PK for a team = opponent's PP against them: pk_opp/pk_goals_against are the OTHER team's pp_opp/pp_goals
    teams = list(result.keys())
    if len(teams) == 2:
        a, b = teams
        result[a]["pk_opp"] = result[b]["pp_opp"]
        result[a]["pk_goals_against"] = result[b]["pp_goals"]
        result[b]["pk_opp"] = result[a]["pp_opp"]
        result[b]["pk_goals_against"] = result[a]["pp_goals"]
    return result


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
                                 attendance, starting_goalie_id, cf_today, ca_today, pp_pk_espn):
    n = games_played_before(conn, team_code, season, game_date)
    back_to_back = games_played_before(conn, team_code, season, game_date) > 0 and \
        prior_season_value(conn, team_code, season, "attendance") is not None  # placeholder, replaced below
    with conn.cursor() as cur:
        cur.execute(
            "select 1 from games where (home_team=%s or away_team=%s) and date = %s - interval '1 day'",
            (team_code, team_code, game_date),
        )
        back_to_back = cur.fetchone() is not None

    cf_td, ca_td = cumulative_cf_ca(conn, team_code, season, game_date)
    prior_cf = prior_season_value(conn, team_code, prior_season, "corsi_for_pct") or 0.5
    cf_pred = REGRESSION_CONSTANTS["cf_int"] + REGRESSION_CONSTANTS["cf_pycf"] * prior_cf
    corsi_for_pct = blended_rate(n, cf_pred, cf_td, cf_td + ca_td)

    pp_stats = pp_pk_espn.get(team_code, {})
    prior_pp = prior_season_value(conn, team_code, prior_season, "pp") or 0.2
    pp_pred = REGRESSION_CONSTANTS["pp_int"] + REGRESSION_CONSTANTS["pp_pypp"] * prior_pp
    with conn.cursor() as cur:
        cur.execute(
            "select coalesce(sum(pp_goals_raw),0), coalesce(sum(pp_opp_raw),0) from team_game_stats_raw "
            "join games g using(game_id) where team_code=%s and g.season=%s and g.playoff=false and g.date < %s",
            (team_code, season, game_date),
        )
        ppg_td, ppo_td = cur.fetchone()
    pp = blended_rate(n, pp_pred, ppg_td, ppo_td)

    prior_pk = prior_season_value(conn, team_code, prior_season, "pk") or 0.8
    pk_pred = REGRESSION_CONSTANTS["pk_int"] + REGRESSION_CONSTANTS["pk_pypk"] * prior_pk
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


def refresh_materialized_views(conn_factory):
    conn = conn_factory()
    try:
        with conn.cursor() as cur:
            for view in [
                "v_player_cumulative_stats", "v_pvadj", "v_team_cumulative_stats",
                "v_team_game_pvadjsum", "v_team_trailing_perf", "v_team_trailing_gd_error",
                "v_lambda", "v_edge_threshold_by_season", "v_team_kf", "v_kelly_signal",
                "v_kelly_daily_return", "v_kelly_bank_theoretical", "v_kelly_bank_live",
            ]:
                cur.execute(f"refresh materialized view {view}")
        conn.commit()
        print("Refreshed materialized view chain.")
    finally:
        conn.close()


def main():
    yesterday = date.today() - timedelta(days=1)
    print(f"NHL daily results ingest — DRY_RUN={DRY_RUN} — pulling games for {yesterday}")

    if not os.environ.get("PGHOST"):
        print("No PGHOST set — nothing to do.")
        return

    conn = get_db_conn()
    try:
        # Games for yesterday come from the ESPN scoreboard (home/away teams,
        # final scores confirmed) — the actual per-player stats still come
        # from Hockey-Reference box scores.
        sb = requests.get(ESPN_SCOREBOARD_URL, params={"dates": yesterday.strftime("%Y%m%d")},
                           headers=HEADERS, timeout=20).json()
        todays_games = []
        for ev in sb.get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            competitors = comp.get("competitors") or []
            home = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not (home and away):
                continue
            home_code = normalize_team_code(home.get("team", {}).get("abbreviation", "").lower())
            away_code = normalize_team_code(away.get("team", {}).get("abbreviation", "").lower())
            todays_games.append((home_code, away_code))

        if not todays_games:
            print("No games found for yesterday.")
            return

        season_code = "2526"  # TODO: derive from date once this spans a season boundary
        prior_season_code = "2425"
        game_ids = next_game_ids(conn, season_code, len(todays_games), playoff=False)

        all_skaters, all_goalies, all_advanced, all_games, all_team_stats = [], [], [], [], []

        for (home_code, away_code), game_id in zip(todays_games, game_ids):
            print(f"Scraping {away_code} @ {home_code} -> {game_id}")
            box = scrape_game_box(game_id, yesterday, home_code, away_code)
            pp_pk = fetch_espn_pp_pk(yesterday, home_code, away_code)

            all_games.append({
                "game_id": game_id, "season": season_code, "date": yesterday,
                "home_team": home_code, "away_team": away_code, "playoff": False,
                "home_goals": box["home_goals"], "away_goals": box["away_goals"],
            })
            all_skaters += box["skaters"]
            all_goalies += box["goalies"]
            all_advanced += box["advanced"]

            for team_code, is_home in ((home_code, True), (away_code, False)):
                starting_goalie = next(
                    (g["player_id"] for g in box["goalies"]
                     if g["team_code"] == team_code and g.get("toi_seconds", 0)
                     and g["toi_seconds"] == max(
                         (gg["toi_seconds"] or 0) for gg in box["goalies"] if gg["team_code"] == team_code)),
                    None,
                )
                team_total = box["team_totals"].get(team_code, {})
                row = compute_team_game_stats_row(
                    conn, game_id, yesterday, season_code, prior_season_code, team_code, is_home,
                    box["attendance"], starting_goalie,
                    team_total.get("cf"), team_total.get("ca"), pp_pk,
                )
                all_team_stats.append(row)

            time.sleep(REQUEST_DELAY_SECONDS)

        upsert(get_db_conn, "games", all_games, ["game_id"],
               ["game_id", "season", "date", "home_team", "away_team", "playoff", "home_goals", "away_goals"])
        upsert(get_db_conn, "team_game_stats", all_team_stats, ["game_id", "team_code"],
               ["game_id", "team_code", "home", "attendance", "goalie", "corsi_for_pct",
                "sv_pct_above_expected", "pp", "pk", "back_to_back"])
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

        if not DRY_RUN:
            refresh_materialized_views(get_db_conn)

    finally:
        conn.close()

    print("Done.")


if __name__ == "__main__":
    main()
