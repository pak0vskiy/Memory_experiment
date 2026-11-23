"""Sessions that were started but never finished: list them, finish the ones where all study work
is done (the person just didn't press the last Continue), or delete people who never really took
part. Works on the live database while the app runs; no restart needed.

Run on the server from /opt/memory-study (the script is piped into the running container):

    docker exec -i memory-study python - list                    < deploy/open-sessions.py
    docker exec -i memory-study python - finish NICKNAME         < deploy/open-sessions.py   # shows what it would do
    docker exec -i memory-study python - finish NICKNAME yes     < deploy/open-sessions.py
    docker exec -i memory-study python - delete NICKNAME         < deploy/open-sessions.py   # shows what it would do
    docker exec -i memory-study python - delete NICKNAME yes     < deploy/open-sessions.py

"finish" is only allowed when every step with study content is done:
  Session 1 on its last step (checking the answers)         -> nothing is lost
  Session 2 on checking the answers or the prediction        -> the prediction is missing
  Session 3 on the questionnaire                             -> the questionnaire is missing
The finish time is set to when the Continue button would have unlocked, not "now", so the 48 h /
7 day gaps count from when the person actually stopped. A backup is written to data/backups/ first.
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

DB = Path(os.environ.get("STUDY_DB", "/data/study.db"))
env = lambda k, d: int(os.environ.get(k, d))
BLOCK, FEEDBACK_MIN, TEST_MODE_SEC = env("BLOCK_SEC", 210), env("FEEDBACK_MIN_SEC", 60), env("TEST_MODE_SEC", 30)
STEPS = {"day0": ["intro", "read1", "read2", "test", "feedback"],
         "day2": ["intro", "hl1", "hl2", "test", "feedback", "predict"],
         "day9": ["intro", "final1", "final2", "survey"]}
NAMES = {"day0": "Session 1", "day2": "Session 2", "day9": "Session 3"}
STEP_NAMES = {"intro": "instructions", "read1": "reading text 1", "read2": "reading text 2", "test": "self-test",
              "feedback": "checking answers", "hl1": "highlighting round 1", "hl2": "highlighting round 2",
              "predict": "prediction", "final1": "final test 1", "final2": "final test 2", "survey": "questionnaire"}
# step from which a session may be finished by hand, and what is lost by doing so
FINISHABLE = {("day0", "feedback"): "nothing", ("day2", "feedback"): "the prediction",
              ("day2", "predict"): "the prediction", ("day9", "survey"): "the questionnaire"}
DONE = {"day0": "day0_done", "day2": "day2_done", "day9": "day9_done"}

args = sys.argv[1:]
if not args or args[0] not in ("list", "finish", "delete") or (args[0] != "list" and len(args) < 2):
    sys.exit(__doc__)
cmd, nick, really = args[0], (args[1] if len(args) > 1 else None), args[-1] == "yes" and len(args) > 2

con = sqlite3.connect(DB, timeout=30, isolation_level=None)
con.row_factory = sqlite3.Row
now = time.time()
ago = lambda ts: f"{(now - ts) / 3600:.1f} h ago" if ts else "?"


def describe(p):
    stage = p["stage"]
    step = STEPS[stage][p["step"]] if stage in STEPS and p["step"] < len(STEPS[stage]) else "?"
    lost = FINISHABLE.get((stage, step))
    verdict = f"all work done; finishing loses {lost}" if lost else "incomplete"
    return stage, step, lost, verdict


def backup(label):
    (DB.parent / "backups").mkdir(exist_ok=True)
    b = DB.parent / "backups" / f"study-before-{label}-{nick}-{time.strftime('%Y-%m-%d_%H%M%S')}.db"
    dst = sqlite3.connect(b)
    con.backup(dst)
    dst.close()
    print(f"Backup written: data/backups/{b.name}")
    return b


if cmd == "list":
    rows = con.execute("SELECT * FROM participants WHERE stage IS NOT NULL ORDER BY step_started").fetchall()
    never = con.execute("SELECT * FROM participants WHERE stage IS NULL AND day0_done IS NULL").fetchall()
    if not rows and not never:
        print("No open sessions.")
    for p in rows:
        stage, step, lost, verdict = describe(p)
        flags = " [test mode]" * bool(p["tester"]) + " [excluded]" * bool(p["excluded"])
        print(f"{p['nickname']:<16} {NAMES[stage]}, on '{STEP_NAMES.get(step, step)}' since {ago(p['step_started'])}  ->  {verdict}{flags}")
    for p in never:
        print(f"{p['nickname']:<16} signed up {ago(p['created_at'])}, never opened Session 1  ->  incomplete")
    print("\nfinish: '... python - finish NICKNAME' (then add 'yes')   delete: '... python - delete NICKNAME' (then add 'yes')")
    sys.exit(0)

p = con.execute("SELECT * FROM participants WHERE nickname=? COLLATE NOCASE", (nick,)).fetchone()
if not p:
    names = [r[0] for r in con.execute("SELECT nickname FROM participants ORDER BY id")]
    sys.exit(f"No participant called {nick!r}. Nicknames: {', '.join(names)}")
nick = p["nickname"]

if cmd == "delete":
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t} WHERE pid=?", (p["id"],)).fetchone()[0]
              for t in ("answers", "highlights", "highlight_log", "predictions", "events", "steps", "survey")}
    print(f"Delete {nick}: done " + ", ".join(f"{NAMES[s]} {'yes' if p[DONE[s]] else 'no'}" for s in STEPS)
          + "\nRows: " + ", ".join(f"{t} {c}" for t, c in counts.items()))
    if not really:
        print("\nNothing changed. Add 'yes' to delete. (To keep the data but leave them out of the results, use Exclude on the admin page instead.)")
        sys.exit(0)
    backup("delete")
    con.execute("BEGIN IMMEDIATE")
    for t in counts:
        con.execute(f"DELETE FROM {t} WHERE pid=?", (p["id"],))
    con.execute("DELETE FROM participants WHERE id=?", (p["id"],))
    con.execute("COMMIT")
    print(f"Deleted {nick}.")
    sys.exit(0)

# finish
if not p["stage"]:
    sys.exit(f"{nick} has no open session.")
stage, step, lost, verdict = describe(p)
print(f"{nick}: {NAMES[stage]}, on '{STEP_NAMES.get(step, step)}' since {ago(p['step_started'])}")
if not lost:
    sys.exit(f"Can't finish: the session is incomplete (stopped at '{STEP_NAMES.get(step, step)}'). "
             "Ask them to continue (the page waits for them), or use Exclude/delete.")
if step == "feedback":   # when Continue unlocked: the rest of the self-test + feedback block
    block, fmin = (TEST_MODE_SEC, 5) if p["tester"] else (BLOCK, FEEDBACK_MIN)
    done_at = p["step_started"] + max(fmin, block - (p["test_used"] or 0))
else:                    # prediction / questionnaire have no timer: the moment the step was reached
    done_at = p["step_started"]
done_at = min(done_at, now)
when = time.strftime("%d %b %H:%M UTC", time.gmtime(done_at)) + time.strftime(" (%d %b %H:%M KST)", time.gmtime(done_at + 9 * 3600))
print(f"Will mark {NAMES[stage]} as finished at {when} (when the last Continue would have unlocked). Lost: {lost}.")
if not really:
    print("\nNothing changed. Add 'yes' to do it.")
    sys.exit(0)
backup("finish")
con.execute("BEGIN IMMEDIATE")
con.execute(f"UPDATE participants SET {DONE[stage]}=?, stage=NULL, step=0, step_started=NULL WHERE id=?", (done_at, p["id"]))
if step == "feedback":   # keep the steps table complete for the analysis
    con.execute("INSERT INTO steps (pid,stage,step_id,type,text_id,condition,started,finished,elapsed_sec,hidden_n,away_sec,extra)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (p["id"], stage, "feedback", "feedback", None, None, p["step_started"], done_at,
                 round(done_at - p["step_started"], 2), 0, 0.0, json.dumps({"admin_finished": True})))
con.execute("INSERT INTO events (pid,stage,step,type,ts,detail) VALUES (?,?,?,?,?,?)",
            (p["id"], None, step, "admin_finish", now, json.dumps({"session": NAMES[stage], "done_at": done_at, "lost": lost})))
con.execute("COMMIT")
print(f"Done: {NAMES[stage]} finished for {nick}. Their next session opens on the usual schedule from {when}.")
