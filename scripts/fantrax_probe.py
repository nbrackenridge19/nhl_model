"""
fantrax_probe.py -- one-off, read-only check that a saved Fantrax login cookie
works from a GitHub Actions runner.

WHAT IT ANSWERS
--------------------------------------------------------------------------
1. Does Fantrax accept the cookie when the request comes from a GitHub
   runner (a different IP and browser than the one you logged in from)?
2. Can we read every team's roster and the league's transaction history?
3. What transaction type codes does the league's history use (claims, drops,
   trades)? The sync design depends on these, and the library's docs do not
   list them.

SAFETY
--------------------------------------------------------------------------
- Read-only: only reads league info, rosters and transactions. Never writes
  to Fantrax and never touches the database.
- The cookie is read from the FANTRAX_COOKIE environment variable and is never
  printed. Error messages are truncated. Treat the cookie like a password.
- Actions logs on a public repo are public: this prints counts, type codes and
  three sample transactions, nothing else.

EXIT CODES: 0 = everything worked, 1 = Fantrax rejected or errored, 2 = setup.
"""

import os
import sys
import time
import traceback
from collections import Counter
from datetime import datetime

import requests
from fantraxapi import League, api
from fantraxapi.exceptions import FantraxException, NotLoggedIn, NotMemberOfLeague
from fantraxapi.objs.position import Position
from fantraxapi.objs.scoring_period import ScoringPeriod
from fantraxapi.objs.status import Status

REQUEST_DELAY_SECONDS = 0.5
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class TolerantLeague(League):
    """fantraxapi 1.0.1 crashes with KeyError (e.g. 'Sep 29') when the league's
    list of scoring dates contains a date missing from the roster page's
    period dropdown. Same logic as League.reset_info, but such dates are
    skipped and recorded instead of raising."""

    skipped_dates: list = []

    def reset_info(self) -> None:
        responses = api.get_init_info(self)
        self.name = responses[0]["fantasySettings"]["leagueName"]
        self.year = responses[0]["fantasySettings"]["subtitle"]
        self.start_date = datetime.fromtimestamp(responses[0]["fantasySettings"]["season"]["startDate"] / 1e3)
        self.end_date = datetime.fromtimestamp(responses[0]["fantasySettings"]["season"]["endDate"] / 1e3)
        self.positions = {k: Position(self, v) for k, v in responses[0]["positionMap"].items()}
        self.status = {k: Status(self, v) for k, v in responses[1]["allObjs"].items() if "name" in v}
        period_to_day_list = {}
        for s in responses[4]["displayedLists"]["periodList"]:
            period, s = s.split(" ", maxsplit=1)
            period_to_day_list[s[5:-1]] = int(period)
        self.scoring_dates = {}
        self.skipped_dates = []
        for day in responses[2]["dates"]:
            scoring_date = datetime.strptime(day["object1"], "%Y-%m-%d").date()
            key = scoring_date.strftime("%b %d")
            if "0" in key and not key.endswith("0"):
                key = key.replace("0", "")
            if key not in period_to_day_list:
                self.skipped_dates.append(key)
                continue
            self.scoring_dates[period_to_day_list[key]] = scoring_date
        self.scoring_periods = {p["value"]: ScoringPeriod(self, p)
                                for p in responses[3]["displayedLists"]["scoringPeriodList"] if p["name"] != "Full Season"}
        self._scoring_periods_lookup = None
        self._update_teams(responses[3]["fantasyTeams"])


def where(e: Exception) -> str:
    """Last stack frame as file:line -- tells us where a failure happened without echoing request data."""
    tb = traceback.extract_tb(e.__traceback__)
    return f"{os.path.basename(tb[-1].filename)}:{tb[-1].lineno} in {tb[-1].name}" if tb else "unknown"


def main() -> int:
    league_id = os.environ.get("FANTRAX_LEAGUE_ID", "").strip()
    cookie = os.environ.get("FANTRAX_COOKIE", "").strip()
    if not league_id:
        print("SETUP: FANTRAX_LEAGUE_ID is not set.")
        return 2
    if not cookie:
        print("SETUP: FANTRAX_COOKIE is empty or missing. Check the repo secret AND the workflow env: mapping.")
        return 2

    session = requests.Session()
    session.headers.update({"Cookie": cookie, "User-Agent": USER_AGENT, "Accept": "application/json"})

    try:
        league = TolerantLeague(league_id, session=session)
    except NotLoggedIn:
        print("RESULT: Fantrax says NOT LOGGED IN. The cookie was rejected (expired, incomplete, or tied to another IP).")
        return 1
    except NotMemberOfLeague:
        print("RESULT: logged in, but the account is not a member of this league. Check the league id.")
        return 1
    except FantraxException as e:
        print(f"RESULT: Fantrax error: {str(e)[:300]}")
        return 1
    except Exception as e:  # noqa: BLE001 - network errors, HTML instead of JSON, etc.
        print(f"RESULT: unexpected error (Fantrax may have answered; this can be a parsing problem): "
              f"{type(e).__name__}: {str(e)[:200]} at {where(e)}")
        return 1

    print(f"OK league: {league.name} ({league.year}); teams: {len(league.teams)}")
    if league.skipped_dates:
        print(f"  note: {len(league.skipped_dates)} scoring dates not in the period list were skipped: "
              f"{league.skipped_dates[:5]}")

    # Rosters for every team
    total_rows = 0
    for team in league.teams:
        try:
            roster = league.team_roster(team.id)
        except Exception as e:  # noqa: BLE001
            print(f"  roster FAILED for team {team.id}: {type(e).__name__}: {str(e)[:150]} at {where(e)}")
            return 1
        filled = sum(1 for r in roster.rows if r.player is not None)
        total_rows += filled
        print(f"  roster {team.name}: {filled} players (active {roster.active}/{roster.active_max}, "
              f"reserve {roster.reserve}, injured {roster.injured})")
        time.sleep(REQUEST_DELAY_SECONDS)
    print(f"OK rosters: {total_rows} rostered players across {len(league.teams)} teams")

    # Transaction history
    try:
        txs = league.transactions(count=100)
    except Exception as e:  # noqa: BLE001
        print(f"RESULT: transactions FAILED: {type(e).__name__}: {str(e)[:200]} at {where(e)}")
        return 1
    type_counts = Counter(p.type for t in txs for p in t.players)
    print(f"OK transactions: {len(txs)} transaction sets; player-level type codes: {dict(type_counts)}")
    for t in txs[:3]:
        print(f"  {t.date:%Y-%m-%d %H:%M} | {t.team.name} | {[f'{p.type}:{p.name}' for p in t.players]}")

    print("RESULT: PASS -- the cookie works from this runner.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
