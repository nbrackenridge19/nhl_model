"""
fantasy_generate_dashboard.py -- builds docs/fantasy/index.html for the Dynasty Puck fantasy league.

Reads (Supabase, schema `fantasy`): hr_season (Hockey-Reference season stats), roster_current + players
(Fantrax rosters), contracts, cut_players, prospects, settings, v_recent_changes, teams.
All SAA/SAR maths runs in the browser (scripts/fantasy_sar_engine.js, validated against the workbook),
so the toggles (stat window, games floor, pool definition) respond instantly.
Cap tables are computed here (they only depend on rosters + contracts).

Usage:  python scripts/fantasy_generate_dashboard.py                  # DB -> docs/fantasy/index.html
        python scripts/fantasy_generate_dashboard.py --fixture f.json --out out.html   # offline test
Env:    PGHOST PGUSER PGPASSWORD PGDATABASE PGPORT
"""
import argparse
import datetime as dt
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SEASONS = ["2526", "2627"]
SEASON_LABEL = {"2526": "2025-26", "2627": "2026-27"}
CURRENT = "2627"
MY_TEAM_NAME = "Cake Eaters"       # default team in the dashboard selector (Nick's team)


# ----------------------------------------------------------------------------- helpers
def xl_round(x, nd=0):
    m = 10 ** nd
    return math.copysign(math.floor(abs(x) * m + 0.5) / m, x)


def jnum(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return int(f) if f == int(f) and abs(f) < 1e15 else f


def pos_group(positions):
    p = (positions or "").upper()
    if "G" in p.split(",") or p.strip() == "G":
        return "G"
    return "D" if p.strip().startswith("D") else "F"


def contract_years(start, end):
    if start is None or end is None:
        return None
    return int(round((int(end) - int(start)) / 101)) + 1


def tier_years(aav, tiers):
    """Years implied by a bid tier (used for cut players): largest tier whose threshold <= aav."""
    yrs = 0
    for thr, y in tiers:
        if aav >= thr:
            yrs = y
    return yrs


# ----------------------------------------------------------------------------- cap
def cap_series(base, n=6):
    cap = list(base)
    while len(cap) < n:
        prev2, prev = cap[-2], cap[-1]
        cap.append(xl_round(prev * (1 + math.sqrt(prev / prev2) - 1), -5))
    return cap[:n]


def build_cap(teams, roster, cuts, settings, first_label="2026-27", n=6):
    """roster: dicts with t (team idx), st, aav, yrs, free(bool = $1M default contract, a free cut), n. cuts: dicts with t, aav, n, yrs.
    space = cap - active salary - dead cap (50% of cut players' salary). IR is excluded from the current season only."""
    base = settings.get("salary_cap", {}).get("base", [104000000, 113500000])
    caps = cap_series(base, n)
    default_aav = settings.get("default_fa_aav", 1000000)
    share = settings.get("dead_cap_share", 0.5)
    tiers = settings.get("contract_tiers", [[1000000, 1]])
    y0 = int(first_label[:4])
    labels = [f"{y0 + i}-{str(y0 + i + 1)[2:]}" for i in range(n)]
    out = []
    for ti, team in enumerate(teams):
        active, ir, dead = [0.0] * n, [0.0] * n, [0.0] * n
        players = []
        for r in roster:
            if r["t"] != ti or r["st"] == "Minors":
                continue
            aav = r["aav"] if r["aav"] is not None else default_aav
            yrs = r["yrs"] if r["yrs"] else 1
            per = [aav if i < yrs else 0 for i in range(n)]
            is_ir = r["st"] == "Inj Res"
            for i in range(n):
                if is_ir:
                    ir[i] += per[i]
                    if i > 0:
                        active[i] += per[i]       # IR is excluded from this season's cap only (as in the workbook)
                else:
                    active[i] += per[i]
            players.append(dict(n=r["n"], g=r["g"], st=r["st"], aav=aav, yrs=yrs, free=r["free"], ct=r.get("ct"), per=per))
        cuts_t = []
        for c in cuts:
            if c["t"] != ti:
                continue
            yrs = c.get("yrs") or tier_years(c["aav"], tiers) or 1
            per = [share * c["aav"] if i < yrs else 0 for i in range(n)]
            for i in range(n):
                dead[i] += per[i]
            cuts_t.append(dict(n=c["n"], aav=c["aav"], yrs=yrs, per=per))
        space = [caps[i] - active[i] - dead[i] for i in range(n)]       # dead cap is always included
        out.append(dict(t=ti, active=active, ir=ir, dead=dead, space=space,
                        players=sorted(players, key=lambda p: -p["aav"]), cuts=cuts_t))
    return dict(labels=labels, caps=caps, teams=out)


# ----------------------------------------------------------------------------- DB
def fetch_db(conn):
    import psycopg2.extras
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def q(sql, args=None):
        cur.execute(sql, args)
        return cur.fetchall()

    settings = {r["key"]: r["value"] for r in q("select key, value from fantasy.settings")}
    teams_rows = q("select fantasy_team_id, name, short_name from fantasy.teams order by name")
    tix = {r["fantasy_team_id"]: i for i, r in enumerate(teams_rows)}
    teams = [dict(id=r["fantasy_team_id"], name=r["name"], short=r["short_name"] or r["name"]) for r in teams_rows]

    roster_rows = q("""
        select rc.fantrax_id, rc.fantasy_team_id, rc.status, rc.age, fp.name, fp.nhl_team, fp.positions,
               fp.hr_player_id, c.aav, c.contract_type, c.start_season, c.end_season
          from fantasy.roster_current rc
          join fantasy.players fp using (fantrax_id)
          left join fantasy.contracts c on c.fantrax_id = rc.fantrax_id""")
    roster = []
    for r in roster_rows:
        ct = r["contract_type"]
        # '$0.1M' MNR rows are placeholders (a workbook workaround so prospects were not "free agents"); Fantrax
        # ownership now does that job. A placeholder is not a real contract: it carries no salary and, if the
        # player is promoted to a non-minors slot, he is a default $1M league-minimum contract.
        placeholder = ct == "MNR"
        has_c = r["aav"] is not None and not placeholder
        yrs = contract_years(r["start_season"], r["end_season"]) if has_c else None
        roster.append(dict(fid=r["fantrax_id"], hr=r["hr_player_id"], n=r["name"], g=pos_group(r["positions"]),
                           tm=r["nhl_team"], age=r["age"], t=tix.get(r["fantasy_team_id"], -1), st=r["status"],
                           aav=jnum(r["aav"]) if has_c else None, ct=ct, end=r["end_season"], yrs=yrs,
                           real=has_c and ct != "FA",           # a real negotiated contract (not $1M default / placeholder)
                           free=(not has_c) or ct == "FA"))

    cut_rows = q("""
        select cp.fantasy_team_id, cp.name, cp.pos, coalesce(cp.aav, c.aav) aav, cp.years
          from fantasy.cut_players cp
          left join fantasy.contracts c on c.fantrax_id = cp.fantrax_id""")
    cuts = [dict(t=tix.get(r["fantasy_team_id"], -1), n=r["name"], aav=float(r["aav"] or 0), yrs=r["years"]) for r in cut_rows]

    stat_rows = q("""
        select season, hr_player_id, name, team, pos, grp, age, gp, g, a, pim, ppg, shg, ppa, sha, sog, hit, blk,
               tk, cf, ca, w, ga, sa, sv_pct, gaa, so, toi_min
          from fantasy.hr_season where season = any(%s)""", (SEASONS,))
    pl = {}
    for r in stat_rows:
        p = pl.setdefault(r["hr_player_id"], dict(id=r["hr_player_id"], n=r["name"], g=r["grp"], pos=r["pos"],
                                                  tm=r["team"], age=r["age"], s={}, _last=""))
        if r["season"] >= p["_last"]:                        # newest season supplies team / position / age
            p.update(tm=r["team"], pos=r["pos"], g=r["grp"], age=r["age"], n=r["name"], _last=r["season"])
        if r["grp"] == "G":
            p["s"][r["season"]] = dict(gp=r["gp"], w=r["w"], ga=r["ga"], sa=r["sa"], sv=jnum(r["sv_pct"]),
                                       gaa=jnum(r["gaa"]), so=r["so"], min=r["toi_min"])
        else:
            stp = sum(x or 0 for x in (r["ppg"], r["shg"], r["ppa"], r["sha"]))
            p["s"][r["season"]] = dict(gp=r["gp"], g=r["g"], a=r["a"], pim=r["pim"], sog=r["sog"], stp=stp,
                                       hit=r["hit"], blk=r["blk"], tk=r["tk"], cf=r["cf"], ca=r["ca"])
    players = []
    for p in pl.values():
        p.pop("_last")
        players.append(p)

    prospects = [dict(t=tix.get(r["fantasy_team_id"], -1), g=r["pos_group"], n=r["name"], age=r["age"],
                      lvl=r["expected_level"], up=jnum(r["upside"]), cert=jnum(r["certainty"]), gp=r["nhl_gp"],
                      traj=r["trajectory"], nxt=r["next_season"])
                 for r in q("select * from fantasy.prospects")]
    changes = [dict(at=r["detected_at"].isoformat(), ev=r["event_type"], p=r["player"], nhl=r["nhl_team"],
                    pos=r["positions"], frm=r["from_team"], to=r["to_team"], fs=r["from_status"], ts=r["to_status"],
                    note=r["note"])
               for r in q("select * from fantasy.v_recent_changes order by detected_at desc limit 300")]
    sync = q("select max(finished_at) f from fantasy.sync_runs where finished_at is not null")
    hr_upd = q("select max(updated_at) u from fantasy.hr_season")
    return dict(teams=teams, roster=roster, cuts=cuts, players=players, prospects=prospects, changes=changes,
                settings=settings,
                asof=dict(roster=sync[0]["f"].isoformat() if sync and sync[0]["f"] else None,
                          stats=hr_upd[0]["u"].isoformat() if hr_upd and hr_upd[0]["u"] else None))


# ----------------------------------------------------------------------------- render
def render(data, out_path):
    for r in data["roster"]:
        r.setdefault("real", r.get("aav") is not None)
    cap = build_cap(data["teams"], data["roster"], data["cuts"], data["settings"])
    payload = dict(league="Dynasty Puck", seasons=SEASONS, seasonLabel=SEASON_LABEL, current=CURRENT,
                   asof=data.get("asof", {}),
                   defaultAav=data["settings"].get("default_fa_aav", 1000000),
                   teams=data["teams"], roster=data["roster"], players=data["players"], cap=cap,
                   prospects=data["prospects"], changes=data["changes"],
                   etaYears=data["settings"].get("league_eta_years", {}), myTeam=MY_TEAM_NAME,
                   deadShare=data["settings"].get("dead_cap_share", 0.5),
                   tiers=data["settings"].get("contract_tiers", [[1000000, 1]]))
    with open(os.path.join(HERE, "fantasy_sar_engine.js"), encoding="utf-8") as f:
        engine = f.read()
    with open(os.path.join(HERE, "fantasy_dashboard_template.html"), encoding="utf-8") as f:
        tpl = f.read()
    blob = json.dumps(payload, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    html = tpl.replace("/*ENGINE*/", engine).replace("/*DATA*/null", blob)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {out_path} ({len(html) / 1024:.0f} KB): {len(data['players'])} stat players, "
          f"{len(data['roster'])} rostered, {len(data['prospects'])} prospects, {len(data['changes'])} changes")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "..", "docs", "fantasy", "index.html"))
    ap.add_argument("--fixture")
    args = ap.parse_args()
    if args.fixture:
        data = json.load(open(args.fixture, encoding="utf-8"))
    else:
        import psycopg2
        conn = psycopg2.connect(host=os.environ["PGHOST"], user=os.environ["PGUSER"], password=os.environ["PGPASSWORD"],
                                dbname=os.environ["PGDATABASE"], port=os.environ.get("PGPORT", "5432"), sslmode="require")
        data = fetch_db(conn)
    render(data, args.out)


if __name__ == "__main__":
    main()
