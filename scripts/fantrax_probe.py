"""
fantrax_probe.py (v3, no fantraxapi library) -- read-only diagnostic of what
Fantrax's own endpoint returns for this league, using the saved login cookie.

WHY THIS VERSION
--------------------------------------------------------------------------
The fantraxapi library assumes DAILY scoring periods and crashed on this
league (all 185 scoring dates failed to match its period list). This probe
calls the same endpoint the library wraps (POST https://www.fantrax.com/fxpa/req)
directly and prints the SHAPE of what comes back, so the real sync can be built
on facts: how periods are labelled, which stat columns the roster call returns
(Hit, Blk, Tk, Cor?), which status ids mean active/reserve/IR/minors, and what
transaction type codes the league's history uses (claims, drops, trades).

SAFETY
--------------------------------------------------------------------------
- Read-only: no writes to Fantrax, no database access.
- The cookie comes from the FANTRAX_COOKIE environment variable and is never
  printed. Actions logs on a public repo are public, so this prints counts,
  column names, type codes and a few sample rows, nothing else.
- Every step is wrapped, so one failure does not hide the others.

EXIT CODES: 0 = all steps worked, 1 = at least one step failed, 2 = setup.
"""

import os
import sys
import time
import traceback
from collections import Counter

import requests

URL = "https://www.fantrax.com/fxpa/req"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DELAY = 0.5


class FxError(Exception):
    pass


def call(session, league_id, method, **data):
    """One Fantrax API call. Values are sent as strings, like the library does."""
    payload = {"leagueId": league_id}
    payload.update({k: str(v) for k, v in data.items() if v is not None})
    r = session.post(URL, params={"leagueId": league_id},
                     json={"msgs": [{"method": method, "data": payload}]}, timeout=30)
    try:
        js = r.json()
    except ValueError:
        raise FxError(f"{method}: HTTP {r.status_code}, non-JSON response ({r.headers.get('content-type')})")
    if r.status_code >= 400:
        raise FxError(f"{method}: HTTP {r.status_code} {str(js)[:150]}")
    if "pageError" in js:
        raise FxError(f"{method}: pageError code={js['pageError'].get('code')}")
    return js["responses"][0]["data"]


def where(e):
    tb = traceback.extract_tb(e.__traceback__)
    return f"{os.path.basename(tb[-1].filename)}:{tb[-1].lineno}" if tb else "?"


def step(name, fn, results):
    print(f"--- {name}")
    try:
        out = fn()
        results[name] = True
        return out
    except Exception as e:  # noqa: BLE001
        results[name] = False
        print(f"  FAILED: {type(e).__name__}: {str(e)[:250]} (at {where(e)})")
        return None


def main():
    league_id = os.environ.get("FANTRAX_LEAGUE_ID", "").strip()
    cookie = os.environ.get("FANTRAX_COOKIE", "").strip()
    if not league_id:
        print("SETUP: FANTRAX_LEAGUE_ID is not set.")
        return 2
    if not cookie:
        print("SETUP: FANTRAX_COOKIE is empty or missing. Check the repo secret AND the workflow env: mapping.")
        return 2

    s = requests.Session()
    s.headers.update({"Cookie": cookie, "User-Agent": USER_AGENT, "Accept": "application/json"})
    results = {}
    state = {}

    # 1. League info + login check
    def league_info():
        d = call(s, league_id, "getFantasyLeagueInfo")
        st = d.get("fantasySettings", {})
        print(f"  league: {st.get('leagueName')} ({st.get('subtitle')}); top-level keys: {sorted(d.keys())[:12]}")
        print(f"  settings keys: {sorted(st.keys())[:25]}")
    step("league info (login check)", league_info, results)

    # 2. Status id -> name
    def statuses():
        d = call(s, league_id, "getRefObject", type="FantasyItemStatus")
        m = {k: v.get("name") for k, v in d.get("allObjs", {}).items() if isinstance(v, dict) and "name" in v}
        state["status_names"] = m
        print(f"  status ids: {m}")
    step("roster status ids", statuses, results)

    # 3. First roster call: structure, period labels, stat columns, team list
    def first_roster():
        d = call(s, league_id, "getTeamRosterInfo", view="STATS")
        teams = d.get("fantasyTeams", {})
        if isinstance(teams, list):
            teams = {t["id"]: t for t in teams}
        state["teams"] = {tid: t.get("name", tid) for tid, t in teams.items()}
        print(f"  teams: {len(state['teams'])}; response keys: {sorted(d.keys())}")
        sel = d.get("displayedSelections", {})
        print(f"  displayedSelections: {sel}")
        dl = d.get("displayedLists", {})
        print(f"  displayedLists keys: {sorted(dl.keys())}")
        pl = dl.get("periodList", [])
        print(f"  periodList: {len(pl)} entries; first 3: {pl[:3]}; last 2: {pl[-2:]}")
        tables = d.get("tables", [])
        print(f"  tables: {len(tables)}")
        for i, t in enumerate(tables[:3]):
            hdr = [c.get("shortName") or c.get("name") for c in t.get("header", {}).get("cells", [])]
            rows = t.get("rows", [])
            print(f"  table {i}: {len(rows)} rows; columns: {hdr}")
        # one sample player row (name, team, positions, status only)
        for t in tables:
            for row in t.get("rows", []):
                sc = row.get("scorer")
                if sc:
                    print(f"  sample player keys: {sorted(sc.keys())[:20]}")
                    print(f"  sample: {sc.get('name')} | {sc.get('teamShortName')} | {sc.get('posShortNames')} "
                          f"| posId={row.get('posId')} statusId={row.get('statusId')} id={sc.get('scorerId')}")
                    return
    step("roster structure (first team)", first_roster, results)

    # 4. Every team's roster counts by status
    def all_rosters():
        names = state.get("status_names", {})
        total = 0
        for tid, tname in state.get("teams", {}).items():
            d = call(s, league_id, "getTeamRosterInfo", teamId=tid, view="STATS")
            by_status = Counter()
            for t in d.get("tables", []):
                for row in t.get("rows", []):
                    if row.get("scorer"):
                        by_status[names.get(str(row.get("statusId")), f"status {row.get('statusId')}")] += 1
            n = sum(by_status.values())
            total += n
            print(f"  {tname}: {n} players {dict(by_status)}")
            time.sleep(DELAY)
        print(f"  total rostered players: {total}")
        if not state.get("teams"):
            raise FxError("no teams found, nothing to count")
    step("all team rosters", all_rosters, results)

    # 5. Transactions
    def transactions():
        d = call(s, league_id, "getTransactionDetailsHistory", maxResultsPerPage=100)
        table = d.get("table", {})
        rows = table.get("rows", [])
        print(f"  data keys: {sorted(d.keys())}; table keys: {sorted(table.keys())}")
        print(f"  rows: {len(rows)}; distinct transaction sets: {len({r.get('txSetId') for r in rows})}")
        codes = Counter((r.get('transactionCode'), r.get('claimType')) for r in rows)
        print(f"  (transactionCode, claimType) counts: {dict(codes)}")
        if rows:
            print(f"  row keys: {sorted(rows[0].keys())}")
        for r in rows[:4]:
            cells = r.get("cells", [])
            print(f"  sample: {cells[1].get('content') if len(cells) > 1 else '?'} | team {cells[0].get('teamId') if cells else '?'} "
                  f"| {r.get('transactionCode')}/{r.get('claimType')} | {(r.get('scorer') or {}).get('name')}")
        if not rows:
            raise FxError("no transaction rows returned")
    step("transaction history", transactions, results)

    print("--- summary")
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}: {k}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
