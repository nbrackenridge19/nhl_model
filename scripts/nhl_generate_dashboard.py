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
    """Every CONFIGURED season, not just ones with settled games -- so a season with zero games played
    yet (2026-27, before opening night) still shows up in the season table with a zero/dash row."""
    with conn.cursor() as cur:
        cur.execute("select season from season_config order by season desc")
        return [r[0] for r in cur.fetchall()]


def get_season_wallet_returns(conn):
    """Per-season wallet return: reset to that season's own season_config.starting_bankroll and compound
    forward using ONLY that season's own v_kelly_daily_return rows (i.e. NOT the continuous cross-season
    v_kelly_bank_theoretical series used elsewhere on this page). Verified against Nick's own reference
    number: 1819 comes out to exactly $2,396.52 -- matching what he cited -- off a $5,000 starting
    bankroll (season_config's actual configured value, not the $2,500 he mentioned; flagged in chat, not
    silently changed to match)."""
    with conn.cursor() as cur:
        cur.execute("""
            select sc.season, sc.starting_bankroll,
                   sc.starting_bankroll * exp(coalesce(sum(ln(1 + r.daily_return_pct)), 0))
            from season_config sc
            left join v_kelly_daily_return r on r.season = sc.season
            group by sc.season, sc.starting_bankroll
        """)
        rows = cur.fetchall()
    return {season: {"starting_bankroll": float(start), "ending_bank": float(end),
                     "wallet_return": float(end) - float(start)}
            for season, start, end in rows}


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
    query += " order by p.date"
    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    instances = []
    for (game_id, team, opp, season, gdate, home, win, model_pct, mlpct, logloss, vlogloss,
         kf, bets_fire, kelly_fraction, lam, bank, home_goals, away_goals) in rows:
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
            "home_goals": home_goals, "away_goals": away_goals,
        })
    return instances


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


def get_season_bank_series(conn, season):
    """(date, bank_start_of_date) pairs for the season, from the continuous all-history
    v_kelly_bank_theoretical series -- used for the wallet chart, not the per-season-reset wallet
    return figure (that's get_season_wallet_returns, a deliberately different, self-contained series)."""
    with conn.cursor() as cur:
        cur.execute("select date, bank_start_of_date from v_kelly_bank_theoretical where season=%s order by date",
                    (season,))
        return [(r[0], float(r[1])) for r in cur.fetchall()]


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

STYLE = (
    "body { font-family: -apple-system, sans-serif; max-width: 1200px; margin: 0 auto; "
    "padding: 16px; background: #fafafa; } "
    "h1 { font-size: 20px; } h2 { font-size: 16px; margin-top: 28px; } "
    ".meta { color: #666; font-size: 13px; margin-bottom: 16px; } "
    "table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; "
    "overflow: hidden; margin-bottom: 8px; } "
    "th, td { padding: 10px 8px; text-align: left; font-size: 14px; border-bottom: 1px solid #eee; } "
    "th { background: #f0f0f0; font-size: 12px; text-transform: uppercase; } "
    ".tag { color: #999; font-size: 11px; } "
    ".scroll-wrap { overflow-x: auto; margin-bottom: 8px; } "
    ".row-grid { display: grid; gap: 4px 10px; align-items: center; padding: 8px 12px; "
    "font-size: 12px; min-width: 820px; } "
    ".row-grid > div { white-space: nowrap; } "
    ".group-head, .col-head { background: #f0f0f0; font-weight: 600; text-transform: uppercase; "
    "font-size: 10px; color: #555; } "
    ".group-head { padding-bottom: 0; } "
    ".col-head { padding-top: 2px; border-radius: 8px 8px 0 0; } "
    ".group-label { text-align: center; border-bottom: 1px solid #ddd; padding-bottom: 2px; } "
    ".block { background: white; border-radius: 0 0 8px 8px; margin-bottom: 8px; overflow: hidden; "
    "border-top: 1px solid #eee; } "
    ".block:first-of-type { border-top: none; } "
    ".block > summary { cursor: pointer; list-style: none; } "
    ".block > summary::-webkit-details-marker { display: none; } "
    ".block > summary::before { content: '\\25B8'; margin-right: 4px; color: #999; } "
    ".block[open] > summary::before { content: '\\25BE'; } "
    ".block-title { font-weight: 700; } "
    ".nested { margin: 0 12px 10px; border: 1px solid #eee; border-radius: 6px; overflow: hidden; } "
    ".nested summary { padding: 8px 10px; cursor: pointer; list-style: none; font-size: 12px; "
    "font-weight: 600; color: #444; background: #fbfbfb; } "
    ".nested summary::-webkit-details-marker { display: none; } "
    ".nested summary::before { content: '\\25B8'; margin-right: 4px; color: #999; } "
    ".nested[open] summary::before { content: '\\25BE'; } "
    "table.detail { border-radius: 0; margin: 0; box-shadow: none; } "
    "table.detail th, table.detail td { padding: 8px; white-space: nowrap; } "
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


def summary_row_html(label, s, wallet_return=None, bold=False):
    win_pct = f"{s['wins'] / s['bets_placed']:.0%}" if s["bets_placed"] else "-"
    profit_color = "#1a7f37" if s["profit"] >= 0 else "#c0392b"
    return_str = f"{s['return_pct']:+.1%}" if s["return_pct"] is not None else "-"
    model_ll = f"{s['model_logloss']:.3f}" if s["model_logloss"] is not None else "-"
    market_ll = f"{s['market_logloss']:.3f}" if s["market_logloss"] is not None else "-"
    delta = (s["model_logloss"] - s["market_logloss"]) if (
        s["model_logloss"] is not None and s["market_logloss"] is not None) else None
    delta_str = f"{delta:+.3f}" if delta is not None else "-"
    if wallet_return is not None:
        wr_color = "#1a7f37" if wallet_return >= 0 else "#c0392b"
        wallet_cell = f"<td><span style=\"color:{wr_color}; font-weight:600;\">{money(wallet_return)}</span></td>"
    else:
        wallet_cell = "<td>-</td>"
    style = "font-weight:700; border-top:2px solid #ccc;" if bold else ""
    return (
        f"<tr style=\"{style}\">"
        f"<td>{label}</td><td>{s['bets_placed']}</td>"
        f"<td>{s['wins']}-{s['losses']} ({win_pct})</td>"
        f"<td>${s['wagered']:,.2f}</td>"
        f"<td><span style=\"color:{profit_color}; font-weight:600;\">{money(s['profit'])}</span></td>"
        f"<td>{return_str}</td>"
        f"{wallet_cell}"
        f"<td>{model_ll}</td><td>{market_ll}</td>"
        f"<td style=\"{logloss_delta_bg(delta)}\">{delta_str}</td>"
        "</tr>"
    )


def summary_table_html(header_label, rows_html):
    return (
        f"<div class=\"scroll-wrap\"><table><tr><th>{header_label}</th><th>Bets</th><th>Record</th>"
        "<th>Wagered</th><th>Profit</th><th>Return</th>"
        "<th>Wallet Return</th>"
        "<th>LogLoss</th><th>Mkt LogLoss</th><th>LL &Delta;</th></tr>"
        f"{rows_html}</table></div>"
    )


def render_wallet_chart_svg(bank_series, ll_delta_series, width=760, height=320):
    """Self-contained inline SVG -- no chart library, no external CDN, so the page stays fully
    self-contained on GitHub Pages. Two independently-scaled lines sharing one date axis: wallet $ on
    the left, cumulative avg LogLoss delta on the right (with its own zero-line, since it crosses zero)."""
    if len(bank_series) < 2 and len(ll_delta_series) < 2:
        return "<p class=\"meta\">Not enough settled days yet for a chart.</p>"

    left, right, top, bottom = 64, 64, 16, 32
    plot_w, plot_h = width - left - right, height - top - bottom
    all_dates = [d for d, _ in bank_series] + [d for d, _ in ll_delta_series]
    dmin, dmax = min(all_dates), max(all_dates)
    dspan = max(1, (dmax - dmin).days)

    def x_of(dt):
        return left + (dt - dmin).days / dspan * plot_w

    def y_scale(values, pad_frac=0.08):
        lo, hi = min(values), max(values)
        if lo == hi:
            lo, hi = lo - 1, hi + 1
        pad = (hi - lo) * pad_frac
        lo, hi = lo - pad, hi + pad

        def y_of(v):
            return top + (1 - (v - lo) / (hi - lo)) * plot_h
        return y_of, lo, hi

    def polyline(series, y_of, color):
        pts = " ".join(f"{x_of(d):.1f},{y_of(v):.1f}" for d, v in series)
        return f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" />'

    svg = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
          f'style="width:100%; height:auto; background:white; border-radius:8px;">']

    if len(bank_series) >= 2:
        bank_vals = [v for _, v in bank_series]
        y_bank, bank_lo, bank_hi = y_scale(bank_vals)
        for frac in (0, 0.5, 1):
            v = bank_lo + frac * (bank_hi - bank_lo)
            svg.append(f'<text x="{left - 8}" y="{y_bank(v):.1f}" font-size="10" fill="#1a7f37" '
                      f'text-anchor="end" dominant-baseline="middle">${v:,.0f}</text>')
        svg.append(polyline(bank_series, y_bank, "#1a7f37"))

    if len(ll_delta_series) >= 2:
        delta_vals = [v for _, v in ll_delta_series]
        y_delta, delta_lo, delta_hi = y_scale(delta_vals)
        for frac in (0, 0.5, 1):
            v = delta_lo + frac * (delta_hi - delta_lo)
            svg.append(f'<text x="{width - right + 8}" y="{y_delta(v):.1f}" font-size="10" fill="#6a3fb5" '
                      f'text-anchor="start" dominant-baseline="middle">{v:+.3f}</text>')
        if delta_lo < 0 < delta_hi:
            svg.append(f'<line x1="{left}" x2="{width - right}" y1="{y_delta(0):.1f}" y2="{y_delta(0):.1f}" '
                      f'stroke="#ccc" stroke-width="1" stroke-dasharray="4,3" />')
        svg.append(polyline(ll_delta_series, y_delta, "#6a3fb5"))

    svg.append(f'<text x="{left}" y="{height - 8}" font-size="10" fill="#999">{dmin}</text>')
    svg.append(f'<text x="{width - right}" y="{height - 8}" font-size="10" fill="#999" text-anchor="end">{dmax}</text>')
    svg.append(
        f'<g font-size="11">'
        f'<rect x="{left}" y="{top}" width="10" height="10" fill="#1a7f37" />'
        f'<text x="{left + 14}" y="{top + 9}" fill="#444">Wallet ($, left)</text>'
        f'<rect x="{left + 150}" y="{top}" width="10" height="10" fill="#6a3fb5" />'
        f'<text x="{left + 164}" y="{top + 9}" fill="#444">Cumulative avg LL &Delta; (right)</text>'
        '</g>'
    )
    svg.append("</svg>")
    return "".join(svg)


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
            wagers.append(f"{side['team'].upper()} ${side['stake_dollar']:,.2f} &mdash; "
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


def group_header_row():
    """The shared, non-collapsible two-tier header sitting above a week/team block list -- printed once,
    with each block's own summary row (see render_group_block) using the same grid so it lines up."""
    return (
        f"<div class=\"row-grid group-head\" style=\"grid-template-columns:{ROW_GRID_COLS};\">"
        "<div></div><div></div><div></div><div></div><div></div><div></div>"
        "<div class=\"group-label\" style=\"grid-column: span 3;\">LogLoss &mdash; Bets Placed</div>"
        "<div class=\"group-label\" style=\"grid-column: span 3;\">LogLoss &mdash; All Games</div>"
        "</div>"
        f"<div class=\"row-grid col-head\" style=\"grid-template-columns:{ROW_GRID_COLS};\">"
        "<div></div><div>Bets</div><div>Record</div><div>Wagered</div><div>Profit</div><div>Return</div>"
        "<div>Model</div><div>Market</div><div>&Delta;</div>"
        "<div>Model</div><div>Market</div><div>&Delta;</div>"
        "</div>"
    )


def render_group_block(label, instances, pairs):
    s = aggregate(instances)
    win_pct = f"{s['wins']}-{s['losses']}" if s["bets_placed"] else "0-0"
    profit_color = "#1a7f37" if s["profit"] >= 0 else "#c0392b"
    return_str = f"{s['return_pct']:+.1%}" if s["return_pct"] is not None else "-"

    def ll_cells(ll, vll):
        delta = (ll - vll) if (ll is not None and vll is not None) else None
        return (
            f"<div>{f'{ll:.3f}' if ll is not None else '-'}</div>"
            f"<div>{f'{vll:.3f}' if vll is not None else '-'}</div>"
            f"<div style=\"{logloss_delta_bg(delta)}\">{f'{delta:+.3f}' if delta is not None else '-'}</div>"
        )

    summary_row = (
        f"<div class=\"row-grid\" style=\"grid-template-columns:{ROW_GRID_COLS};\">"
        f"<div class=\"block-title\">{label}</div>"
        f"<div>{s['bets_placed']}</div><div>{win_pct}</div>"
        f"<div>${s['wagered']:,.2f}</div>"
        f"<div style=\"color:{profit_color}; font-weight:600;\">{money(s['profit'])}</div>"
        f"<div>{return_str}</div>"
        f"{ll_cells(s['bet_model_logloss'], s['bet_market_logloss'])}"
        f"{ll_cells(s['all_model_logloss'], s['all_market_logloss'])}"
        "</div>"
    )

    bet_pairs = [p for p in pairs if p["away"]["bets_fire"] or p["home"]["bets_fire"]]
    return (
        "<details class=\"block\"><summary>" + summary_row + "</summary>"
        f"<details class=\"nested\"><summary>Bets placed ({len(bet_pairs)})</summary>"
        f"{games_table_html(bet_pairs)}</details>"
        f"<details class=\"nested\"><summary>All games ({len(pairs)})</summary>"
        f"{games_table_html(pairs)}</details>"
        "</details>"
    )


def render_nested_drilldown(groups_sorted, label_fn, all_pairs_by_game_id):
    """groups_sorted: list of (key, instances) already in display order. all_pairs_by_game_id maps
    game_id -> pair (see pair_by_game), used to pull each group's games without re-pairing repeatedly."""
    if not groups_sorted:
        return ""
    blocks = ""
    for key, instances in groups_sorted:
        game_ids_in_group = {x["game_id"] for x in instances}
        pairs = sorted((all_pairs_by_game_id[gid] for gid in game_ids_in_group), key=lambda p: (p["date"], p["game_id"]))
        blocks += render_group_block(label_fn(key), instances, pairs)
    return f"<div class=\"scroll-wrap\">{group_header_row()}{blocks}</div>"


def render_html(model_version, bankroll, todays_rows, detail_season, week_groups, team_groups,
                season_summaries, pairs_by_game_id, wallet_returns, detail_season_summary, chart_svg):
    season_disp = f"{detail_season[:2]}-{detail_season[2:]}"

    week_html = render_nested_drilldown(week_groups, lambda k: f"Week {k}", pairs_by_game_id)
    team_html = render_nested_drilldown(team_groups, lambda k: k.upper(), pairs_by_game_id)

    season_rows_html = ""
    totals = {"n_instances": 0, "bets_placed": 0, "wins": 0, "losses": 0, "wagered": 0.0, "profit": 0.0,
              "model_logloss": None, "market_logloss": None}
    ll_sum = ll_n = vll_sum = vll_n = 0
    total_wallet_return = 0.0
    for season, s in season_summaries:
        disp = f"{season[:2]}-{season[2:]}"
        wr = wallet_returns.get(season, {}).get("wallet_return")
        season_rows_html += summary_row_html(disp, s, wallet_return=wr)
        totals["bets_placed"] += s["bets_placed"]; totals["wins"] += s["wins"]; totals["losses"] += s["losses"]
        totals["wagered"] += s["wagered"]; totals["profit"] += s["profit"]
        if wr is not None:
            total_wallet_return += wr
        if s["model_logloss"] is not None:
            ll_sum += s["model_logloss"]; ll_n += 1
        if s["market_logloss"] is not None:
            vll_sum += s["market_logloss"]; vll_n += 1
    totals["model_logloss"] = ll_sum / ll_n if ll_n else None
    totals["market_logloss"] = vll_sum / vll_n if vll_n else None
    totals["return_pct"] = (totals["profit"] / totals["wagered"]) if totals["wagered"] else None
    if season_summaries:
        season_rows_html += summary_row_html("Total", totals, wallet_return=total_wallet_return, bold=True)

    detail_wr = wallet_returns.get(detail_season, {}).get("wallet_return")
    detail_row_html = summary_row_html(season_disp, detail_season_summary, wallet_return=detail_wr)

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
        f"<h2>{season_disp} at a glance</h2>"
        f"{summary_table_html('Season', detail_row_html)}"
        f"<h2>{season_disp} &mdash; wallet &amp; LogLoss over time</h2>"
        f"{chart_svg}"
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
        detail_pairs = pair_by_game(detail_instances)
        pairs_by_game_id = {p["game_id"]: p for p in detail_pairs}
        week_groups_dict = group_by(detail_instances, lambda x: calendar_week(x["date"], starts[x["season"]]))
        week_groups = sorted(week_groups_dict.items(), key=lambda kv: kv[0], reverse=True)
        team_groups_dict = group_by(detail_instances, lambda x: x["team"])
        team_groups = sorted(team_groups_dict.items(), key=lambda kv: kv[0])
        detail_season_summary = aggregate(detail_instances)

        bank_series = get_season_bank_series(conn, detail_season) if detail_season else []
        ll_delta_series = cumulative_avg_ll_delta_series(detail_instances)
        chart_svg = render_wallet_chart_svg(bank_series, ll_delta_series)

        all_seasons = get_all_seasons(conn)
        all_instances = get_instances(conn, all_seasons)
        by_season = group_by(all_instances, lambda x: x["season"])
        # Every configured season gets a row, even one with zero games played (2627 before opening
        # night) -- by_season only has keys for seasons that actually returned instances.
        season_summaries = [(s, aggregate(by_season.get(s, []))) for s in sorted(all_seasons)]
        wallet_returns = get_season_wallet_returns(conn)
    finally:
        conn.close()

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        f.write(render_html(model_version, bankroll, todays_rows, detail_season,
                            week_groups, team_groups, season_summaries, pairs_by_game_id,
                            wallet_returns, detail_season_summary, chart_svg))
    print(f"Dashboard written to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
