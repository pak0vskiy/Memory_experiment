"""Restore ONE participant from a backup into the live database, leaving everyone else as they are.
Use it to undo a mistaken delete, restart or finish for a single person. Interactive: you choose the
backup and the person by number and confirm with "yes" (no nicknames to type). The app keeps running.

    cd /opt/memory-study && python3 deploy/restore-participant.py

What happens: the person's current rows (if any) are removed and their participant row and all their
rows from the backup are copied in. Their login (nickname, PIN, token) comes back as it was, so their
browser stays logged in. A backup of the current database is written first, so this can be undone too.
"""
import os
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
DB = Path(os.environ.get("STUDY_DB") or HERE / "data" / "study.db")
BACKUPS = DB.parent / "backups"
CHILD = ("answers", "highlights", "highlight_log", "predictions", "events", "steps", "survey")
NAMES = {"day0_done": "S1", "day2_done": "S2", "day9_done": "S3"}


def pad(text, width):
    out, w = "", 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if w + cw > width:
            break
        out, w = out + ch, w + cw
    return out + " " * (width - w)


def summary(con, pid):
    """One line: which sessions are done, and how many rows per table."""
    p = con.execute("SELECT * FROM participants WHERE id=?", (pid,)).fetchone()
    if not p:
        return "not present"
    done = " ".join(f"{k}✓" if p[c] else f"{k}·" for c, k in NAMES.items())
    open_ = f", in {p['stage']} step {p['step']}" if p["stage"] else ""
    counts = ", ".join(f"{t} {con.execute(f'SELECT COUNT(*) FROM {t} WHERE pid=?', (pid,)).fetchone()[0]}" for t in ("answers", "highlights", "predictions", "survey"))
    return f"{done}{open_}  ({counts})"


def choose(prompt, n):
    a = input(prompt).strip()
    if not a:
        sys.exit("nothing changed")
    if not a.isdigit() or not 1 <= int(a) <= n:
        sys.exit("not a number from the list; nothing changed")
    return int(a)


def main():
    # oldest first: new backups (including the safety copy each restore makes) are added at the
    # bottom, so the numbers of existing backups never change between runs
    files = sorted(BACKUPS.glob("*.db"), key=lambda f: (f.stat().st_mtime, f.name))
    if not files:
        sys.exit(f"no backups in {BACKUPS}")
    print("Backups (oldest first, newest at the bottom):")
    for i, f in enumerate(files, 1):
        print(f"{i:3}. {time.strftime('%d %b %H:%M', time.localtime(f.stat().st_mtime))}  {f.name}")
    bfile = files[choose("\nnumber of the backup to restore from (Enter = cancel): ", len(files)) - 1]

    live = sqlite3.connect(DB, timeout=30, isolation_level=None); live.row_factory = sqlite3.Row
    bak = sqlite3.connect(f"file:{bfile}?mode=ro", uri=True); bak.row_factory = sqlite3.Row
    people = bak.execute("SELECT * FROM participants ORDER BY id").fetchall()
    print(f"\nPeople in {bfile.name}:\n   id  nickname         lang  in the backup  |  now in the live database")
    for p in people:
        print(f"{p['id']:5}  {pad(p['nickname'], 16)} {p['lang']:<4}  {summary(bak, p['id'])}  |  {summary(live, p['id'])}")
    a = input("\nid of the person to restore (Enter = cancel): ").strip()
    src = next((p for p in people if str(p["id"]) == a), None)
    if not src:
        sys.exit("no such id in this backup; nothing changed")

    # who is this person in the live database now? (same nickname, or the same id still theirs)
    by_nick = live.execute("SELECT * FROM participants WHERE nickname=? COLLATE NOCASE", (src["nickname"],)).fetchone()
    by_id = live.execute("SELECT * FROM participants WHERE id=?", (src["id"],)).fetchone()
    if by_id and by_id["nickname"].lower() != src["nickname"].lower():
        new_id = live.execute("SELECT MAX(id) FROM participants").fetchone()[0] + 1
        note = f"(their old id {src['id']} now belongs to someone else, so they get id {new_id})"
    else:
        new_id, note = src["id"], ""
    old = by_nick
    print(f"\nRestore id {src['id']} from {bfile.name} {note}")
    print(f"  backup: {summary(bak, src['id'])}")
    print(f"  now:    {summary(live, old['id']) if old else 'not present (deleted)'}")
    if old:
        print("  Their current data will be REPLACED by the backup version.")
    if input("  type yes to confirm: ").strip().lower() != "yes":
        sys.exit("cancelled; nothing changed")

    BACKUPS.mkdir(exist_ok=True)
    safety = BACKUPS / f"study-before-restore-id{src['id']}-{time.strftime('%Y-%m-%d_%H%M%S')}.db"
    dst = sqlite3.connect(safety); live.backup(dst); dst.close()
    print(f"  backup of the current state: data/backups/{safety.name}")

    live.execute("BEGIN IMMEDIATE")
    try:
        if old:
            for t in CHILD:
                live.execute(f"DELETE FROM {t} WHERE pid=?", (old["id"],))
            live.execute("DELETE FROM participants WHERE id=?", (old["id"],))
        cols = src.keys()
        vals = [new_id if c == "id" else src[c] for c in cols]
        live.execute(f"INSERT INTO participants ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        n = 0
        live_cols = {t: {r[1] for r in live.execute(f"PRAGMA table_info({t})")} for t in CHILD}
        for t in CHILD:
            for r in bak.execute(f"SELECT * FROM {t} WHERE pid=? ORDER BY id", (src["id"],)):
                cs = [c for c in r.keys() if c != "id" and c in live_cols[t]]   # new row ids: no clashes with rows added since
                live.execute(f"INSERT INTO {t} ({','.join(cs)}) VALUES ({','.join('?' * len(cs))})",
                             [new_id if c == "pid" else r[c] for c in cs])
                n += 1
        live.execute("INSERT INTO events (pid,stage,step,type,ts,detail) VALUES (?,?,?,?,?,?)",
                     (new_id, None, "", "admin_restore", time.time(), f'{{"from": "{bfile.name}"}}'))
        live.execute("COMMIT")
    except BaseException:
        live.execute("ROLLBACK")
        raise
    print(f"  done: restored with {n} rows. Now: {summary(live, new_id)}")
    print("  They can keep using their nickname and PIN; their browser stays logged in.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\ncancelled")
