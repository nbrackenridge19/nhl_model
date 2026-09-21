"""
nhl_check_sources.py -- connectivity check, meant to run on GitHub Actions (check-sources.yml).

Purpose: the daily ingest failed on GitHub because ESPN answered with something that was not JSON.
Sites often treat GitHub's servers differently from a home computer, so this asks every source the
project depends on for one small page and prints what each returned (status, size, first characters).
It reads nothing from and writes nothing to the database.
"""

import time

import requests

PLAIN = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
}

CHECKS = [
    # (label, url, headers, what a good answer looks like)
    ("ESPN scoreboard", "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard?dates=20260920", PLAIN, "json"),
    ("ESPN game summary", "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/summary?event=401879644", PLAIN, "json"),
    ("ESPN odds", "https://sports.core.api.espn.com/v2/sports/hockey/leagues/nhl/events/401879644/competitions/401879644/odds", PLAIN, "json"),
    ("NHL.com scores", "https://api-web.nhle.com/v1/score/2026-09-20", PLAIN, "json"),
    ("NHL.com box score", "https://api-web.nhle.com/v1/gamecenter/2026010008/boxscore", PLAIN, "json"),
    ("NHL.com right-rail", "https://api-web.nhle.com/v1/gamecenter/2026010008/right-rail", PLAIN, "json"),
    ("Hockey-Reference box score", "https://www.hockey-reference.com/boxscores/202510070FLA.html", PLAIN, "html"),
    ("Hockey-Reference schedule", "https://www.hockey-reference.com/leagues/NHL_2027_games.html", PLAIN, "html"),
    ("DailyFaceoff goalies", "https://www.dailyfaceoff.com/starting-goalies/2025-12-09", BROWSER, "html"),
    ("DailyFaceoff lines", "https://www.dailyfaceoff.com/teams/new-jersey-devils/line-combinations", BROWSER, "html"),
]


def check(label, url, headers, kind):
    t0 = time.time()
    try:
        r = requests.get(url, headers=headers, timeout=25)
    except Exception as e:  # noqa: BLE001
        return {"label": label, "status": "ERROR", "ok": False, "note": str(e)[:150], "secs": time.time() - t0}
    ctype = r.headers.get("content-type", "")
    body = r.text
    ok = r.status_code == 200
    if ok and kind == "json":
        try:
            r.json()
        except Exception:  # noqa: BLE001
            ok = False
    if ok and kind == "html" and "denied" in body[:500].lower():
        ok = False
    return {"label": label, "status": r.status_code, "ok": ok, "ctype": ctype.split(";")[0], "size": len(body),
            "server": r.headers.get("server"), "start": body[:120].replace("\n", " "), "secs": time.time() - t0}


def main():
    results = []
    for label, url, headers, kind in CHECKS:
        res = check(label, url, headers, kind)
        results.append(res)
        print(f"{'OK  ' if res['ok'] else 'FAIL'} {label}: HTTP {res['status']} in {res['secs']:.1f}s"
              + (f" | {res.get('ctype')} {res.get('size')} bytes | server={res.get('server')}" if 'size' in res else f" | {res['note']}"))
        if not res["ok"]:
            print(f"       body starts: {res.get('start', '')!r}")
        time.sleep(3)
    print("\nSUMMARY")
    for res in results:
        print(f"  {'OK  ' if res['ok'] else 'FAIL'}  {res['label']}")


if __name__ == "__main__":
    main()
