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
- "Current bankroll" in the header: v_kelly_bank_live -- the live-era
  series (anchored at 2025-26's start), not the full 2017-18-onward
  theoretical one, since that's the number that actually matters for
  "how much money is available right now."

SAFETY
--------------------------------------------------------------------------
Read-only against the database -- this script never writes to Supabase.
Its own side effect is purely local: it writes docs/index.html, which the
workflow then commits only if the content actually changed.
"""

import os
from datetime import date, datetime, timedelta, timezone

import psycopg2

OUTPUT_PATH = "docs/index.html"


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


# --------------------------- header ---------------------------

def get_current_model_version(conn):
    with conn.cursor() as cur:
        cur.execute("select model_version_id, effective_from from model_versions "
                    "where model_type='base_logit' order by effective_from desc limit 1")
        return cur.fetchone()


def get_live_bankroll(conn):
    with conn.cursor() as cur:
        cur.execute("select date, bank_start_of_date from v_kelly_bank_live order by date desc limit 1")
        row = cur.fetchone()
        if row is None:
            return None
        last_date, bank_before = row
        cur.execute("select daily_return_pct from v_kelly_daily_return where date=%s", (last_date,))
        ret_row = cur.fetchone()
    daily_return = ret_row[0] if ret_row else 0
    return float(bank_before) * (1 + float(daily_return))


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


# --------------------------- historical instances ---------------------------

def get_all_seasons(conn):
    with conn.cursor() as cur:
        cur.execute("select distinct season from v_kelly_signal order by season desc")
        return [r[0] for r in cur.fetchall()]


def get_detail_season(conn):
    override = os.environ.get("DETAIL_SEASON", "").strip()
    if override:
        return override
    with conn.cursor() as cur:
        cur.execute("select season from v_kelly_signal order by season desc limit 1")
        row = cur.fetchone()
    return row[0] if row else None


def get_instances(conn, seasons=None):
    """One row per team-game-instance, for the given seasons (or all if None).
    Every dollar figure uses v_kelly_bank_theoretical's bankroll for that date --
    one consistent all-history compounding series, not a per-season reset."""
    query = """
        select p.game_id, p.team, p.opp, p.season, p.date, p.home, p.win, p.model_pct, p.mlpct,
               p.logloss, p.vlogloss, k.kf, k.bets_fire, sc.kelly_fraction, l.lambda,
               bt.bank_start_of_date
        from v_team_game_perspective p
        join v_kelly_signal k on k.game_id=p.game_id and k.team=p.team
        join v_lambda l on l.game_id=p.game_id and l.team=p.team
        join season_config sc on sc.season=p.season
        left join v_kelly_bank_theoretical bt on bt.season=p.season and bt.date=p.date
    """
    params = ()
    if seasons:
        query += " where p.season = any(%s)"
        params = (list(seasons),)
    query += " order by p.date"
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    instances = []
    for (game_id, team, opp, season, gdate, home, win, model_pct, mlpct, logloss, vlogloss,
         kf, bets_fire, kelly_fraction, lam, bank) in rows:
        stake_dollar = profit_dollar = 0.0
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
        })
    return instances


def season_start_dates(conn):
    with conn.cursor() as cur:
        cur.execute("select season, season_start_date from season_config")
        return {r[0]: r[1] for r in cur.fetchall()}


def calendar_week(game_date, season_start):
    return (game_date - season_start).days // 7 + 1


# --------------------------- aggregation ---------------------------

def aggregate(instances):
    fired = [x for x in instances if x["bets_fire"]]
    wins = sum(1 for x in fired if x["win"])
    losses = len(fired) - wins
    wagered = sum(x["stake_dollar"] for x in fired)
    profit = sum(x["profit_dollar"] for x in fired)
    ll = [x["logloss"] for x in instances if x["logloss"] is not None]
    vll = [x["vlogloss"] for x in instances if x["vlogloss"] is not None]
    return {
        "n_instances": len(instances), "bets_placed": len(fired), "wins": wins, "losses": losses,
        "wagered": wagered, "profit": profit,
        "return_pct": (profit / wagered) if wagered else None,
        "model_logloss": (sum(ll) / len(ll)) if ll else None,
        "market_logloss": (sum(vll) / len(vll)) if vll else None,
    }


def group_by(instances, key_fn):
    groups = {}
    for x in instances:
        groups.setdefault(key_fn(x), []).append(x)
    return groups


# --------------------------- rendering ---------------------------

STYLE = (
    "body { font-family: -apple-system, sans-serif; max-width: 1100px; margin: 0 auto; "
    "padding: 16px; background: #fafafa; } "
    "h1 { font-size: 20px; } h2 { font-size: 16px; margin-top: 28px; } "
    ".meta { color: #666; font-size: 13px; margin-bottom: 16px; } "
    "table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; "
    "overflow: hidden; margin-bottom: 8px; } "
    "th, td { padding: 10px 8px; text-align: left; font-size: 14px; border-bottom: 1px solid #eee; } "
    "th { background: #f0f0f0; font-size: 12px; text-transform: uppercase; } "
    ".tag { color: #999; font-size: 11px; } "
    ".block { background: white; border-radius: 8px; margin-bottom: 8px; overflow: hidden; } "
    ".block summary { padding: 12px; cursor: pointer; list-style: none; display: flex; "
    "flex-wrap: wrap; gap: 4px 16px; align-items: center; font-size: 13px; } "
    ".block summary::-webkit-details-marker { display: none; } "
    ".block summary::before { content: '\\25B8'; margin-right: 4px; color: #999; } "
    ".block[open] summary::before { content: '\\25BE'; } "
    ".block-title { font-weight: 700; font-size: 14px; margin-right: 4px; } "
    ".block-stat { color: #444; } "
    "table.detail { border-radius: 0; margin: 0; box-shadow: none; } "
    "table.detail th, table.detail td { padding: 8px; } "
    "@media (max-width: 480px) { th, td { font-size: 12px; padding: 8px 4px; } }"
)


def logloss_delta_bg(delta, scale=0.04):
    if delta is None:
        return ""
    intensity = min(abs(delta) / scale, 1.0)
    alpha = 0.10 + intensity * 0.55
    rgb = "26,127,55" if delta < 0 else "192,57,43"
    return f"background:rgba({rgb},{alpha:.2f});"


def money(v):
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def summary_row_html(label, s, bold=False):
    win_pct = f"{s['wins'] / s['bets_placed']:.0%}" if s["bets_placed"] else "-"
    profit_color = "#1a7f37" if s["profit"] >= 0 else "#c0392b"
    return_str = f"{s['return_pct']:+.1%}" if s["return_pct"] is not None else "-"
    model_ll = f"{s['model_logloss']:.3f}" if s["model_logloss"] is not None else "-"
    market_ll = f"{s['market_logloss']:.3f}" if s["market_logloss"] is not None else "-"
    delta = (s["model_logloss"] - s["market_logloss"]) if (
        s["model_logloss"] is not None and s["market_logloss"] is not None) else None
    delta_str = f"{delta:+.3f}" if delta is not None else "-"
    style = "font-weight:700; border-top:2px solid #ccc;" if bold else ""
    return (
        f"<tr style=\"{style}\">"
        f"<td>{label}</td><td>{s['bets_placed']}</td>"
        f"<td>{s['wins']}-{s['losses']} ({win_pct})</td>"
        f"<td>${s['wagered']:,.2f}</td>"
        f"<td><span style=\"color:{profit_color}; font-weight:600;\">{money(s['profit'])}</span></td>"
        f"<td>{return_str}</td><td>{model_ll}</td><td>{market_ll}</td>"
        f"<td style=\"{logloss_delta_bg(delta)}\">{delta_str}</td>"
        "</tr>"
    )


def summary_table_html(header_label, rows_html):
    return (
        f"<table><tr><th>{header_label}</th><th>Bets</th><th>Record</th><th>Wagered</th>"
        "<th>Profit</th><th>Return</th><th>LogLoss</th><th>Mkt LogLoss</th><th>LL &Delta;</th></tr>"
        f"{rows_html}</table>"
    )


def render_todays_games(rows):
    if not rows:
        return "<p class=\"meta\">No games scheduled today.</p>"
    html = ""
    for (game_id, home, away, h_fire, h_stake, h_mpct, h_mlpct, h_snap,
         a_fire, a_stake, a_mpct, a_mlpct, a_snap) in rows:
        for team, opp, is_home, fire, stake, mpct, mlpct, snap in (
            (home, away, True, h_fire, h_stake, h_mpct, h_mlpct, h_snap),
            (away, home, False, a_fire, a_stake, a_mpct, a_mlpct, a_snap),
        ):
            if mpct is None:
                html += (
                    "<tr><td>" + f"{away.upper()} @ {home.upper()}" + "</td>"
                    f"<td>{team.upper()} {'(H)' if is_home else '(A)'}</td>"
                    "<td colspan=\"5\" class=\"tag\">awaiting odds/lineup data -- no signal yet</td></tr>"
                )
                continue
            mpct, mlpct = float(mpct), float(mlpct)
            delta = mpct - mlpct
            delta_color = "#1a7f37" if delta > 0 else ("#c0392b" if delta < 0 else "#666")
            bet_str = "BET" if fire else "no"
            bet_color = "#1a7f37" if fire else "#666"
            stake_str = f"${float(stake):,.2f}" if fire and stake is not None else "-"
            html += (
                "<tr>"
                f"<td>{away.upper()} @ {home.upper()}</td>"
                f"<td>{team.upper()} {'(H)' if is_home else '(A)'} "
                f"<span class=\"tag\">({snap})</span></td>"
                f"<td><span style=\"color:{bet_color}; font-weight:600;\">{bet_str}</span></td>"
                f"<td>{stake_str}</td>"
                f"<td>{mpct:.1%}</td>"
                f"<td>{mlpct:.1%}</td>"
                f"<td style=\"color:{delta_color}; font-weight:600;\">{delta:+.1%}</td>"
                "</tr>"
            )
    return (
        "<table><tr><th>Matchup</th><th>Team</th><th>Bet?</th><th>$ Amount</th>"
        "<th>Model %</th><th>Market %</th><th>Delta</th></tr>" + html + "</table>"
    )


def render_drilldown(title_prefix, groups_sorted, label_fn):
    """groups_sorted: list of (key, instances) already in display order."""
    html = ""
    for key, instances in groups_sorted:
        s = aggregate(instances)
        win_pct = f"{s['wins']}-{s['losses']}" if s["bets_placed"] else "0-0"
        profit_color = "#1a7f37" if s["profit"] >= 0 else "#c0392b"
        model_ll = f"{s['model_logloss']:.3f}" if s["model_logloss"] is not None else "-"
        market_ll = f"{s['market_logloss']:.3f}" if s["market_logloss"] is not None else "-"
        detail_rows = ""
        for x in sorted(instances, key=lambda x: (x["date"], x["game_id"], not x["home"])):
            mpct = f"{x['model_pct']:.1%}" if x["model_pct"] is not None else "-"
            mktpct = f"{x['mlpct']:.1%}" if x["mlpct"] is not None else "-"
            if x["bets_fire"]:
                result_str = "WON" if x["win"] else "lost"
                result_color = "#1a7f37" if x["win"] else "#c0392b"
                wager_str = f"${x['stake_dollar']:,.2f}"
            else:
                result_str, result_color, wager_str = "-", "#999", "-"
            detail_rows += (
                "<tr>"
                f"<td>{x['date']}</td><td>{x['team'].upper()} {'(H)' if x['home'] else '(A)'}</td>"
                f"<td>{x['opp'].upper()}</td><td>{mpct}</td><td>{mktpct}</td>"
                f"<td>{wager_str}</td>"
                f"<td><span style=\"color:{result_color}; font-weight:600;\">{result_str}</span></td>"
                "</tr>"
            )
        html += (
            "<details class=\"block\"><summary>"
            f"<span class=\"block-title\">{(title_prefix + ' ' + label_fn(key)).strip()}</span>"
            f"<span class=\"block-stat\">{win_pct} ({s['bets_placed']} bets)</span>"
            f"<span class=\"block-stat\" style=\"color:{profit_color}; font-weight:600;\">{money(s['profit'])}</span>"
            f"<span class=\"block-stat\">LL {model_ll} / Mkt {market_ll}</span>"
            "</summary>"
            "<table class=\"detail\"><tr><th>Date</th><th>Team</th><th>Opp</th>"
            "<th>Model %</th><th>Market %</th><th>Wager</th><th>Result</th></tr>"
            f"{detail_rows}</table></details>"
        )
    return html


def render_html(model_version, bankroll, todays_rows, detail_season, week_groups, team_groups, season_summaries):
    season_disp = f"{detail_season[:2]}-{detail_season[2:]}"

    week_html = render_drilldown("Week", week_groups, lambda k: str(k))
    team_html = render_drilldown("", team_groups, lambda k: k.upper())

    season_rows_html = ""
    totals = {"n_instances": 0, "bets_placed": 0, "wins": 0, "losses": 0, "wagered": 0.0, "profit": 0.0,
              "model_logloss": None, "market_logloss": None}
    ll_sum = ll_n = vll_sum = vll_n = 0
    for season, s in season_summaries:
        disp = f"{season[:2]}-{season[2:]}"
        season_rows_html += summary_row_html(disp, s)
        totals["bets_placed"] += s["bets_placed"]; totals["wins"] += s["wins"]; totals["losses"] += s["losses"]
        totals["wagered"] += s["wagered"]; totals["profit"] += s["profit"]
        if s["model_logloss"] is not None:
            ll_sum += s["model_logloss"]; ll_n += 1
        if s["market_logloss"] is not None:
            vll_sum += s["market_logloss"]; vll_n += 1
    totals["model_logloss"] = ll_sum / ll_n if ll_n else None
    totals["market_logloss"] = vll_sum / vll_n if vll_n else None
    totals["return_pct"] = (totals["profit"] / totals["wagered"]) if totals["wagered"] else None
    if season_summaries:
        season_rows_html += summary_row_html("Total (unweighted LL mean)", totals, bold=True)

    bankroll_str = f"${bankroll:,.2f}" if bankroll is not None else "-"
    html = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>NHL Model Dashboard</title>"
        f"<style>{STYLE}</style></head><body>"
        "<h1>NHL Model Dashboard</h1>"
        f"<div class=\"meta\">Live bankroll: {bankroll_str} &middot; "
        f"Model version {model_version[0]} (effective {model_version[1]}) &middot; "
        f"Updated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</div>"
        "<h2>Today's games</h2>"
        f"{render_todays_games(todays_rows)}"
        f"<h2>{season_disp} &mdash; by week</h2>"
        f"{week_html if week_html else '<p class=\"meta\">No settled games yet this season.</p>'}"
        f"<h2>{season_disp} &mdash; by team</h2>"
        f"{team_html if team_html else '<p class=\"meta\">No settled games yet this season.</p>'}"
        "<h2>Past seasons</h2>"
        f"{summary_table_html('Season', season_rows_html)}"
        "</body></html>"
    )
    return html


def main():
    conn = get_db_conn()
    try:
        model_version = get_current_model_version(conn)
        bankroll = get_live_bankroll(conn)
        todays_rows = get_todays_games(conn)
        print(f"Today's games: {len(todays_rows)}")

        detail_season = get_detail_season(conn)
        print(f"Detail season: {detail_season}")
        starts = season_start_dates(conn)

        detail_instances = get_instances(conn, [detail_season]) if detail_season else []
        week_groups_dict = group_by(detail_instances, lambda x: calendar_week(x["date"], starts[x["season"]]))
        week_groups = sorted(week_groups_dict.items(), key=lambda kv: kv[0], reverse=True)
        team_groups_dict = group_by(detail_instances, lambda x: x["team"])
        team_groups = sorted(team_groups_dict.items(), key=lambda kv: kv[0])

        all_seasons = get_all_seasons(conn)
        all_instances = get_instances(conn, all_seasons)
        by_season = group_by(all_instances, lambda x: x["season"])
        season_summaries = [(s, aggregate(by_season[s])) for s in sorted(by_season)]
    finally:
        conn.close()

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        f.write(render_html(model_version, bankroll, todays_rows, detail_season,
                            week_groups, team_groups, season_summaries))
    print(f"Dashboard written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
