"""
fantrax_sync.py -- keeps the fantasy schema in sync with the Fantrax league
"Dynasty Puck": who is on which team, and what changed (adds, drops, trades,
slot moves).

HOW IT WORKS
--------------------------------------------------------------------------
1. Pull every team's roster and the league's recent transaction history from
   Fantrax (POST https://www.fantrax.com/fxpa/req, the endpoint the Fantrax
   website itself uses), authenticated with a saved login cookie.
2. Compare the pull with fantasy.roster_current (the previous pull).
   - player on a roster that was not there before  -> ADD
   - player no longer on any roster                -> DROP
   - player on a different team                    -> TRADE, unless Fantrax's
     history shows a free-agent claim by the new team, in which case it was a
     DROP by the old team plus an ADD by the new one (both happened between
     two syncs)
   - same team, different slot                     -> STATUS
3. Write the changes to fantasy.roster_events, refresh fantasy.roster_current,
   and store the transaction history in fantasy.transactions.

The first run only SEEDS roster_current (no events), since there is nothing to
compare against.

SAFETY
--------------------------------------------------------------------------
- Read-only against Fantrax. Never writes to Fantrax.
- All Fantrax calls finish BEFORE anything is written. A failed or partial
  pull (a team missing, a roster far smaller than last time, an expired
  cookie) aborts the run without touching roster_current, so a bad pull can
  never show up as a wave of fake DROPs.
- More than MAX_EVENTS changes in one run aborts unless FORCE=true.
- DRY_RUN defaults to true: prints what it would write.
- The cookie comes from FANTRAX_COOKIE and is never printed.
- Database: PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT, short-lived connection.

EXIT CODES: 0 ok, 1 Fantrax/network error, 2 setup, 3 cookie rejected (refresh
the FANTRAX_COOKIE secret), 4 sanity check aborted the run.
"""

import os
import re
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime

import requests

URL = "https://www.fantrax.com/fxpa/req"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
FORCE = os.environ.get("FORCE", "false").strip().lower() == "true"
MAX_EVENTS = int(os.environ.get("MAX_EVENTS", "60"))
MIN_ROSTER_FRACTION = 0.8      # a pull smaller than this share of the previous one is treated as broken
REQUEST_DELAY_SECONDS = 0.5
TX_PAGE_SIZE = 100
TAG_RE = re.compile(r"<[^>]+>")


class FxError(Exception):
    pass


class NotLoggedIn(FxError):
    pass


class SanityAbort(Exception):
    pass


# --------------------------- Fantrax calls ---------------------------

def call(session, league_id, method, **data):
    """One Fantrax call (values sent as strings, as the website does). Retries transient failures twice."""
    payload = {"leagueId": league_id}
    payload.update({k: str(v) for k, v in data.items() if v is not None})
    last = None
    for attempt in range(3):
        try:
            r = session.post(URL, params={"leagueId": league_id},
                             json={"msgs": [{"method": method, "data": payload}]}, timeout=30)
            if r.status_code >= 500:
                raise FxError(f"{method}: HTTP {r.status_code}")
            try:
                js = r.json()
            except ValueError:
                raise FxError(f"{method}: HTTP {r.status_code}, non-JSON response ({r.headers.get('content-type')})")
            if r.status_code >= 400:
                raise FxError(f"{method}: HTTP {r.status_code} {str(js)[:150]}")
            err = js.get("pageError")
            if err:
                code = err.get("code")
                if code == "WARNING_NOT_LOGGED_IN":
                    raise NotLoggedIn("Fantrax says not logged in")
                raise FxError(f"{method}: pageError code={code}")
            return js["responses"][0]["data"]
        except NotLoggedIn:
            raise
        except (requests.RequestException, FxError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise FxError(f"{method} failed after 3 attempts: {str(last)[:200]}")


# --------------------------- parsing (pure functions) ---------------------------

def clean(s):
    return TAG_RE.sub("", s).strip() if isinstance(s, str) else s


def to_int(s):
    try:
        return int(str(s).strip())
    except (ValueError, TypeError):
        return None


def parse_status_names(resp):
    return {k: v.get("name") for k, v in resp.get("allObjs", {}).items() if isinstance(v, dict) and "name" in v}


def parse_teams(resp):
    teams = resp.get("fantasyTeams", {})
    if isinstance(teams, list):
        teams = {t["id"]: t for t in teams}
    return {tid: {"name": t.get("name", tid), "short_name": t.get("shortName")} for tid, t in teams.items()}


def parse_roster(resp, team_id, status_names):
    """Rostered players (one dict each) from a getTeamRosterInfo STATS response."""
    out = []
    for table in resp.get("tables", []):
        hdr = [(c.get("shortName") or c.get("name") or "") for c in table.get("header", {}).get("cells", [])]
        for row in table.get("rows", []):
            sc = row.get("scorer")
            if not sc:
                continue  # empty slot
            vals = dict(zip(hdr, [clean(c.get("content")) for c in row.get("cells", [])]))
            sid = str(row.get("statusId"))
            out.append({
                "fantrax_id": str(sc["scorerId"]),
                "name": sc.get("name"),
                "nhl_team": sc.get("teamShortName"),
                "positions": clean(sc.get("posShortNames")),
                "fantasy_team_id": team_id,
                "status": status_names.get(sid, f"status {sid}"),
                "pos_id": str(row.get("posId")),
                "age": to_int(vals.get("Age")),
                "salary_text": vals.get("Sal"),
                "contract": vals.get("Con"),
            })
    return out


def parse_transactions(resp):
    """Transaction rows from getTransactionDetailsHistory. Rows arrive grouped by txSetId; only the first
    row of a set carries the team and date, so the rest inherit them. Lineup changes are dropped."""
    rows = resp.get("table", {}).get("rows", [])
    out, head_team, head_text, current_set = [], None, None, None
    for r in rows:
        if r.get("txSetId") != current_set:
            current_set = r.get("txSetId")
            cells = r.get("cells") or []
            head_team = cells[0].get("teamId") if cells else None
            head_text = cells[1].get("content") if len(cells) > 1 else None
        code = r.get("transactionCode")
        sc = r.get("scorer")
        if not sc or not code or code == "LINEUP_CHANGE":
            continue
        try:
            local = datetime.strptime(head_text, "%a %b %d, %Y, %I:%M%p") if head_text else None
        except ValueError:
            local = None
        out.append({
            "tx_set_id": str(current_set), "fantrax_id": str(sc["scorerId"]), "name": sc.get("name"),
            "tx_code": code, "claim_type": r.get("claimType") or None, "fantasy_team_id": head_team,
            "occurred_text": head_text, "occurred_local": local, "executed": bool(r.get("executed", True)),
        })
    return out


# --------------------------- change detection (pure function) ---------------------------

def diff_rosters(prev, cur, txs):
    """prev / cur: {fantrax_id: {'fantasy_team_id', 'status'}}. txs: parsed transactions.
    Returns a list of event dicts."""
    claims = defaultdict(dict)   # fantrax_id -> {team_id: tx_set_id} for executed free-agent claims
    for t in txs:
        if t["tx_code"] == "CLAIM" and t["executed"]:
            claims[t["fantrax_id"]][t["fantasy_team_id"]] = t["tx_set_id"]
    events = []

    def ev(kind, fid, **kw):
        events.append({"event_type": kind, "fantrax_id": fid, "from_team_id": kw.get("ft"), "to_team_id": kw.get("tt"),
                       "from_status": kw.get("fs"), "to_status": kw.get("ts"), "tx_set_id": kw.get("tx"),
                       "note": kw.get("note")})

    for fid, c in cur.items():
        p = prev.get(fid)
        claim_tx = claims.get(fid, {}).get(c["fantasy_team_id"])
        if p is None:
            ev("ADD", fid, tt=c["fantasy_team_id"], ts=c["status"], tx=claim_tx,
               note="free-agent claim" if claim_tx else None)
        elif p["fantasy_team_id"] != c["fantasy_team_id"]:
            if claim_tx:
                ev("DROP", fid, ft=p["fantasy_team_id"], fs=p["status"], note="dropped, then claimed by another team")
                ev("ADD", fid, tt=c["fantasy_team_id"], ts=c["status"], tx=claim_tx, note="free-agent claim")
            else:
                ev("TRADE", fid, ft=p["fantasy_team_id"], tt=c["fantasy_team_id"], fs=p["status"], ts=c["status"])
        elif p["status"] != c["status"]:
            ev("STATUS", fid, ft=c["fantasy_team_id"], tt=c["fantasy_team_id"], fs=p["status"], ts=c["status"])
    for fid, p in prev.items():
        if fid not in cur:
            ev("DROP", fid, ft=p["fantasy_team_id"], fs=p["status"])
    return events


# --------------------------- Fantrax pull ---------------------------

def pull_league(session, league_id):
    status_names = parse_status_names(call(session, league_id, "getRefObject", type="FantasyItemStatus"))
    first = call(session, league_id, "getTeamRosterInfo", view="STATS")
    teams = parse_teams(first)
    if not teams:
        raise FxError("no teams returned")
    roster = {}
    for tid in teams:
        resp = call(session, league_id, "getTeamRosterInfo", teamId=tid, view="STATS")
        rows = parse_roster(resp, tid, status_names)
        if not rows:
            raise SanityAbort(f"team {teams[tid]['name']} returned an empty roster; treating the pull as broken")
        for r in rows:
            roster[r["fantrax_id"]] = r
        time.sleep(REQUEST_DELAY_SECONDS)
    txs = parse_transactions(call(session, league_id, "getTransactionDetailsHistory", maxResultsPerPage=TX_PAGE_SIZE))
    return teams, roster, txs


# --------------------------- database ---------------------------

def get_conn():
    import psycopg2
    return psycopg2.connect(connect_timeout=20)


def load_prev(conn):
    with conn.cursor() as cur:
        cur.execute("select fantrax_id, fantasy_team_id, status from fantasy.roster_current")
        return {r[0]: {"fantasy_team_id": r[1], "status": r[2]} for r in cur.fetchall()}


def write_all(conn, teams, roster, txs, events, seeded, run_started):
    from psycopg2.extras import execute_values
    with conn.cursor() as cur:
        execute_values(cur, "insert into fantasy.teams (fantasy_team_id, name, short_name) values %s "
                            "on conflict (fantasy_team_id) do update set name = excluded.name, "
                            "short_name = excluded.short_name, updated_at = now()",
                       [(tid, t["name"], t["short_name"]) for tid, t in teams.items()])
        execute_values(cur, "insert into fantasy.players (fantrax_id, name, nhl_team, positions) values %s "
                            "on conflict (fantrax_id) do update set name = excluded.name, nhl_team = excluded.nhl_team, "
                            "positions = excluded.positions, last_seen_at = now()",
                       [(r["fantrax_id"], r["name"], r["nhl_team"], r["positions"]) for r in roster.values()])
        # players that only appear in the transaction history (dropped players) still need a row for name lookups
        extra = {t["fantrax_id"]: t["name"] for t in txs if t["fantrax_id"] not in roster}
        if extra:
            execute_values(cur, "insert into fantasy.players (fantrax_id, name) values %s on conflict (fantrax_id) do nothing",
                           list(extra.items()))
        if events:
            execute_values(cur, "insert into fantasy.roster_events (fantrax_id, event_type, from_team_id, to_team_id, "
                                "from_status, to_status, tx_set_id, note) values %s",
                           [(e["fantrax_id"], e["event_type"], e["from_team_id"], e["to_team_id"], e["from_status"],
                             e["to_status"], e["tx_set_id"], e["note"]) for e in events])
        execute_values(cur, "insert into fantasy.roster_current (fantrax_id, fantasy_team_id, status, pos_id, age, "
                            "salary_text, contract) values %s on conflict (fantrax_id) do update set "
                            "fantasy_team_id = excluded.fantasy_team_id, status = excluded.status, pos_id = excluded.pos_id, "
                            "age = excluded.age, salary_text = excluded.salary_text, contract = excluded.contract, "
                            "updated_at = now()",
                       [(r["fantrax_id"], r["fantasy_team_id"], r["status"], r["pos_id"], r["age"], r["salary_text"],
                         r["contract"]) for r in roster.values()])
        cur.execute("delete from fantasy.roster_current where not (fantrax_id = any(%s))", (list(roster.keys()),))
        tx_new = 0
        if txs:
            cur.execute("select count(*) from fantasy.transactions")
            before = cur.fetchone()[0]
            execute_values(cur, "insert into fantasy.transactions (tx_set_id, fantrax_id, tx_code, claim_type, "
                                "fantasy_team_id, occurred_text, occurred_local, executed) values %s "
                                "on conflict (tx_set_id, fantrax_id, tx_code) do nothing",
                           [(t["tx_set_id"], t["fantrax_id"], t["tx_code"], t["claim_type"], t["fantasy_team_id"],
                             t["occurred_text"], t["occurred_local"], t["executed"]) for t in txs])
            cur.execute("select count(*) from fantasy.transactions")
            tx_new = cur.fetchone()[0] - before
        cur.execute("insert into fantasy.sync_runs (started_at, finished_at, status, teams, players, events, tx_new, message) "
                    "values (%s, now(), %s, %s, %s, %s, %s, %s)",
                    (run_started, "seeded" if seeded else "ok", len(teams), len(roster), len(events), tx_new, None))
    conn.commit()
    return tx_new


def log_run(status, message, run_started):
    """Record an aborted/failed run when the database is reachable. Never raises."""
    if DRY_RUN or not os.environ.get("PGHOST"):
        return
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("insert into fantasy.sync_runs (started_at, finished_at, status, message) "
                            "values (%s, now(), %s, %s)", (run_started, status, message[:500]))
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


# --------------------------- main ---------------------------

def main():
    run_started = datetime.utcnow()
    league_id = os.environ.get("FANTRAX_LEAGUE_ID", "").strip()
    cookie = os.environ.get("FANTRAX_COOKIE", "").strip()
    if not league_id or not cookie:
        print("SETUP: FANTRAX_LEAGUE_ID and FANTRAX_COOKIE must both be set "
              "(check the repo secret AND the workflow env: mapping).")
        return 2
    print(f"Fantrax sync -- DRY_RUN={DRY_RUN} -- {run_started:%Y-%m-%d %H:%M} UTC")

    session = requests.Session()
    session.headers.update({"Cookie": cookie, "User-Agent": USER_AGENT, "Accept": "application/json"})

    try:
        teams, roster, txs = pull_league(session, league_id)
    except NotLoggedIn:
        msg = "cookie rejected -- refresh the FANTRAX_COOKIE secret"
        print(f"RESULT: {msg}")
        log_run("failed", msg, run_started)
        return 3
    except SanityAbort as e:
        print(f"RESULT: aborted, nothing written: {e}")
        log_run("aborted", str(e), run_started)
        return 4
    except Exception as e:  # noqa: BLE001
        tb = traceback.extract_tb(e.__traceback__)
        where = f"{os.path.basename(tb[-1].filename)}:{tb[-1].lineno}" if tb else "?"
        msg = f"{type(e).__name__}: {str(e)[:200]} (at {where})"
        print(f"RESULT: Fantrax pull failed: {msg}")
        log_run("failed", msg, run_started)
        return 1
    print(f"Pulled {len(teams)} teams, {len(roster)} rostered players, {len(txs)} transaction rows")

    if not os.environ.get("PGHOST"):
        print("No PGHOST set -- pull succeeded, skipping the database step.")
        return 0

    conn = get_conn()
    try:
        prev = load_prev(conn)
        seeded = not prev
        if seeded:
            events = []
            print(f"Seed run: roster_current is empty, so {len(roster)} players will be recorded with no events.")
        else:
            if len(roster) < MIN_ROSTER_FRACTION * len(prev) and not FORCE:
                msg = f"pulled {len(roster)} players vs {len(prev)} last time; treating as a broken pull"
                print(f"RESULT: aborted, nothing written: {msg}")
                log_run("aborted", msg, run_started)
                return 4
            events = diff_rosters(prev, roster, txs)
            if len(events) > MAX_EVENTS and not FORCE:
                msg = f"{len(events)} changes in one run (limit {MAX_EVENTS}); set FORCE=true if this is real"
                print(f"RESULT: aborted, nothing written: {msg}")
                log_run("aborted", msg, run_started)
                return 4

        names = {r["fantrax_id"]: r["name"] for r in roster.values()}
        names.update({t["fantrax_id"]: t["name"] for t in txs})
        for e in events:
            tn = lambda tid: teams.get(tid, {}).get("name", tid) if tid else "-"  # noqa: E731
            print(f"  {e['event_type']:<6} {names.get(e['fantrax_id'], e['fantrax_id'])}: "
                  f"{tn(e['from_team_id'])} [{e['from_status'] or '-'}] -> {tn(e['to_team_id'])} [{e['to_status'] or '-'}]"
                  f"{'  (' + e['note'] + ')' if e['note'] else ''}")
        print(f"{len(events)} change(s) detected.")

        if DRY_RUN:
            print("[dry-run] nothing written.")
            return 0
        tx_new = write_all(conn, teams, roster, txs, events, seeded, run_started)
        print(f"[live] wrote {len(events)} event(s), refreshed {len(roster)} roster rows, {tx_new} new transaction row(s).")
        # Best effort: link any newly seen Fantrax players to Hockey-Reference ids.
        # A failure here must never fail the sync, so it is isolated.
        try:
            with conn.cursor() as cur:
                cur.execute("select fantasy.link_players()")
                linked = cur.fetchone()[0]
            conn.commit()
            print(f"[live] linked {linked} new player(s) to Hockey-Reference ids.")
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            print(f"[live] player linking skipped: {type(e).__name__}: {str(e)[:150]}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
