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


def state(client, token):
    r = client.get("/api/state", headers=hdr(token))
    assert r.status_code == 200, r.text
    return r.json()


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
    log = [{"i": i, "a": "on", "t": 1000 * (k + 1)} for k, i in enumerate(hl_selected)] + [{"i": 99, "a": "on", "t": 1}]
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
    assert [(r["round"], r["sentence"], r["action"]) for r in log] == [("1", "0", "on"), ("1", "1", "on")]   # bad index dropped
    steps = {(r["stage"], r["step_id"]): r for r in csv_rows(client, "steps")}
    assert len(steps) == 4 + 5 + 3
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
    assert d["participants"] == 2 and d["items"] == 16
    assert d["by_condition"]["test"]["accuracy"] == 1.0 and d["by_condition"]["highlight"]["accuracy"] == 0.5
    assert d["by_condition"]["test"]["mean_confidence"] == 4.0
    hl = d["highlighting"]
    assert hl["highlighted"]["n"] + hl["not_highlighted"]["n"] == 8
    assert d["s1_errors"]["high-conf error"]["n"] == 8 and d["s1_errors"]["high-conf error"]["accuracy"] == 1.0
    assert d["calibration"]["test"]["4"]["n"] == 8
    assert d["prediction_vs_actual"]["test"] == {"predicted_pct": 25.0, "actual_pct": 100.0}
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
