"""Mark the borderline self-test answers (Appendix A of the interim report) in one go.

    cd /opt/memory-study && python3 deploy/regrade.py

Shows every answer with the suggested decision. You can flip any of them by typing its answer id,
then type "yes" to apply. Nothing is typed in Korean/Chinese: you only type numbers and "yes".
Works while the app runs. A full backup is written to data/backups/ first.

What it does to each row (the same as the buttons on the admin page, Check answers -> Practice):
  correct  sets the manual mark to "correct"
  wrong    leaves the answer as the automatic marking had it (wrong); nothing is written
  undo     removes an earlier manual mark, so the automatic marking counts again
A row is skipped (and reported) if its answer text is not what this list expects, or if somebody
already marked it by hand, so it never overwrites a decision made on the admin page.

Standard used (the same one the team used for its first 13 manual marks):
  typos and misspellings count; a first name alone counts (as "Дженифер?" was accepted);
  "female / leader female" counts for "the queen" (as "雌性" was accepted); shelled prey counts for
  "snails and crabs"; a wrong number, a wrong word or only half a term stays wrong.
"""
import json
import os
import sqlite3
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
DB = Path(os.environ.get("STUDY_DB") or ("/data/study.db" if Path("/data/study.db").exists() else HERE / "data" / "study.db"))

# (answer id, participant id, expected answer text, suggestion, reason)
# suggestion: "correct", "wrong" (leave as is) or "undo" (remove an earlier manual mark)
DECISIONS = [
    (71,  6,  "Stomapede",                     "correct", "Stomatopods: misspelling"),
    (211, 6,  "Stompedes",                     "correct", "Stomatopods: misspelling"),
    (217, 6,  "calvation",                     "correct", "Cavitation: misspelling"),
    (73,  6,  "sea shells",                    "correct", "snails and crabs: 'sea shells' (shelled prey)"),
    (213, 6,  "Sea shells",                    "correct", "snails and crabs: 'sea shells' (shelled prey)"),
    (133, 12, "панцири и раковины",            "correct", "snails and crabs: 'carapaces and shells'"),
    (203, 15, "ракушки и устрицы",             "correct", "snails and crabs: 'shells and oysters'"),
    (323, 12, "ракушки",                       "correct", "snails and crabs: 'shells'"),
    (283, 24, "성게 등 어패류",                 "correct", "snails and crabs: 'sea urchins and other shellfish'"),
    (293, 25, "먹이(고동, 게)의 껍질",          "correct", "snails and crabs: 'shells of the prey (sea snails, crabs)'"),
    (483, 25, "고동과 게",                      "correct", "snails and crabs: 'sea snails and crabs' (고동 = 고둥)"),
    (573, 49, "조개",                           "correct", "snails and crabs: 'clams' (shelled prey)"),
    (325, 12, "24 метра в секунду",            "correct", "about 23 m/s: '24 m/s'"),
    (75,  6,  "20 mps",                        "wrong",   "about 23 m/s: '20 m/s' is a different number"),
    (302, 26, "이빨 뒤로 입이 닫혀",            "correct", "lips: 'the mouth closes behind the teeth'"),
    (364, 36, "제니퍼",                         "correct", "Jennifer Jarvis: first name only"),
    (314, 11, "дженнифер",                     "correct", "Jennifer Jarvis: first name only"),
    (344, 14, "джениффер, фамилию не помню",   "correct", "Jennifer Jarvis: first name, 'forgot the surname'"),
    (164, 2,  "詹妮弗",                         "correct", "Jennifer Jarvis: first name only"),
    (514, 16, "제니퍼",                         "correct", "Jennifer Jarvis: first name only"),
    (584, 40, "제퍼슨자바스",                   "wrong",   "Jennifer Jarvis: 'Jefferson Javis' (wrong first name)"),
    (404, 40, "갠지즈자바스",                   "wrong",   "Jennifer Jarvis: 'Ganges Javis' (wrong first name)"),
    (509, 16, "당",                             "wrong",   "Fructose: only 'sugar'"),
    (83,  7,  "1 самка",                       "correct", "the queen: '1 female' (as '雌性' was accepted)"),
    (523, 47, "우두머리암것",                   "correct", "the queen: 'leader female'"),
    (306, 26, "진사생학",                       "wrong",   "Eusociality: garbled word"),
    (546, 26, "진사",                           "wrong",   "Eusociality: only the first two syllables"),
    (46,  2,  "群居",                           "wrong",   "Eusociality: 'group living' (only part of the idea)"),
    (480, 46, "손목자르개",                     "wrong",   "Thumb splitters: 'wrist cutter' (different word)"),
    (500, 31, "劈手虾",                         "correct", "Thumb splitters: 'hand-splitting shrimp', one character off 劈指虾"),
    # earlier manual marks that look like misclicks
    (239, 4,  "",                              "undo",    "Fructose: blank answer was marked correct"),
    (240, 4,  "",                              "undo",    "Acid: blank answer was marked correct"),
    (310, 26, "산화.",                          "undo",    "Acid: '산화' means oxidation, not acid; was marked correct"),
]
FLIP = {"correct": "wrong", "wrong": "correct", "undo": "keep"}   # "keep" = leave the earlier mark


def pad(text, width):
    """Cut/pad to a screen width; Korean/Chinese characters take two columns."""
    out, w = "", 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if w + cw > width:
            break
        out, w = out + ch, w + cw
    return out + " " * (width - w)


def norm(s):
    return " ".join((s or "").split())


def check(con, aid, pid, expected, action):
    """None if the row can be changed, else the reason it is skipped."""
    r = con.execute("SELECT pid, answer, override FROM answers WHERE id=?", (aid,)).fetchone()
    if not r:
        return "answer not in the database"
    if r["pid"] != pid or norm(r["answer"]) != norm(expected):
        return "answer text differs from the list (database changed?)"
    if action == "correct" and r["override"] is not None:
        return f"already marked by hand ({'correct' if r['override'] else 'wrong'})"
    if action == "undo" and r["override"] is None:
        return "no manual mark to undo"
    return None


def main():
    con = sqlite3.connect(DB, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    print(f"database: {DB}")
    choice = {aid: sug for aid, _, _, sug, _ in DECISIONS}
    while True:
        print("\n   id  decision  given answer           reason")
        for aid, pid, expected, _, why in DECISIONS:
            skip = check(con, aid, pid, expected, choice[aid]) if choice[aid] in ("correct", "undo") else None
            note = f"   [skipped: {skip}]" if skip else ""
            print(f"{aid:5}  {choice[aid]:<8}  {pad(expected or '(blank)', 22)} {why}{note}")
        a = input("\nids to flip (space separated), Enter = done: ").replace(",", " ").split()
        if not a:
            break
        for x in a:
            if x.isdigit() and int(x) in choice:
                choice[int(x)] = FLIP[choice[int(x)]] if choice[int(x)] != "keep" else "undo"
            else:
                print(f"  {x}: not an id in the list")

    todo = [(aid, pid, exp, choice[aid]) for aid, pid, exp, _, _ in DECISIONS
            if choice[aid] in ("correct", "undo") and not check(con, aid, pid, exp, choice[aid])]
    if not todo:
        return print("nothing to change")
    n_c = sum(1 for t in todo if t[3] == "correct")
    n_u = len(todo) - n_c
    print(f"\nWill mark {n_c} answers correct and remove {n_u} earlier manual marks.")
    if input("type yes to confirm: ").strip().lower() != "yes":
        return print("cancelled; nothing changed")

    backups = DB.parent / "backups"
    backups.mkdir(exist_ok=True)
    b = backups / f"study-before-regrade-{time.strftime('%Y-%m-%d_%H%M%S')}.db"
    dst = sqlite3.connect(b)
    con.backup(dst)
    dst.close()
    print(f"backup: data/backups/{b.name}")

    con.execute("BEGIN IMMEDIATE")
    try:
        for aid, pid, exp, action in todo:
            if check(con, aid, pid, exp, action):      # changed since the list was shown
                continue
            con.execute("UPDATE answers SET override=? WHERE id=?", (1 if action == "correct" else None, aid))
            con.execute("INSERT INTO events (pid,stage,step,type,ts,detail) VALUES (?,?,?,?,?,?)",
                        (pid, None, "", "admin_regrade", time.time(), json.dumps({"answer_id": aid, "action": action})))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    print("done. The admin page (Check answers) and the CSV exports show the new marks.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\ncancelled; nothing changed")
