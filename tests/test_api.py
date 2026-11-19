"""API tests for the study rules, with the REAL timings (240 s reading, 48 h / 7 d gaps).

A fake clock replaces time.time() inside app.main, so a whole 3-session run takes well under a second.
Run: pip install -r requirements.txt pytest httpx && pytest -q
"""
import copy
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main

KEY = "test-admin-key-123456"
ADMIN = {"X-Admin-Key": KEY}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += sec


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = Clock(1_800_000_000.0)
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "study.db")
    monkeypatch.setattr(main, "time", SimpleNamespace(time=clock, strftime=time.strftime, gmtime=time.gmtime))
    monkeypatch.setattr(main, "ADMIN_KEY", KEY)
    monkeypatch.setattr(main, "ADMIN_ENABLED", True)
    for name, val in dict(READ_SEC=240, HIGHLIGHT_SEC=240, TEST_MAX_SEC=180, BLOCK_SEC=240, FEEDBACK_MIN_SEC=60,
                          DAY2_GAP=48 * 3600, DAY9_GAP=7 * 24 * 3600).items():
        monkeypatch.setattr(main, name, val)
    main._fails.clear()
    with TestClient(main.app) as client:
        yield client, clock


def join(client, nick="alice", pin="1234", lang="en", consent=True):
    return client.post("/api/join", json={"nickname": nick, "pin": pin, "lang": lang, "consent": consent})


def hdr(token):
    return {"X-Token": token}


def state(client, token, skip_intro=True):
    """Current state. Each session starts with an untimed instructions step; by default it's
    confirmed here, so tests start at the first real step."""
    r = client.get("/api/state", headers=hdr(token))
    assert r.status_code == 200, r.text
    st = r.json()
    if skip_intro and st.get("step", {}).get("type") == "intro":
        assert complete(client, token, "intro").status_code == 200
        return state(client, token)
    return st


def complete(client, token, step_id, data=None):
    return client.post("/api/complete", headers=hdr(token), json={"step_id": step_id, "data": data or {}})


def practice_answers(text_id, right=True, conf=5):
    qs = main.CONTENT["texts"][text_id]["practice"]
    return {"answers": [{"qid": q["id"], "answer": q["accept"]["en"][0] if right else "no idea",
                         "confidence": conf, "rt_ms": 1000} for q in qs]}


def final_answers(text_id, n_right, conf=3):
    qs = main.CONTENT["texts"][text_id]["final"]
    return {"answers": [{"qid": q["id"], "answer": q["accept"]["en"][0] if i < n_right else "dunno", "rt_ms": 900,
                         "first_ms": 200, "edits": 4, "confidence": conf}
                        for i, q in enumerate(qs)]}


SURVEY_OK = {"belief_after": "test", "harder": "test", "prior_T1": "2", "prior_T2": "1",
             "reviewed": "no", "notes": "no", "habit": "reread"}


def run_session1(client, clock, token):
    st = state(client, token)
    assert st["stage"] == "day0" and st["step"]["id"] == "read1"
    clock.advance(240)
    assert complete(client, token, "read1").status_code == 200
    clock.advance(240)
    assert complete(client, token, "read2").status_code == 200
    st = state(client, token)
    clock.advance(100)
    assert complete(client, token, "test", practice_answers(st["step"]["text"], right=False, conf=5)).status_code == 200
    clock.advance(140)
    assert complete(client, token, "feedback").status_code == 200


# ------------------------------------------------------------------ sign-up / login
def test_join_validation(env):
    client, _ = env
    assert join(client, pin="12").status_code == 400
    assert join(client, nick="a").status_code == 400
    assert join(client, consent=False).status_code == 400
    assert join(client, lang="de").status_code == 400
    r = join(client)
    assert r.status_code == 200 and r.json()["resumed"] is False
    assert join(client, pin="9999").status_code == 409            # taken nickname
    assert join(client, nick="ALICE").json()["resumed"] is True   # same nickname+PIN resumes, case-insensitive
    assert client.post("/api/login", json={"nickname": "alice", "pin": "0000"}).status_code == 401
    assert client.post("/api/login", json={"nickname": "Alice", "pin": "1234"}).status_code == 200
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", headers=hdr("nope")).status_code == 401


def test_pin_bruteforce_lockout(env):
    client, clock = env
    join(client)
    for pin in range(main.LOGIN_MAX_FAILS):
        assert client.post("/api/login", json={"nickname": "alice", "pin": f"{pin:04d}"}).status_code == 401
    # even the right PIN is refused while locked; guessing through /api/join is blocked as well
    assert client.post("/api/login", json={"nickname": "alice", "pin": "1234"}).status_code == 429
    assert join(client).status_code == 429
    clock.advance(main.LOGIN_LOCK_SEC + 1)
    assert client.post("/api/login", json={"nickname": "alice", "pin": "1234"}).status_code == 200


def test_groups_are_balanced(env):
    client, _ = env
    for i in range(6):
        assert join(client, nick=f"user{i}").status_code == 200
    con = sqlite3.connect(main.DB_PATH)
    groups = [g for (g,) in con.execute("SELECT grp FROM participants")]
    assert groups.count("A") == 3 and groups.count("B") == 3


# ------------------------------------------------------------------ session rules
def test_timers_and_step_order_are_enforced(env):
    client, clock = env
    tok = join(client).json()["token"]
    st = state(client, tok)
    assert st["step"]["remaining"] == 240
    clock.advance(100)
    assert complete(client, tok, "read1").json()["detail"] == "time not up yet"
    assert complete(client, tok, "read2").json()["detail"] == "step mismatch, reload"
    assert state(client, tok)["step"]["remaining"] == 140   # a reload resumes the server timer
    clock.advance(140)
    assert complete(client, tok, "read1").status_code == 200


def test_text_never_sent_during_tests(env):
    client, clock = env
    tok = join(client).json()["token"]
    for sid in ("read1", "read2"):
        assert "sentences" in state(client, tok)["step"]
        clock.advance(240)
        complete(client, tok, sid)
    st = state(client, tok)
    assert st["step"]["type"] == "test"
    assert "sentences" not in st["step"] and "answer" not in json.dumps(st["step"]).lower()


def test_double_submit_stores_answers_once(env):
    client, clock = env
    tok = join(client).json()["token"]
    state(client, tok)                     # the browser always loads the state before submitting
    for sid in ("read1", "read2"):
        clock.advance(240)
        complete(client, tok, sid)
    text = state(client, tok)["step"]["text"]
    data = practice_answers(text)
    assert complete(client, tok, "test", data).status_code == 200
    assert complete(client, tok, "test", data).status_code == 409
    n = sqlite3.connect(main.DB_PATH).execute("SELECT COUNT(*) FROM answers").fetchone()[0]
    assert n == len(main.CONTENT["texts"][text]["practice"])


def test_feedback_fills_the_rest_of_the_block(env):
    client, clock = env
    tok = join(client).json()["token"]
    state(client, tok)                     # the browser always loads the state before submitting
    for sid in ("read1", "read2"):
        clock.advance(240)
        complete(client, tok, sid)
    text = state(client, tok)["step"]["text"]
    clock.advance(100)
    complete(client, tok, "test", practice_answers(text))
    fb = state(client, tok)["step"]
    assert fb["type"] == "feedback" and fb["dur"] == 140      # 240 s block - 100 s used
    assert all(item["correct"] for item in fb["items"])
    clock.advance(130)
    assert complete(client, tok, "feedback").status_code == 409
    clock.advance(10)
    assert complete(client, tok, "feedback").status_code == 200


def test_full_three_session_run(env):
    client, clock = env
    tok = join(client).json()["token"]
    grp = sqlite3.connect(main.DB_PATH).execute("SELECT grp FROM participants").fetchone()[0]
    routine = main.text_for(grp, "test")
    usual = main.text_for(grp, "highlight")

    run_session1(client, clock, tok)
    st = state(client, tok)
    assert st["stage"] == "wait" and st["next"] == "day2"
    assert st["unlock_at"] == pytest.approx(clock.t + 48 * 3600)
    assert complete(client, tok, "hl1").status_code == 409          # Session 2 still locked

    clock.advance(48 * 3600)
    st = state(client, tok)
    assert st["stage"] == "day2" and st["step"]["id"] == "hl1" and st["step"]["text"] == usual
    clock.advance(240)
    assert complete(client, tok, "hl1", {"selected": [0, 2, 2, 5]}).status_code == 200
    st = state(client, tok)
    assert st["step"]["selected"] == [0, 2, 5]                       # round 2 starts from round-1 highlights
    clock.advance(240)
    assert complete(client, tok, "hl2", {"selected": [0, 5]}).status_code == 200
    st = state(client, tok)
    assert st["step"]["text"] == routine
    clock.advance(180)
    complete(client, tok, "test", practice_answers(routine))
    clock.advance(60)                                                # test used the full 180 s -> 60 s minimum
    assert complete(client, tok, "feedback").status_code == 200
    assert complete(client, tok, "predict", {"pred": {"T1": 99, "T2": 1}, "belief": "highlight"}).status_code == 200

    st = state(client, tok)
    assert st["stage"] == "wait" and st["next"] == "day9"
    clock.advance(7 * 24 * 3600)
    st = state(client, tok)
    assert st["stage"] == "day9" and st["step"]["id"] == "final1"
    assert "sentences" not in st["step"]
    n_final = {t: len(main.CONTENT["texts"][t]["final"]) for t in main.TEXT_IDS}
    right = {routine: n_final[routine], usual: 1}
    complete(client, tok, "final1", final_answers("T1", right["T1"], conf=5 if routine == "T1" else 2))
    complete(client, tok, "final2", final_answers("T2", right["T2"], conf=5 if routine == "T2" else 2))
    assert state(client, tok)["step"]["type"] == "survey"
    bad = complete(client, tok, "survey", {"answers": {**SURVEY_OK, "habit": "sleep"}})
    assert bad.status_code == 400 and "habit" in bad.json()["detail"]
    assert complete(client, tok, "survey", {"answers": SURVEY_OK}).status_code == 200

    st = state(client, tok)
    assert st["stage"] == "done"
    assert st["scores"]["test"] == {"correct": n_final[routine], "n": n_final[routine]}
    assert st["scores"]["highlight"] == {"correct": 1, "n": n_final[usual]}
    assert st["predicted"]["test" if routine == "T1" else "highlight"] == n_final["T1"]  # 99 clamped to max

    ov = client.get("/api/admin/overview", headers=ADMIN).json()["participants"][0]
    # gaps are end-to-end: Session 1 end -> Session 2 end includes Session 2's ~14 min
    assert ov["status"] == "done" and ov["gap_day0_day2_h"] == 48.2 and ov["gap_day2_day9_h"] == 168.0
    assert ov["belief"] == "highlight" and ov["s1_min"] == 12.0 and ov["s3_min"] == 0.0

    # admin re-marks one wrong final answer as correct
    wrong = [a for a in client.get("/api/admin/answers", headers=ADMIN).json()["answers"] if not a["effective"]]
    assert client.post("/api/admin/grade", headers=ADMIN, json={"id": wrong[0]["id"], "override": 1}).status_code == 200
    s = state(client, tok)["scores"]
    assert s["test"]["correct"] + s["highlight"]["correct"] == n_final[routine] + 2

    for what in ("participants", "answers", "highlights", "events"):
        r = client.get(f"/api/admin/export/{what}.csv", headers=ADMIN)
        assert r.status_code == 200 and r.text.startswith("﻿") and len(r.text.splitlines()) > 1
    answers_csv = client.get("/api/admin/export/answers.csv", headers=ADMIN).text
    assert "correct" in answers_csv.splitlines()[0]


# ------------------------------------------------------------------ marking
@pytest.mark.parametrize("answer,key,ok", [
    ("8", "8", True), ("8 legs", "8", True), ("0.5 mm", "05", True), ("0,5", "05", True), ("5", "05", False), ("eight legs", "eight", True), ("18", "8", False),
    ("2008", "8", False), ("in 2007.", "2007", True), ("20071", "2007", False), ("８", "8", True),
    ("암", "=암", True), ("암컷", "=암", False), (" 암 ", "=암", True), ("이산화탄소", "=산", False),
    ("Бочонок", "бочон", True), ("бочонком", "бочон", True), ("Ёлка", "елк", True), ("툰 상태", "툰", True), ("", "8", False),
])
def test_keyword_matching(answer, key, ok):
    assert main.key_matches(answer, key) is ok


def test_auto_score_uses_language_keys():
    item = {"accept": {"en": ["eight"], "ru": ["восем"]}}
    assert main.auto_score("Восемь", item, "ru") == 1
    assert main.auto_score("eight", item, "ru") == 0
    assert main.auto_score("eight", item, "ko") == 1          # falls back to English keys
    assert main.auto_score("   ", item, "en") == 0


# ------------------------------------------------------------------ admin + misc
def test_admin_requires_key(env):
    client, _ = env
    assert client.get("/api/admin/overview").status_code == 403
    assert client.get("/api/admin/overview", headers={"X-Admin-Key": "wrong"}).status_code == 403
    assert client.get("/api/admin/export/answers.csv").status_code == 403
    assert client.get("/api/admin/overview", headers=ADMIN).status_code == 200


def test_admin_disabled_with_placeholder_key(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(main, "ADMIN_ENABLED", False)
    assert client.get("/api/admin/overview", headers=ADMIN).status_code == 503


def test_backup_download_is_a_sqlite_db(env):
    client, _ = env
    join(client)
    r = client.get("/api/admin/backup.db", headers=ADMIN)
    assert r.status_code == 200 and r.content.startswith(b"SQLite format 3")


def test_sample_content_is_valid():
    raw = json.loads(main.CONTENT_FILE.read_text(encoding="utf-8"))
    assert main.validate_content(raw) == []


def test_content_validation_catches_mistakes():
    raw = json.loads(main.CONTENT_FILE.read_text(encoding="utf-8"))
    bad = copy.deepcopy(raw)
    bad["texts"]["T1"]["final"][0]["fact"] = "nonexistent"
    bad["texts"]["T1"]["final"][1]["accept"]["ru"] = []
    bad["texts"]["T2"]["practice"][0]["accept"]["en"] = ["a"]
    del bad["texts"]["T2"]["body"]["ko"]
    problems = "\n".join(main.validate_content(bad))
    assert "no matching practice question" in problems
    assert "no accept keywords in 'ru'" in problems
    assert "single letter" in problems
    assert "language 'ko' has a body for only one text" in problems


def test_reminder_ics(env):
    client, clock = env
    r = client.get(f"/reminder.ics?at={int(clock.t) + 3600}&n=2&lang=ko")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert "BEGIN:VEVENT" in r.text and "세션 2" in r.text and "\r\n" in r.text
    assert client.get(f"/reminder.ics?at={int(clock.t) + 400 * 86400}&n=2").status_code == 400


def test_events(env):
    client, _ = env
    tok = join(client).json()["token"]
    assert client.post("/api/event", headers=hdr(tok), json={"type": "hidden", "step_id": "read1"}).status_code == 200
    assert client.post("/api/event", headers=hdr(tok), json={"type": "<script>"}).status_code == 400


def test_security_headers(env):
    client, _ = env
    r = client.get("/")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/docs").status_code == 404


# ------------------------------------------------------------------ data collection for the analyses
def run_all(client, clock, nick="bob", hl_selected=(0, 1), routine_right=True):
    """Full run; returns (token, group, routine text, usual text)."""
    tok = join(client, nick=nick).json()["token"]
    grp = sqlite3.connect(main.DB_PATH).execute("SELECT grp FROM participants WHERE nickname=?", (nick,)).fetchone()[0]
    routine, usual = main.text_for(grp, "test"), main.text_for(grp, "highlight")
    st = state(client, tok)
    clock.advance(240); complete(client, tok, "read1", {"scroll_max": 0.8})
    clock.advance(240); complete(client, tok, "read2", {"scroll_max": 1.0})
    state(client, tok)
    clock.advance(180)   # used the whole self-test time: auto-submitted
    complete(client, tok, "test", {**practice_answers(routine, right=False, conf=5), "auto": True})
    clock.advance(60); complete(client, tok, "feedback")
    clock.advance(48 * 3600); state(client, tok)
    client.post("/api/event", headers=hdr(tok), json={"type": "hidden", "step_id": "hl1"})
    clock.advance(30)
    client.post("/api/event", headers=hdr(tok), json={"type": "visible", "step_id": "hl1"})
    clock.advance(210)
    log = [{"i": i, "a": "on", "t": 1000 * (k + 1), "m": "pen" if k else "touch"} for k, i in enumerate(hl_selected)] \
        + [{"i": 99, "a": "on", "t": 1}, {"i": 0, "a": "on", "t": 5, "m": "laser"}]
    complete(client, tok, "hl1", {"selected": list(hl_selected), "log": log})
    clock.advance(240); complete(client, tok, "hl2", {"selected": list(hl_selected), "log": []})
    state(client, tok)
    clock.advance(60); complete(client, tok, "test", practice_answers(routine, right=routine_right, conf=3))
    clock.advance(180); complete(client, tok, "feedback")
    complete(client, tok, "predict", {"pred": {routine: 1, usual: 3}, "belief": "highlight"})
    clock.advance(7 * 24 * 3600); state(client, tok)
    n = len(main.CONTENT["texts"][usual]["final"])
    for sid, tid in (("final1", "T1"), ("final2", "T2")):
        complete(client, tok, sid, final_answers(tid, n if tid == routine else 2, conf=4))
    complete(client, tok, "survey", {"answers": SURVEY_OK})
    assert state(client, tok)["stage"] == "done"
    return tok, grp, routine, usual


def csv_rows(client, what):
    import csv as _csv
    import io as _io
    r = client.get(f"/api/admin/export/{what}.csv", headers=ADMIN)
    assert r.status_code == 200
    return list(_csv.DictReader(_io.StringIO(r.text.lstrip("\ufeff"))))


def test_items_link_facts_to_highlights_and_practice(env):
    client, clock = env
    _, grp, routine, usual = run_all(client, clock)
    items = csv_rows(client, "items")
    n_final = sum(len(main.CONTENT["texts"][t]["final"]) for t in main.TEXT_IDS)
    assert len(items) == n_final
    lang_src = {q["id"]: q["_src"]["en"] for q in main.CONTENT["texts"][usual]["final"]}
    for r in items:
        assert r["final_confidence"] == "4" and r["final_edits"] == "4" and r["final_first_ms"] == "200"
        if r["condition"] == "highlight":
            expect = int(any(i in (0, 1) for i in lang_src[r["qid"]]))
            assert r["highlighted_r1"] == r["highlighted_r2"] == str(expect)
            assert r["s1_correct"] == "" and r["predicted_for_text"] == "3"
        else:
            assert r["highlighted_r2"] == ""
            assert r["s1_correct"] == "0" and r["s1_confidence"] == "5" and r["s1_error_type"] == "high-conf error"
            assert r["s2_correct"] == "1" and r["predicted_for_text"] == "1"
        assert r["prior_knowledge"] == ("2" if r["text_id"] == "T1" else "1")
    # at least one usual-text fact highlighted and one not, so the comparison is possible with the sample
    flags = {r["highlighted_r2"] for r in items if r["condition"] == "highlight"}
    assert flags == {"0", "1"}


def test_behaviour_logs(env):
    client, clock = env
    tok, grp, routine, usual = run_all(client, clock)
    client.post("/api/event", headers=hdr(tok), json={"type": "device", "detail": {"kind": "tablet", "sw": 820, "sh": 1180, "tz": "Asia/Seoul", "ua": "x"}})
    log = csv_rows(client, "highlight_log")
    # bad index dropped; unknown method stored as empty
    assert [(r["round"], r["sentence"], r["action"], r["method"]) for r in log] == \
        [("1", "0", "on", "touch"), ("1", "1", "on", "pen"), ("1", "0", "on", "")]
    steps = {(r["stage"], r["step_id"]): r for r in csv_rows(client, "steps")}
    assert len(steps) == 5 + 6 + 4
    assert json.loads(steps[("day0", "read1")]["extra"]) == {"scroll_max": 0.8}
    assert json.loads(steps[("day0", "test")]["extra"])["auto_submit"] is True
    assert steps[("day2", "hl1")]["hidden_n"] == "1" and float(steps[("day2", "hl1")]["away_sec"]) == 30.0
    assert float(steps[("day0", "read1")]["elapsed_sec"]) == 240.0
    survey = {r["item"]: r["value"] for r in csv_rows(client, "survey")}
    assert survey == SURVEY_OK
    preds = csv_rows(client, "predictions")
    assert {r["condition"]: r["predicted"] for r in preds} == {"test": "1", "highlight": "3"}
    part = csv_rows(client, "participants")[0]
    assert part["away_sec"] == "30.0" and part["tests_auto_submitted"] == "1" and part["device"] == "tablet"
    assert part["survey_habit"] == "reread" and part["hl_sentences_r2"] == "2" and part["final_conf_usual"] == "4.0"
    ev = csv_rows(client, "events")
    assert any(r["type"] == "device" and "Asia/Seoul" in r["detail"] for r in ev)


def test_insights(env):
    client, clock = env
    run_all(client, clock, nick="p1", hl_selected=(0, 1))
    run_all(client, clock, nick="p2", hl_selected=(0, 1, 2, 3, 4, 5, 6, 7), routine_right=False)
    d = client.get("/api/admin/insights", headers=ADMIN).json()
    n = len(main.CONTENT["texts"]["T1"]["final"])          # questions per text (both texts have the same number)
    assert all(len(main.CONTENT["texts"][t][k]) == n for t in main.TEXT_IDS for k in ("practice", "final"))
    assert d["participants"] == 2 and d["items"] == 2 * 2 * n
    # run_all: routine text all right, usual text 2 right
    assert d["by_condition"]["test"]["accuracy"] == 1.0 and d["by_condition"]["highlight"]["accuracy"] == round(2 / n, 3)
    assert d["by_condition"]["test"]["mean_confidence"] == 4.0
    hl = d["highlighting"]
    assert hl["highlighted"]["n"] + hl["not_highlighted"]["n"] == 2 * n
    assert d["s1_errors"]["high-conf error"]["n"] == 2 * n and d["s1_errors"]["high-conf error"]["accuracy"] == 1.0
    assert d["calibration"]["test"]["4"]["n"] == 2 * n
    assert d["prediction_vs_actual"]["test"] == {"predicted_pct": round(100 / n, 3), "actual_pct": 100.0}
    assert d["beliefs"] == {"before_test": {"highlight": 2}, "after_test": {"test": 2}}


def test_practice_answers_can_be_regraded(env):
    client, clock = env
    run_all(client, clock)
    answers = client.get("/api/admin/answers", headers=ADMIN).json()["answers"]
    assert {a["kind"] for a in answers} == {"practice", "final"}
    p = next(a for a in answers if a["kind"] == "practice" and a["stage"] == "day0")
    assert p["question"] and p["key"]
    client.post("/api/admin/grade", headers=ADMIN, json={"id": p["id"], "override": 1})
    items = csv_rows(client, "items")
    assert any(r["s1_correct"] == "1" and r["s1_error_type"] == "correct" for r in items)


def test_old_database_is_migrated(tmp_path, monkeypatch):
    db = tmp_path / "study.db"
    con = sqlite3.connect(db)
    con.executescript("""
      CREATE TABLE participants (id INTEGER PRIMARY KEY, nickname TEXT NOT NULL UNIQUE COLLATE NOCASE, pin_hash TEXT NOT NULL,
        token TEXT NOT NULL UNIQUE, lang TEXT NOT NULL, grp TEXT NOT NULL, created_at REAL NOT NULL,
        day0_done REAL, day2_done REAL, day9_done REAL, stage TEXT, step INTEGER DEFAULT 0, step_started REAL,
        test_used REAL, belief TEXT, excluded INTEGER DEFAULT 0);
      CREATE TABLE answers (id INTEGER PRIMARY KEY, pid INTEGER NOT NULL, stage TEXT NOT NULL, kind TEXT NOT NULL,
        text_id TEXT NOT NULL, condition TEXT NOT NULL, qid TEXT NOT NULL, fact TEXT, answer TEXT, confidence INTEGER,
        auto_correct INTEGER NOT NULL, override INTEGER, rt_ms INTEGER, ts REAL NOT NULL);
      CREATE TABLE events (id INTEGER PRIMARY KEY, pid INTEGER NOT NULL, stage TEXT, step TEXT, type TEXT NOT NULL, ts REAL NOT NULL);
      INSERT INTO participants (nickname,pin_hash,token,lang,grp,created_at) VALUES ('old','h','tok','en','A',1);
    """)
    con.commit(); con.close()
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "DB_PATH", db)
    with TestClient(main.app):
        pass
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(answers)")}
    assert {"first_ms", "edits"} <= cols
    assert "detail" in {r[1] for r in con.execute("PRAGMA table_info(events)")}
    assert con.execute("SELECT nickname FROM participants").fetchone()[0] == "old"
    assert con.execute("SELECT COUNT(*) FROM steps").fetchone()[0] == 0


def test_each_session_starts_with_instructions(env):
    client, clock = env
    tok = join(client).json()["token"]
    st = state(client, tok, skip_intro=False)
    assert st["step"]["type"] == "intro" and st["step"]["variant"] == "s1" and st["step_index"] == 0
    assert st["step"]["timings"] == {"read": 240, "highlight": 240, "test": 180, "block": 240}
    assert "remaining" not in st["step"]                         # untimed
    clock.advance(75)
    assert complete(client, tok, "intro").status_code == 200
    st = state(client, tok, skip_intro=False)
    assert st["step"]["id"] == "read1" and st["step"]["remaining"] == 240   # reading timer starts after the intro
    for sid in ("read1", "read2"):
        clock.advance(240); complete(client, tok, sid)
    text = state(client, tok)["step"]["text"]
    clock.advance(180); complete(client, tok, "test", practice_answers(text))
    clock.advance(60); complete(client, tok, "feedback")
    clock.advance(48 * 3600)
    assert state(client, tok, skip_intro=False)["step"]["variant"] == "s2"
    steps = csv_rows(client, "steps")
    assert steps[0]["step_id"] == "intro" and float(steps[0]["elapsed_sec"]) == 75.0


def test_real_content_is_comparable_across_languages():
    """Every language has the same paragraphs/sentences and each fact sits in the same sentence,
    so highlights and results can be compared across languages; every key accepts its own answer."""
    for tid in main.TEXT_IDS:
        t = main.CONTENT["texts"][tid]
        shape = {l: [s["p"] for s in t["sentences"][l]] for l in main.LANGS}
        assert len({tuple(v) for v in shape.values()}) == 1, (tid, shape)
        for kind in ("practice", "final"):
            for q in t[kind]:
                assert len({tuple(q["_src"][l]) for l in main.LANGS}) == 1, (tid, q["id"])
                assert all(len(q["_src"][l]) == 1 for l in main.LANGS), (tid, q["id"])
                for l in main.LANGS:
                    assert main.auto_score(q["answer"][l], q, l) == 1, (tid, q["id"], l)


def test_test_mode_shortens_timers_and_gaps(env):
    client, clock = env
    tok = join(client, nick="pilot").json()["token"]
    # a real participant who already finished Session 1 is waiting 48 h ...
    run_session1(client, clock, tok)
    assert state(client, tok)["stage"] == "wait"
    pid = sqlite3.connect(main.DB_PATH).execute("SELECT id FROM participants WHERE nickname='pilot'").fetchone()[0]
    # ... switching on test mode opens the next session 30 s after the last one
    assert client.post("/api/admin/tester", headers=ADMIN, json={"id": pid, "tester": True}).status_code == 200
    clock.advance(main.TEST_MODE_GAP_SEC)
    st = state(client, tok, skip_intro=False)
    assert st["stage"] == "day2" and st["step"]["timings"]["highlight"] == main.TEST_MODE_SEC
    # the self-test keeps its real limit in test mode (it can be submitted early anyway)
    assert st["step"]["timings"]["test"] == main.TEST_MAX_SEC
    complete(client, tok, "intro")
    st = state(client, tok)
    assert st["step"]["id"] == "hl1" and st["step"]["remaining"] == main.TEST_MODE_SEC
    clock.advance(main.TEST_MODE_SEC)
    assert complete(client, tok, "hl1", {"selected": [0]}).status_code == 200
    ov = client.get("/api/admin/overview", headers=ADMIN).json()["participants"][0]
    assert ov["tester"] is True and ov["excluded"] is True
    # testers don't count when balancing groups
    groups = []
    for i in range(4):
        join(client, nick=f"real{i}")
    con = sqlite3.connect(main.DB_PATH)
    groups = [g for (g,) in con.execute("SELECT grp FROM participants WHERE tester=0")]
    assert groups.count("A") == groups.count("B") == 2
    # switching it off restores the real timings
    client.post("/api/admin/tester", headers=ADMIN, json={"id": pid, "tester": False})
    clock.advance(1)
    assert state(client, tok)["step"]["dur"] == main.HIGHLIGHT_SEC   # same session continues, real timer


def test_word_level_highlights_link_to_answer_words(env):
    client, clock = env
    tok = join(client, nick="wordy").json()["token"]
    grp = sqlite3.connect(main.DB_PATH).execute("SELECT grp FROM participants WHERE nickname='wordy'").fetchone()[0]
    routine, usual = main.text_for(grp, "test"), main.text_for(grp, "highlight")
    t = main.CONTENT["texts"][usual]
    words = t["words"]["en"]
    q_a, q_b = t["final"][0], t["final"][3]
    aw = q_a["_words"]["en"]                       # answer words of fact A: highlighted exactly
    sent_b = q_b["_src"]["en"][0]                  # fact B: a non-answer word in its sentence only
    other_b = next(k for k, w in enumerate(words) if w["s"] == sent_b and k not in q_b["_words"]["en"])
    chosen = sorted(set(aw) | {other_b})
    run_session1(client, clock, tok)
    clock.advance(48 * 3600); state(client, tok)
    clock.advance(240)
    log = [{"a": "on", "f": aw[0], "to": aw[-1], "t": 1200, "m": "pen"}, {"a": "on", "f": other_b, "to": other_b, "t": 3000, "m": "touch"}]
    assert complete(client, tok, "hl1", {"words": chosen, "log": log}).status_code == 200
    st = state(client, tok)
    assert st["step"]["selected_words"] == chosen                         # round 2 starts from round-1 words
    assert st["step"]["selected"] == sorted({words[k]["s"] for k in chosen})
    clock.advance(240); complete(client, tok, "hl2", {"words": chosen, "log": []})
    state(client, tok)
    clock.advance(100); complete(client, tok, "test", practice_answers(routine))
    clock.advance(140); complete(client, tok, "feedback")
    complete(client, tok, "predict", {"pred": {"T1": 2, "T2": 2}, "belief": "same"})
    clock.advance(7 * 24 * 3600); state(client, tok)
    complete(client, tok, "final1", final_answers("T1", 3)); complete(client, tok, "final2", final_answers("T2", 3))
    complete(client, tok, "survey", {"answers": SURVEY_OK})

    items = {r["qid"]: r for r in csv_rows(client, "items") if r["condition"] == "highlight"}
    a, b = items[q_a["id"]], items[q_b["id"]]
    assert a["highlighted_r2"] == "1" and a["sentence_hl_r2"] == "1"
    assert b["highlighted_r2"] == "0" and b["sentence_hl_r2"] == "1"        # its sentence touched, answer not
    assert 0 < float(b["sentence_share_r2"]) < 1
    untouched = [r for qid, r in items.items() if r["sentence_hl_r2"] == "0"]
    assert untouched and all(r["highlighted_r2"] == "0" for r in untouched)
    assert json.loads(a["answer_words"]) == aw

    part = csv_rows(client, "participants")[0]
    assert part["hl_words_r2"] == str(len(chosen)) and part["text_words"] == str(len(words))
    hlog = csv_rows(client, "highlight_log")
    assert [(r["action"], r["method"], r["word_from"], r["word_to"]) for r in hlog] == \
        [("on", "pen", str(aw[0]), str(aw[-1])), ("on", "touch", str(other_b), str(other_b))]
    hl = csv_rows(client, "highlights")
    assert json.loads(hl[0]["words"]) == chosen


def test_every_question_has_answer_words_in_every_language():
    for tid in main.TEXT_IDS:
        t = main.CONTENT["texts"][tid]
        for q in t["final"] + t["practice"]:
            for l in main.LANGS:
                aw = q["_words"][l]
                assert aw, (tid, q["id"], l)
                # answer words sit inside the fact's sentence
                assert all(t["words"][l][k]["s"] in q["_src"][l] for k in aw), (tid, q["id"], l)


def test_tokenize():
    assert main.tokenize("In 2004, the biologist", "en") == ["In", "2004,", "the", "biologist"]
    assert main.tokenize("2004年，希拉·帕特克用Apple", "zh") == ["2004", "年", "，", "希", "拉", "·", "帕", "特", "克", "用", "Apple"]



def test_feedback_says_when_the_self_test_ran_out_of_time(env):
    client, clock = env
    for nick, auto in (("slow", True), ("quick", False)):
        tok = join(client, nick=nick).json()["token"]
        state(client, tok)
        for sid in ("read1", "read2"):
            clock.advance(240); complete(client, tok, sid)
        text = state(client, tok)["step"]["text"]
        clock.advance(180 if auto else 60)
        complete(client, tok, "test", {**practice_answers(text), "auto": auto})
        assert state(client, tok)["step"]["auto_submitted"] is auto


def test_language_can_change_only_before_the_first_text(env):
    client, clock = env
    tok = join(client, nick="polyglot", lang="en").json()["token"]
    st = state(client, tok, skip_intro=False)
    assert st["step"]["type"] == "intro" and st["lang_locked"] is False
    assert client.post("/api/lang", headers=hdr(tok), json={"lang": "xx"}).status_code == 400
    assert client.post("/api/lang", headers=hdr(tok), json={"lang": "mn"}).status_code == 200
    st = state(client, tok, skip_intro=False)
    assert st["lang"] == "mn" and st["step"]["type"] == "intro"
    complete(client, tok, "intro")
    st = state(client, tok, skip_intro=False)
    assert st["lang_locked"] is True and st["step"]["title"] == main.CONTENT["texts"]["T1"]["title"]["mn"]
    assert client.post("/api/lang", headers=hdr(tok), json={"lang": "en"}).status_code == 409
    ev = [r for r in csv_rows(client, "events") if r["type"] == "lang_change"]
    assert len(ev) == 1 and json.loads(ev[0]["detail"]) == {"from": "en", "to": "mn"}


def test_admin_can_delete_a_participant_with_confirmation(env):
    client, clock = env
    run_all(client, clock, nick="910goat")
    keep = join(client, nick="keeper").json()["token"]
    con = sqlite3.connect(main.DB_PATH)
    pid = con.execute("SELECT id FROM participants WHERE nickname='910goat'").fetchone()[0]
    assert client.post("/api/admin/delete", json={"id": pid, "confirm": "910goat"}).status_code == 403
    assert client.post("/api/admin/delete", headers=ADMIN, json={"id": pid, "confirm": "910goa"}).status_code == 400
    r = client.post("/api/admin/delete", headers=ADMIN, json={"id": pid, "confirm": "910GOAT"})
    assert r.status_code == 200
    for table in ("participants",) + main.PARTICIPANT_TABLES:
        col = "id" if table == "participants" else "pid"
        assert con.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (pid,)).fetchone()[0] == 0, table
    assert con.execute("SELECT nickname FROM participants").fetchall() == [("keeper",)]
    backup = main.DATA_DIR / "backups" / r.json()["backup"]
    assert sqlite3.connect(backup).execute("SELECT COUNT(*) FROM participants WHERE nickname='910goat'").fetchone()[0] == 1
    assert client.get("/api/state", headers=hdr(keep)).status_code == 200
