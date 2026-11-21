"""Clear one session (and every later one) for a participant, directly in the live database, so
they can do it again. Earlier sessions are kept. Works while the app is running; no restart needed.

Run on the server from /opt/memory-study (the script is piped into the running container):

    docker exec -i memory-study python - NICKNAME 2     < deploy/reset-session.py   # shows what would be deleted
    docker exec -i memory-study python - NICKNAME 2 yes < deploy/reset-session.py   # backs up, then deletes

Session: 1, 2 or 3. Clearing Session 2 also clears Session 3 (it depends on Session 2).
A full backup is written to data/backups/ before anything is deleted.
For a real participant a redone session isn't comparable any more: consider excluding them.
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

DB = Path(os.environ.get("STUDY_DB", "/data/study.db"))   # override only for testing
STAGES = {1: ("day0", "day2", "day9"), 2: ("day2", "day9"), 3: ("day9",)}
DONE = {"day0": "day0_done", "day2": "day2_done", "day9": "day9_done"}
NAMES = {"day0": "Session 1", "day2": "Session 2", "day9": "Session 3"}

if len(sys.argv) < 3 or sys.argv[2] not in ("1", "2", "3"):
    sys.exit("usage: ... python - NICKNAME SESSION(1|2|3) [yes] < deploy/reset-session.py")
nick, session, really = sys.argv[1], int(sys.argv[2]), len(sys.argv) > 3 and sys.argv[3] == "yes"
stages = STAGES[session]

con = sqlite3.connect(DB, timeout=30, isolation_level=None)
con.row_factory = sqlite3.Row
p = con.execute("SELECT * FROM participants WHERE nickname=? COLLATE NOCASE", (nick,)).fetchone()
if not p:
    names = [r[0] for r in con.execute("SELECT nickname FROM participants ORDER BY id")]
    sys.exit(f"No participant called {nick!r}. Nicknames: {', '.join(names)}")
pid = p["id"]
marks = ",".join("?" * len(stages))


def n(sql, *args):
    return con.execute(sql, (pid, *args)).fetchone()[0]


plan = {
    "answers": n(f"SELECT COUNT(*) FROM answers WHERE pid=? AND stage IN ({marks})", *stages),
    "steps": n(f"SELECT COUNT(*) FROM steps WHERE pid=? AND stage IN ({marks})", *stages),
    "events": n(f"SELECT COUNT(*) FROM events WHERE pid=? AND stage IN ({marks})", *stages),
    "survey": n("SELECT COUNT(*) FROM survey WHERE pid=?"),
}
if session <= 2:
    for t in ("highlights", "highlight_log", "predictions"):
        plan[t] = n(f"SELECT COUNT(*) FROM {t} WHERE pid=?")

fmt = lambda ts: time.strftime("%d %b %H:%M", time.localtime(ts)) if ts else "not done"
print(f"Participant: {p['nickname']} (id {pid}, group {p['grp']}, lang {p['lang']})")
print("  " + ", ".join(f"{NAMES[s]}: {fmt(p[DONE[s]])}" for s in ("day0", "day2", "day9")))
print(f"  currently in: {NAMES.get(p['stage'], 'no session')}" + (f", step {p['step']}" if p["stage"] else ""))
print(f"Clearing: {', '.join(NAMES[s] for s in stages)}  (kept: {', '.join(NAMES[s] for s in DONE if s not in stages) or 'nothing'})")
print("Rows to delete: " + ", ".join(f"{t} {c}" for t, c in plan.items()))

if not really:
    print(f"\nNothing changed. To do it, run the same command with 'yes' after the session number.")
    sys.exit(0)

backups = DB.parent / "backups"
backups.mkdir(exist_ok=True)
backup = backups / f"study-before-reset-s{session}-{nick}-{time.strftime('%Y-%m-%d_%H%M%S')}.db"
dst = sqlite3.connect(backup)
con.backup(dst)
dst.close()
print(f"Backup written: data/backups/{backup.name}")

con.execute("BEGIN IMMEDIATE")
try:
    for t in ("answers", "steps", "events"):
        con.execute(f"DELETE FROM {t} WHERE pid=? AND stage IN ({marks})", (pid, *stages))
    if session <= 2:
        for t in ("highlights", "highlight_log", "predictions"):
            con.execute(f"DELETE FROM {t} WHERE pid=?", (pid,))
    con.execute("DELETE FROM survey WHERE pid=?", (pid,))
    sets = ", ".join(f"{DONE[s]}=NULL" for s in stages)
    con.execute(f"UPDATE participants SET {sets}, stage=NULL, step=0, step_started=NULL, test_used=NULL"
                + (", belief=NULL" if session <= 2 else "") + " WHERE id=?", (pid,))
    con.execute("INSERT INTO events (pid,stage,step,type,ts,detail) VALUES (?,?,?,?,?,?)",
                (pid, None, "", "admin_reset", time.time(), json.dumps({"session": session, "backup": backup.name})))
    con.execute("COMMIT")
except BaseException:
    con.execute("ROLLBACK")
    raise
print(f"Done: {', '.join(NAMES[s] for s in stages)} cleared for {p['nickname']}. "
      f"They can start {NAMES[stages[0]]} again as soon as it is open (reload the page).")
