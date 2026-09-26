"""
nhl_add_player.py -- run locally when the dashboard's Skater check says a player needs attention.

    python nhl_add_player.py            # walk through everything open, one player at a time
    python nhl_add_player.py --list     # just print what's open, change nothing

Two queues are handled:

  1. Lineup side (player_id_review_queue): players DailyFaceoff lists that the model can't match to a
     player it knows (brand-new players), or that need a human call (trade / name collision).
       - New player -> Rookie (draft round + position) or Veteran (gp, points, toi).
         A provisional player row 'dfo_<id>' is created so he can be in lineup snapshots right away.
       - Once a player is resolved here he is never asked about again.
  2. Results side (player_review_queue): players who showed up in a box score (real Hockey-Reference id)
     with no rookie / prior-season row. Enter them as Rookie / Veteran, or LINK them to a player you
     already entered from DailyFaceoff under a different spelling of the name.

Connection: same env vars as the GitHub Actions scripts (PGHOST, PGUSER, PGPASSWORD, PGDATABASE, PGPORT),
or a single DATABASE_URL.

Model facts this script relies on (checked against the existing rows / views):
  ppg = pts / gp        atoi = toi_total_minutes / gp        pv = atoi + atoi * ppg
  rookie pv comes from rookie_pv_lookup (draft round x F/D); age is not used anywhere, so it's not asked.
"""

import os
import sys

import psycopg2

VALID_ROUNDS = {"1", "2", "3", "4", "5", "6", "7", "U"}


# --------------------------- helpers ---------------------------

def get_conn():
    url = os.environ.get("DATABASE_URL")
    if url:
        return psycopg2.connect(url)
    if os.environ.get("PGHOST"):
        return psycopg2.connect()
    sys.exit("Set PGHOST / PGUSER / PGPASSWORD / PGDATABASE / PGPORT (or DATABASE_URL) first.")


def norm(name):
    """Key used by rookie_projections / prior_season_stats / the name bridge view: lower(trim(display))."""
    return (name or "").strip().lower()


def ask(prompt, valid=None, default=None, cast=None):
    while True:
        raw = input(prompt + (f" [{default}]" if default is not None else "") + ": ").strip()
        if raw == "" and default is not None:
            raw = str(default)
        if valid is not None and raw.upper() not in {v.upper() for v in valid}:
            print(f"  enter one of: {', '.join(sorted(valid))}")
            continue
        if cast is not None:
            try:
                return cast(raw)
            except ValueError:
                print("  not a valid number")
                continue
        return raw


def confirm(prompt):
    return input(prompt + " [y/N]: ").strip().lower() == "y"


def season_of_game(cur, game_id):
    cur.execute("select season from games where game_id = %s", (game_id,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("select max(season) from games where playoff = false and home_goals is null")
    return cur.fetchone()[0]


def prior_season(season):
    return str(int(season) - 101)  # '2627' -> '2526'


def pos_group_default(pos):
    return {"F": "F", "D": "D", "G": "G"}.get((pos or "").upper(), "F")


# --------------------------- data entry ---------------------------

def enter_rookie(display, pos_hint):
    print("  ROOKIE (never played in the NHL before)")
    rnd = ask("  Draft round (1-7, or U for undrafted)", valid=VALID_ROUNDS).upper()
    pos = ask("  Position (C / LW / RW / F / D)", valid={"C", "LW", "RW", "W", "F", "D"},
              default=pos_hint if pos_hint in ("F", "D") else None).upper()
    return {"kind": "rookie", "draft_round": rnd, "pos": pos}


def enter_veteran(display, pos_hint):
    print("  VETERAN (played before, fell out of the model). Enter the totals you want counted --")
    print("  one season or several consolidated, whatever you decide.")
    pos = ask("  Position (C / LW / RW / F / D)", valid={"C", "LW", "RW", "W", "F", "D"},
              default=pos_hint if pos_hint in ("F", "D") else None).upper()
    gp = ask("  GP", cast=int)
    tp = ask("  Points (total)", cast=int)
    raw = ask("  TOI: total minutes, or average minutes per game with an 'a' after it (e.g. 17.5a)")
    try:
        toi = float(raw[:-1]) * gp if raw.lower().endswith("a") else float(raw)
    except ValueError:
        print("  couldn't read that TOI, try again")
        return enter_veteran(display, pos_hint)
    if gp <= 0:
        print("  GP must be positive")
        return enter_veteran(display, pos_hint)
    ppg = tp / gp
    atoi = toi / gp
    pv = atoi + atoi * ppg
    print(f"  -> ppg {ppg:.3f}   atoi {atoi:.2f}   pv {pv:.2f}")
    return {"kind": "veteran", "pos": pos, "gp": gp, "tp": tp, "toi": toi, "ppg": ppg, "atoi": atoi, "pv": pv}


def write_stats(cur, display, season, entry):
    key = norm(display)
    if entry["kind"] == "rookie":
        cur.execute(
            "insert into rookie_projections (player_name_normalized, player_name_display, season, pos, draft_round) "
            "values (%s, %s, %s, %s, %s) on conflict (player_name_normalized, season) "
            "do update set pos = excluded.pos, draft_round = excluded.draft_round",
            (key, display, season, entry["pos"], entry["draft_round"]))
    else:
        cur.execute(
            "insert into prior_season_stats (player_name_normalized, player_name_display, season, pos, gp, tp, toi, "
            "ppg, atoi, pv) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "on conflict (player_name_normalized, season) do update set pos = excluded.pos, gp = excluded.gp, "
            "tp = excluded.tp, toi = excluded.toi, ppg = excluded.ppg, atoi = excluded.atoi, pv = excluded.pv",
            (key, display, prior_season(season), entry["pos"], entry["gp"], entry["tp"], entry["toi"],
             entry["ppg"], entry["atoi"], entry["pv"]))


def entry_summary(display, season, entry):
    if entry["kind"] == "rookie":
        return f"{display}: ROOKIE, round {entry['draft_round']}, {entry['pos']} (season {season})"
    return (f"{display}: VETERAN, {entry['pos']}, {entry['gp']} GP / {entry['tp']} pts / {entry['toi']:.0f} min "
            f"-> pv {entry['pv']:.2f} (stored as season {prior_season(season)})")


# --------------------------- lineup-side queue ---------------------------

def open_lineup_rows(cur):
    cur.execute("select id, source, dfo_player_id, player_name_display, pos, team_code, reason, game_id "
                "from player_id_review_queue where status = 'open' order by first_seen_at, id")
    return cur.fetchall()


def backfill_preliminary(cur, game_id, team, player_id):
    """The lineup ingest never overwrites a snapshot, so a player who was unmatched when the preliminary
    lineup was written is missing from it. Add him. The confirmed snapshot is written at T-5 from a fresh
    scrape, so if it hasn't happened yet he'll be in it automatically."""
    if not game_id or not team:
        return
    cur.execute("select distinct snapshot_type from lineup_snapshots where game_id = %s and team_code = %s",
                (game_id, team))
    have = {r[0] for r in cur.fetchall()}
    if "preliminary" in have:
        cur.execute("insert into lineup_snapshots (game_id, team_code, player_id, snapshot_type, scraped_at) "
                    "values (%s, %s, %s, 'preliminary', now()) on conflict do nothing", (game_id, team, player_id))
        print("  added to the preliminary lineup for that game")
    if "confirmed" in have:
        print("  WARNING: the confirmed lineup for that game was already frozen WITHOUT this player, and the")
        print("  bet signal may already be frozen too. It can't be redone for that game.")


def handle_lineup_row(conn, row):
    qid, source, dfo_id, name, pos, team, reason, game_id = row
    print(f"\n[lineup] {name}  ({team}, {pos or '?'})  -- {reason}")
    cur = conn.cursor()

    if not reason.startswith("no match"):
        # Trade / name collision: the player exists, a human just has to confirm who he is.
        from_nm = norm(name)
        cur.execute("select player_id, player_name_display, position from players where player_id not like 'dfo\\_%%' "
                    "and lower(trim(player_name_display)) = %s", (from_nm,))
        cands = cur.fetchall()
        if not cands:
            print("  no existing player with that exact name; treating as a new player.")
        else:
            for i, c in enumerate(cands, 1):
                print(f"  {i}) {c[0]}  {c[1]}  {c[2] or ''}")
            pick = ask("  Which one is he? (number, N = new player, S = skip)",
                       valid={str(i) for i in range(1, len(cands) + 1)} | {"N", "S"})
            if pick.upper() == "S":
                return
            if pick.upper() != "N":
                pid = cands[int(pick) - 1][0]
                if confirm(f"  Confirm {name} = {pid} for good?"):
                    cur.execute("update player_id_review_queue set status = 'resolved', resolved_player_id = %s, "
                                "notes = 'confirmed via nhl_add_player' where id = %s", (pid, qid))
                    backfill_preliminary(cur, game_id, team, pid)
                    conn.commit()
                    print("  done.")
                return

    grp = pos_group_default(pos)
    if grp == "G":
        if confirm("  Goalie: no stats needed. Add him as a player?"):
            pid = f"dfo_{dfo_id}"
            cur.execute("insert into players (player_id, player_name_display, position) values (%s, %s, 'G') "
                        "on conflict (player_id) do nothing", (pid, name))
            cur.execute("update player_id_review_queue set status = 'resolved', resolved_player_id = %s, "
                        "notes = 'goalie added via nhl_add_player' where id = %s", (pid, qid))
            conn.commit()
            print("  done. The next goalie run will pick him up.")
        return

    kind = ask("  [R]ookie, [V]eteran, or [S]kip", valid={"R", "V", "S"}).upper()
    if kind == "S":
        return
    entry = enter_rookie(name, grp) if kind == "R" else enter_veteran(name, grp)
    season = season_of_game(cur, game_id)
    print("  " + entry_summary(name, season, entry))
    if not confirm("  Write this?"):
        print("  not saved.")
        return
    pid = f"dfo_{dfo_id}"
    cur.execute("insert into players (player_id, player_name_display, position) values (%s, %s, %s) "
                "on conflict (player_id) do nothing", (pid, name, entry["pos"]))
    write_stats(cur, name, season, entry)
    cur.execute("update player_id_review_queue set status = 'resolved', resolved_player_id = %s, "
                "notes = %s where id = %s", (pid, f"{entry['kind']} entered via nhl_add_player", qid))
    backfill_preliminary(cur, game_id, team, pid)
    conn.commit()
    print("  saved.")


# --------------------------- results-side queue ---------------------------

def open_results_rows(cur):
    cur.execute("select player_id, player_name_display, team_code, first_game_id, first_game_date, reason "
                "from player_review_queue where status = 'open' order by first_game_date, player_id")
    return cur.fetchall()


def link_to_provisional(conn, hr_id, hr_name):
    cur = conn.cursor()
    cur.execute("select player_id, player_name_display from players where player_id like 'dfo\\_%' order by 2")
    provs = cur.fetchall()
    if not provs:
        print("  no players entered from DailyFaceoff to link to.")
        return False
    for i, p in enumerate(provs, 1):
        print(f"  {i}) {p[1]}  ({p[0]})")
    pick = ask("  Which one is the same person? (number, S = cancel)",
               valid={str(i) for i in range(1, len(provs) + 1)} | {"S"})
    if pick.upper() == "S":
        return False
    prov_id, prov_name = provs[int(pick) - 1]
    if not confirm(f"  Merge '{prov_name}' ({prov_id}) into {hr_name} ({hr_id})?"):
        return False
    # The stats rows are keyed by the DailyFaceoff spelling; the alias lets the views find them for the real id.
    cur.execute("insert into player_aliases (player_id, alias_name_normalized, source) values (%s, %s, 'manual_review') "
                "on conflict do nothing", (hr_id, norm(prov_name)))
    cur.execute("update lineup_snapshots s set player_id = %s where s.player_id = %s and not exists ("
                "select 1 from lineup_snapshots x where x.game_id = s.game_id and x.team_code = s.team_code "
                "and x.snapshot_type = s.snapshot_type and x.player_id = %s)", (hr_id, prov_id, hr_id))
    cur.execute("delete from lineup_snapshots where player_id = %s", (prov_id,))
    cur.execute("update player_id_review_queue set resolved_player_id = %s where resolved_player_id = %s",
                (hr_id, prov_id))
    cur.execute("delete from player_aliases where player_id = %s", (prov_id,))
    cur.execute("delete from players where player_id = %s", (prov_id,))
    cur.execute("update player_review_queue set status = 'resolved', notes = %s where player_id = %s",
                (f"linked to DailyFaceoff entry {prov_name}", hr_id))
    conn.commit()
    print("  merged.")
    return True


def handle_results_row(conn, row):
    pid, name, team, gid, gdate, reason = row
    print(f"\n[results] {name}  ({team}, first game {gdate})  -- {reason}")
    cur = conn.cursor()
    kind = ask("  [R]ookie, [V]eteran, [L]ink to a player you already entered, or [S]kip",
               valid={"R", "V", "L", "S"}).upper()
    if kind == "S":
        return
    if kind == "L":
        link_to_provisional(conn, pid, name)
        return
    entry = enter_rookie(name, None) if kind == "R" else enter_veteran(name, None)
    season = season_of_game(cur, gid)
    print("  " + entry_summary(name, season, entry))
    if not confirm("  Write this?"):
        print("  not saved.")
        return
    write_stats(cur, name, season, entry)
    cur.execute("update player_review_queue set status = 'resolved', notes = %s where player_id = %s",
                (f"{entry['kind']} entered via nhl_add_player", pid))
    conn.commit()
    print("  saved.")


# --------------------------- known player, no stats (NULL PVAdj) ---------------------------

def open_null_pvadj_rows(cur):
    """Skaters in a lineup snapshot for a game that hasn't been played, who matched a player row (often a
    veteran with an old Hockey-Reference id) but have no prior-season / rookie stats -> PVAdj is NULL.
    These never appear in either review queue, so they're found straight from the pvadj view."""
    cur.execute("""
        select v.player_id, v.player_name_display, v.team_code, min(v.game_id) as first_game
        from v_pregame_lineup_pvadj_players v
        join games g on g.game_id = v.game_id
        where v.pvadj is null and g.playoff = false and g.home_goals is null and g.date >= current_date - 1
        group by 1, 2, 3
        order by min(g.date), 2""")
    return cur.fetchall()


def handle_null_pvadj_row(conn, row):
    pid, name, team, gid = row
    print(f"\n[no stats] {name}  ({team}, in the lineup for {gid}) -- known player, no prior-season or rookie row")
    cur = conn.cursor()
    kind = ask("  [R]ookie, [V]eteran, or [S]kip", valid={"R", "V", "S"}).upper()
    if kind == "S":
        return
    entry = enter_rookie(name, None) if kind == "R" else enter_veteran(name, None)
    season = season_of_game(cur, gid)
    print("  " + entry_summary(name, season, entry))
    if not confirm("  Write this?"):
        print("  not saved.")
        return
    write_stats(cur, name, season, entry)
    conn.commit()
    print("  saved.")


# --------------------------- main ---------------------------

def main():
    list_only = "--list" in sys.argv
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            lineup = open_lineup_rows(cur)
            results = open_results_rows(cur)
            nostats = open_null_pvadj_rows(cur)
        print(f"Open: {len(lineup)} unmatched lineup player(s), {len(nostats)} lineup player(s) with no stats, "
              f"{len(results)} results-side player(s).")
        for r in lineup:
            print(f"  unmatched : {r[3]} ({r[5]}) -- {r[6]}")
        for r in nostats:
            print(f"  no stats  : {r[1]} ({r[2]}, {r[3]})")
        for r in results:
            print(f"  results   : {r[1]} ({r[2]}, {r[4]}) -- {r[5]}")
        if list_only or not (lineup or results or nostats):
            return
        for r in lineup:
            handle_lineup_row(conn, r)
        for r in nostats:
            handle_null_pvadj_row(conn, r)
        for r in results:
            handle_results_row(conn, r)
        with conn.cursor() as cur:
            left = (len(open_lineup_rows(cur)), len(open_null_pvadj_rows(cur)), len(open_results_rows(cur)))
        print(f"\nStill open: {left[0]} unmatched, {left[1]} no stats, {left[2]} results-side.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
