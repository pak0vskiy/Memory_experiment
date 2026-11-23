"""Interactive helper for the live database: see every participant, finish sessions that were left
open after all the work was done, exclude, restart a session or delete. You only type numbers,
single letters and "yes" (no nicknames). Works while the app runs; no restart needed.

Run on the server:
    cd /opt/memory-study && python3 deploy/open-sessions.py

Rules:
  finish   only when every step with study content is done:
             Session 1 on "checking answers"            -> nothing is lost
             Session 2 on "checking answers"/prediction -> the prediction is missing
             Session 3 on the questionnaire              -> the questionnaire is missing
           The finish time is when the last Continue would have unlocked (not "now"), so the 48 h /
           7 day gaps keep their schedule.
  exclude  keeps the data, leaves the person out of the results (same as Exclude on the admin page)
  restart  clears the open (or last) session and every later one, so it can be done again
  delete   removes the person and all their data
Every change writes a full backup to data/backups/ first.
"""
import json
import os
import sqlite3
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # /opt/memory-study
DB = Path(os.environ.get("STUDY_DB") or ("/data/study.db" if Path("/data/study.db").exists() else HERE / "data" / "study.db"))


def read_env():
    """Timings from .env (the same file the container uses)."""
    vals = {}
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    get = lambda k, d: int(os.environ.get(k) or vals.get(k) or d)
    return get("BLOCK_SEC", 210), get("FEEDBACK_MIN_SEC", 60), get("TEST_MODE_SEC", 30)


BLOCK, FEEDBACK_MIN, TEST_MODE_SEC = read_env()
STEPS = {"day0": ["intro", "read1", "read2", "test", "feedback"],
         "day2": ["intro", "hl1", "hl2", "test", "feedback", "predict"],
         "day9": ["intro", "final1", "final2", "survey"]}
NAMES = {"day0": "Session 1", "day2": "Session 2", "day9": "Session 3"}
STEP_NAMES = {"intro": "instructions", "read1": "reading text 1", "read2": "reading text 2", "test": "self-test",
              "feedback": "checking answers", "hl1": "highlighting 1", "hl2": "highlighting 2",
              "predict": "prediction", "final1": "final test 1", "final2": "final test 2", "survey": "questionnaire"}
FINISHABLE = {("day0", "feedback"): "nothing", ("day2", "feedback"): "the prediction",
              ("day2", "predict"): "the prediction", ("day9", "survey"): "the questionnaire"}
DONE = {"day0": "day0_done", "day2": "day2_done", "day9": "day9_done"}
TABLES = ("answers", "highlights", "highlight_log", "predictions", "events", "steps", "survey")

con = sqlite3.connect(DB, timeout=30, isolation_level=None)
con.row_factory = sqlite3.Row
kst = lambda ts: time.strftime("%d %b %H:%M", time.gmtime(ts + 9 * 3600)) + " KST"


def pad(text, width):
    """Cut/pad to a screen width; Korean/Chinese characters take two columns."""
    out, w = "", 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if w + cw > width:
            break
        out, w = out + ch, w + cw
    return out + " " * (width - w)


def hours(ts):
    return f"{(time.time() - ts) / 3600:.1f} h ago"


def status(p):
    """(sort key, text, what finishing loses or None if it can't be finished)."""
    if p["stage"] in STEPS:
        st = STEPS[p["stage"]]
        step = st[p["step"]] if p["step"] < len(st) else "?"
        lost = FINISHABLE.get((p["stage"], step))
        verdict = f"all work done -> can finish (loses {lost})" if lost else "incomplete"
        return 0, f"{NAMES[p['stage']]}, on '{STEP_NAMES.get(step, step)}' since {hours(p['step_started'])}: {verdict}", lost
    if p["day9_done"]:
        return 3, "finished all 3 sessions", None
    if p["day2_done"]:
        return 2, f"finished Session 2 ({kst(p['day2_done'])}), Session 3 next", None
    if p["day0_done"]:
        return 2, f"finished Session 1 ({kst(p['day0_done'])}), Session 2 next", None
    return 1, f"signed up {hours(p['created_at'])}, never opened Session 1", None


def backup(label, p):
    d = DB.parent / "backups"
    d.mkdir(exist_ok=True)
    b = d / f"study-before-{label}-id{p['id']}-{time.strftime('%Y-%m-%d_%H%M%S')}.db"
    dst = sqlite3.connect(b)
    con.backup(dst)
    dst.close()
    print(f"  backup: data/backups/{b.name}")


def confirm(text):
    print(text)
    return input("  type yes to confirm: ").strip().lower() == "yes"


def do_finish(p, lost):
    step = STEPS[p["stage"]][p["step"]]
    if step == "feedback":    # when Continue unlocked: the rest of the self-test + feedback block
        block, fmin = (TEST_MODE_SEC, 5) if p["tester"] else (BLOCK, FEEDBACK_MIN)
        done_at = p["step_started"] + max(fmin, block - (p["test_used"] or 0))
    else:                     # prediction / questionnaire have no timer
        done_at = p["step_started"]
    done_at = min(done_at, time.time())
    if not confirm(f"  Mark {NAMES[p['stage']]} as finished at {kst(done_at)} (when the last Continue would have unlocked).\n"
                   f"  Lost: {lost}. The next session opens on the usual schedule from that time."):
        return print("  cancelled")
    backup("finish", p)
    con.execute("BEGIN IMMEDIATE")
    con.execute(f"UPDATE participants SET {DONE[p['stage']]}=?, stage=NULL, step=0, step_started=NULL WHERE id=?", (done_at, p["id"]))
    if step == "feedback":
        con.execute("INSERT INTO steps (pid,stage,step_id,type,text_id,condition,started,finished,elapsed_sec,hidden_n,away_sec,extra)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (p["id"], p["stage"], "feedback", "feedback", None, None, p["step_started"], done_at,
                     round(done_at - p["step_started"], 2), 0, 0.0, json.dumps({"admin_finished": True})))
    con.execute("INSERT INTO events (pid,stage,step,type,ts,detail) VALUES (?,?,?,?,?,?)",
                (p["id"], None, step, "admin_finish", time.time(), json.dumps({"session": NAMES[p["stage"]], "done_at": done_at, "lost": lost})))
    con.execute("COMMIT")
    print("  done")


def do_exclude(p):
    new = 0 if p["excluded"] else 1
    if not confirm(f"  {'Exclude' if new else 'Include again'}: the data is kept; excluded people are left out of the results."):
        return print("  cancelled")
    con.execute("UPDATE participants SET excluded=? WHERE id=?", (new, p["id"]))
    print("  done")


def do_restart(p):
    if p["stage"] in STEPS:
        session = {"day0": 1, "day2": 2, "day9": 3}[p["stage"]]
    else:
        session = 3 if p["day2_done"] else 2 if p["day0_done"] else 1
    stages = {1: ("day0", "day2", "day9"), 2: ("day2", "day9"), 3: ("day9",)}[session]
    if not confirm(f"  Clear Session {session}" + (" and later sessions" if session < 3 else "")
                   + " so it can be done again from the start (earlier sessions are kept).\n"
                   "  For a real participant the redone session isn't fully comparable (they've seen part of it)."):
        return print("  cancelled")
    backup(f"restart-s{session}", p)
    marks = ",".join("?" * len(stages))
    con.execute("BEGIN IMMEDIATE")
    for t in ("answers", "steps", "events"):
        con.execute(f"DELETE FROM {t} WHERE pid=? AND stage IN ({marks})", (p["id"], *stages))
    if session <= 2:
        for t in ("highlights", "highlight_log", "predictions"):
            con.execute(f"DELETE FROM {t} WHERE pid=?", (p["id"],))
    con.execute("DELETE FROM survey WHERE pid=?", (p["id"],))
    sets = ", ".join(f"{DONE[s]}=NULL" for s in stages)
    con.execute(f"UPDATE participants SET {sets}, stage=NULL, step=0, step_started=NULL, test_used=NULL"
                + (", belief=NULL" if session <= 2 else "") + " WHERE id=?", (p["id"],))
    con.execute("INSERT INTO events (pid,stage,step,type,ts,detail) VALUES (?,?,?,?,?,?)",
                (p["id"], None, "", "admin_reset", time.time(), json.dumps({"session": session})))
    con.execute("COMMIT")
    print(f"  done: Session {session} starts from the beginning when they open the page")


def do_delete(p):
    n = sum(con.execute(f"SELECT COUNT(*) FROM {t} WHERE pid=?", (p["id"],)).fetchone()[0] for t in TABLES)
    if not confirm(f"  Permanently delete this person and all {n} rows of their data?"):
        return print("  cancelled")
    backup("delete", p)
    con.execute("BEGIN IMMEDIATE")
    for t in TABLES:
        con.execute(f"DELETE FROM {t} WHERE pid=?", (p["id"],))
    con.execute("DELETE FROM participants WHERE id=?", (p["id"],))
    con.execute("COMMIT")
    print("  deleted")


def main():
    print(f"database: {DB}")
    while True:
        ps = [dict(r) for r in con.execute("SELECT * FROM participants")]
        ps.sort(key=lambda p: (status(p)[0], p["id"]))
        # people are chosen by their id, which never changes (list positions shift after each action)
        print("\n  id  nickname         lang grp  status")
        for p in ps:
            flags = " [TEST]" * bool(p["tester"]) + " [EXCLUDED]" * bool(p["excluded"])
            print(f"{p['id']:4}  {pad(p['nickname'], 16)} {p['lang']:<4} {p['grp']}    {status(p)[1]}{flags}")
        choice = input("\nid of a person (Enter = quit): ").strip()
        if not choice:
            return
        byid = {str(p["id"]): p for p in ps}
        if choice not in byid:
            print("  no person with that id")
            continue
        p = byid[choice]
        _, text, lost = status(p)
        print(f"\nid {p['id']} ({p['lang']}, group {p['grp']}, signed up {kst(p['created_at'])}): {text}")
        opts = (["f = finish"] if lost else []) + [("x = include again" if p["excluded"] else "x = exclude"),
                                                   "r = restart session", "d = delete", "Enter = back"]
        a = input("  " + "   ".join(opts) + "\n  > ").strip().lower()
        if a == "f" and lost:
            do_finish(p, lost)
        elif a == "x":
            do_exclude(p)
        elif a == "r":
            do_restart(p)
        elif a == "d":
            do_delete(p)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print()
