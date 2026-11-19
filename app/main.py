"""Memory study platform: highlight + cram vs. spaced self-testing with confidence and feedback.

One FastAPI app + SQLite. Participants study two texts; the server decides which text gets which
method (counterbalanced groups A/B), unlocks Day 2 and Day 9, enforces phase timers and stores
everything for analysis. Admin pages live under /admin.
"""
import csv
import hashlib
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import time
import unicodedata
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

log = logging.getLogger("uvicorn.error")  # shows up in `docker compose logs`

BASE = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE.parent / "data"))
CONTENT_FILE = Path(os.environ.get("CONTENT_FILE", BASE.parent / "content" / "content.json"))
DB_PATH = DATA_DIR / "study.db"
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
# The admin API stays locked until ADMIN_KEY is set to something that isn't a placeholder.
WEAK_KEYS = {"", "change-me", "change-me-to-a-long-random-password"}
ADMIN_ENABLED = ADMIN_KEY not in WEAK_KEYS and len(ADMIN_KEY) >= 12


def env_int(name, default):
    return int(os.environ.get(name, default))


# Durations in seconds. Override with environment variables for piloting (see README).
READ_SEC = env_int("READ_SEC", 240)          # Day 0: first reading of each text
HIGHLIGHT_SEC = env_int("HIGHLIGHT_SEC", 240)  # Day 2: each highlight + reread round (2 rounds)
TEST_MAX_SEC = env_int("TEST_MAX_SEC", 180)  # self-test answering limit
BLOCK_SEC = env_int("BLOCK_SEC", 240)        # self-test + feedback block total (matches one reading round)
FEEDBACK_MIN_SEC = env_int("FEEDBACK_MIN_SEC", 60)
DAY2_GAP = env_int("DAY2_GAP_SEC", 48 * 3600)
DAY9_GAP = env_int("DAY9_GAP_SEC", 7 * 24 * 3600)
TOLERANCE = 3  # seconds of clock slack when checking that a timed phase really finished

TEXT_IDS = ("T1", "T2")
LANGS = ("en", "ru", "ko")

# Brute-force guard for 4-digit PINs: after LOGIN_MAX_FAILS wrong PINs for one nickname,
# that nickname is locked for LOGIN_LOCK_SEC. Kept in memory; a restart clears it.
LOGIN_MAX_FAILS = env_int("LOGIN_MAX_FAILS", 8)
LOGIN_LOCK_SEC = env_int("LOGIN_LOCK_SEC", 900)
_fails = defaultdict(list)


@asynccontextmanager
async def lifespan(_app):
    with db() as con:
        con.executescript(SCHEMA)
    problems = load_content()
    for msg in problems:
        log.warning("content.json: %s", msg)
    if not ADMIN_ENABLED:
        log.warning("ADMIN_KEY is missing or a placeholder (min 12 chars): the admin page is disabled.")
    yield


app = FastAPI(title="Memory study", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------- database
def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def db():
    """One write transaction per request. BEGIN IMMEDIATE serialises writers, so two quick
    submits of the same step can't both pass the step check and store answers twice."""
    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
  id INTEGER PRIMARY KEY,
  nickname TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pin_hash TEXT NOT NULL,
  token TEXT NOT NULL UNIQUE,
  lang TEXT NOT NULL,
  grp TEXT NOT NULL,
  created_at REAL NOT NULL,
  day0_done REAL, day2_done REAL, day9_done REAL,
  stage TEXT, step INTEGER DEFAULT 0, step_started REAL,
  test_used REAL, belief TEXT, excluded INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS answers (
  id INTEGER PRIMARY KEY,
  pid INTEGER NOT NULL REFERENCES participants(id),
  stage TEXT NOT NULL, kind TEXT NOT NULL, text_id TEXT NOT NULL, condition TEXT NOT NULL,
  qid TEXT NOT NULL, fact TEXT, answer TEXT, confidence INTEGER,
  auto_correct INTEGER NOT NULL, override INTEGER, rt_ms INTEGER, ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS highlights (
  id INTEGER PRIMARY KEY, pid INTEGER NOT NULL REFERENCES participants(id),
  round INTEGER NOT NULL, text_id TEXT NOT NULL, sentences TEXT NOT NULL, ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS predictions (
  id INTEGER PRIMARY KEY, pid INTEGER NOT NULL REFERENCES participants(id),
  text_id TEXT NOT NULL, condition TEXT NOT NULL, predicted INTEGER NOT NULL, max INTEGER NOT NULL, ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, pid INTEGER NOT NULL REFERENCES participants(id),
  stage TEXT, step TEXT, type TEXT NOT NULL, ts REAL NOT NULL
);
"""


# ---------------------------------------------------------------- content
CONTENT = {}
SENT_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")


def validate_content(raw):
    """Return a list of human-readable problems (empty = OK). Missing languages are only a
    problem if the language is half-done (one text has it, the other doesn't)."""
    out = []
    texts = raw.get("texts", {})
    for tid in TEXT_IDS:
        if tid not in texts:
            out.append(f"missing text {tid}")
    if out:
        return out
    langs = [l for l in LANGS if all(texts[t].get("body", {}).get(l, "").strip() for t in TEXT_IDS)]
    for l in LANGS:
        if l not in langs and any(texts[t].get("body", {}).get(l, "").strip() for t in TEXT_IDS):
            out.append(f"language '{l}' has a body for only one text, so it won't be offered")
    if not langs:
        out.append("no language has a body for both texts")
    for tid in TEXT_IDS:
        t = texts[tid]
        for l in langs:
            if not tr_strict(t.get("title"), l):
                out.append(f"{tid}: title missing in '{l}'")
        practice_facts = set()
        for kind in ("practice", "final"):
            items = t.get(kind) or []
            if not items:
                out.append(f"{tid}: no {kind} questions")
            ids = [q.get("id") for q in items]
            if len(set(ids)) != len(ids) or None in ids:
                out.append(f"{tid}/{kind}: question ids missing or duplicated")
            for q in items:
                where = f"{tid}/{kind}/{q.get('id')}"
                for l in langs:
                    for field in ("q", "answer"):
                        if not tr_strict(q.get(field), l):
                            out.append(f"{where}: '{field}' missing in '{l}'")
                    keys = (q.get("accept") or {}).get(l) or []
                    if not keys:
                        out.append(f"{where}: no accept keywords in '{l}'")
                    for k in keys:
                        nk = norm(k)
                        # A single Latin/Cyrillic letter matches almost any answer. (One Hangul
                        # syllable such as 툰 is a real word, so it's allowed.)
                        if len(nk) == 1 and not nk.isdigit() and not ("\uac00" <= nk <= "\ud7a3"):
                            out.append(f"{where}: keyword '{k}' ({l}) is a single letter and will match almost anything")
                if kind == "practice":
                    practice_facts.add(q.get("fact", q.get("id")))
                elif q.get("fact") not in practice_facts:
                    out.append(f"{where}: fact '{q.get('fact')}' has no matching practice question (hypercorrection analysis needs one)")
    return out


def tr_strict(obj, lang):
    if isinstance(obj, dict):
        return (obj.get(lang) or "").strip()
    return (obj or "").strip() if isinstance(obj, str) else ""


def load_content():
    """(Re)load content.json. Returns a list of validation warnings."""
    raw = json.loads(CONTENT_FILE.read_text(encoding="utf-8"))
    problems = validate_content(raw)
    texts = raw["texts"]
    for tid in TEXT_IDS:
        if tid not in texts:
            raise RuntimeError(f"content.json needs texts {TEXT_IDS}")
        t = texts[tid]
        t["sentences"] = {}
        for lang in LANGS:
            body = t.get("body", {}).get(lang, "")
            paras = [p.strip() for p in body.split("\n\n") if p.strip()]
            sents = []
            for pi, para in enumerate(paras):
                for s in SENT_SPLIT.split(para):
                    if s.strip():
                        sents.append({"s": s.strip(), "p": pi})
            t["sentences"][lang] = sents
    CONTENT.clear()
    CONTENT.update(raw)
    return problems


def tr(obj, lang):
    """Pick a language from {"en":..., "ru":..., "ko":...}; fall back to English."""
    if isinstance(obj, dict):
        return obj.get(lang) or obj.get("en") or next(iter(obj.values()), "")
    return obj


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").lower().replace("ё", "е")
    return re.sub(r"[\W_]+", "", s)


def key_matches(answer, key):
    """Keyword match. Text keys match anywhere (so Russian stems work). Number keys must be a
    whole number in the answer: '8' matches '8 legs' but not '18' or '2008'."""
    k = norm(key)
    if not k:
        return False
    if k.isdigit():
        digits_only = re.sub(r"\D+", " ", unicodedata.normalize("NFKC", answer or ""))
        return k in digits_only.split()
    return k in norm(answer)


def auto_score(answer, item, lang):
    if not norm(answer):
        return 0
    accept = item.get("accept", {})
    keys = accept.get(lang) or accept.get("en") or []
    return int(any(key_matches(answer, k) for k in keys))


def lang_available(lang):
    return all(CONTENT["texts"][tid]["sentences"].get(lang) for tid in TEXT_IDS)


# ---------------------------------------------------------------- design
def conditions(grp):
    """Group A: T1 highlight (usual), T2 test (routine). Group B: swapped."""
    return {"T1": "highlight", "T2": "test"} if grp == "A" else {"T1": "test", "T2": "highlight"}


def text_for(grp, condition):
    return next(t for t, c in conditions(grp).items() if c == condition)


def plan(p, stage):
    usual = text_for(p["grp"], "highlight")
    routine = text_for(p["grp"], "test")
    if stage == "day0":
        return [
            {"id": "read1", "type": "read", "text": "T1", "dur": READ_SEC},
            {"id": "read2", "type": "read", "text": "T2", "dur": READ_SEC},
            {"id": "test", "type": "test", "text": routine, "dur": TEST_MAX_SEC},
            {"id": "feedback", "type": "feedback", "text": routine},
        ]
    if stage == "day2":
        return [
            {"id": "hl1", "type": "highlight", "text": usual, "dur": HIGHLIGHT_SEC, "round": 1},
            {"id": "hl2", "type": "highlight", "text": usual, "dur": HIGHLIGHT_SEC, "round": 2},
            {"id": "test", "type": "test", "text": routine, "dur": TEST_MAX_SEC},
            {"id": "feedback", "type": "feedback", "text": routine},
            {"id": "predict", "type": "predict"},
        ]
    if stage == "day9":
        return [
            {"id": "final1", "type": "final", "text": "T1"},
            {"id": "final2", "type": "final", "text": "T2"},
        ]
    return []


def current_stage(p, now):
    if p["day0_done"] is None:
        return "day0", None
    if p["day2_done"] is None:
        unlock = p["day0_done"] + DAY2_GAP
        return ("day2", None) if now >= unlock else ("wait", unlock)
    if p["day9_done"] is None:
        unlock = p["day2_done"] + DAY9_GAP
        return ("day9", None) if now >= unlock else ("wait", unlock)
    return "done", None


def effective(row):
    return row["override"] if row["override"] is not None else row["auto_correct"]


def scores_for(con, pid):
    rows = con.execute("SELECT * FROM answers WHERE pid=? AND kind='final'", (pid,)).fetchall()
    out = {}
    for cond in ("test", "highlight"):
        r = [x for x in rows if x["condition"] == cond]
        out[cond] = {"correct": sum(effective(x) for x in r), "n": len(r)}
    return out


# ---------------------------------------------------------------- auth helpers
def hash_pin(nickname, pin):
    return hashlib.sha256(f"{nickname.lower()}::{pin}::memory-study".encode()).hexdigest()


def get_participant(con, token):
    if not token:
        raise HTTPException(401, "no token")
    p = con.execute("SELECT * FROM participants WHERE token=?", (token,)).fetchone()
    if not p:
        raise HTTPException(401, "bad token")
    return p


def require_admin(key):
    if not ADMIN_ENABLED:
        raise HTTPException(503, "admin disabled: set ADMIN_KEY (12+ characters) in .env and restart")
    if not key or not secrets.compare_digest(key.encode(), ADMIN_KEY.encode()):
        raise HTTPException(403, "admin key required")


def check_pin(nickname, pin, stored_hash):
    """Compare a PIN, counting failures per nickname. Raises 429 while a nickname is locked."""
    k = nickname.lower()
    now = time.time()
    _fails[k] = [t for t in _fails[k] if now - t < LOGIN_LOCK_SEC]
    if len(_fails[k]) >= LOGIN_MAX_FAILS:
        raise HTTPException(429, "too many wrong PINs, try again later")
    ok = stored_hash is not None and secrets.compare_digest(stored_hash, hash_pin(nickname, pin))
    if ok:
        _fails.pop(k, None)
    else:
        _fails[k].append(now)
    return ok


# ---------------------------------------------------------------- participant API
@app.get("/api/meta")
def meta():
    return {"langs": [l for l in LANGS if lang_available(l)], "server_now": time.time()}


@app.post("/api/join")
async def join(request: Request):
    body = await request.json()
    nickname = (body.get("nickname") or "").strip()
    pin = str(body.get("pin") or "").strip()
    lang = body.get("lang")
    if not (2 <= len(nickname) <= 30) or not re.fullmatch(r"\d{4}", pin):
        raise HTTPException(400, "nickname 2-30 chars and 4-digit PIN required")
    with db() as con:
        existing = con.execute("SELECT * FROM participants WHERE nickname=?", (nickname,)).fetchone()
        if existing:
            if not check_pin(nickname, pin, existing["pin_hash"]):
                raise HTTPException(409, "nickname taken")
            return {"token": existing["token"], "resumed": True}
        if not body.get("consent"):
            raise HTTPException(400, "consent required")
        if lang not in LANGS or not lang_available(lang):
            raise HTTPException(400, "language not available")
        counts = {g: 0 for g in "AB"}
        for r in con.execute("SELECT grp, COUNT(*) c FROM participants WHERE excluded=0 GROUP BY grp"):
            counts[r["grp"]] = r["c"]
        grp = "A" if counts["A"] < counts["B"] else "B" if counts["B"] < counts["A"] else secrets.choice("AB")
        token = secrets.token_urlsafe(24)
        con.execute(
            "INSERT INTO participants (nickname,pin_hash,token,lang,grp,created_at) VALUES (?,?,?,?,?,?)",
            (nickname, hash_pin(nickname, pin), token, lang, grp, time.time()),
        )
        return {"token": token, "resumed": False}


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    nickname = (body.get("nickname") or "").strip()
    pin = str(body.get("pin") or "").strip()
    with db() as con:
        p = con.execute("SELECT * FROM participants WHERE nickname=?", (nickname,)).fetchone()
        if not check_pin(nickname, pin, p["pin_hash"] if p else None):
            raise HTTPException(401, "wrong nickname or PIN")
        return {"token": p["token"]}


def step_payload(con, p, step, now):
    lang = p["lang"]
    t = CONTENT["texts"].get(step.get("text", ""), {})
    payload = {"id": step["id"], "type": step["type"]}
    if "text" in step:
        payload["text"] = step["text"]
        payload["title"] = tr(t.get("title", step["text"]), lang)
    if step["type"] in ("read", "highlight"):
        payload["sentences"] = t["sentences"][lang]
        if step["type"] == "highlight":
            payload["round"] = step["round"]
            prev = con.execute(
                "SELECT sentences FROM highlights WHERE pid=? AND text_id=? ORDER BY id DESC LIMIT 1",
                (p["id"], step["text"]),
            ).fetchone()
            payload["selected"] = json.loads(prev["sentences"]) if prev else []
    elif step["type"] == "test":
        payload["questions"] = [{"qid": q["id"], "q": tr(q["q"], lang)} for q in t["practice"]]
    elif step["type"] == "feedback":
        rows = con.execute(
            "SELECT * FROM answers WHERE pid=? AND stage=? AND kind='practice' ORDER BY id",
            (p["id"], p["stage"]),
        ).fetchall()
        mine = {r["qid"]: r for r in rows}
        payload["items"] = [
            {
                "q": tr(q["q"], lang),
                "answer": tr(q["answer"], lang),
                "yours": mine[q["id"]]["answer"] if q["id"] in mine else "",
                "confidence": mine[q["id"]]["confidence"] if q["id"] in mine else None,
                "correct": bool(mine[q["id"]]["auto_correct"]) if q["id"] in mine else False,
            }
            for q in t["practice"]
        ]
    elif step["type"] == "predict":
        payload["texts"] = [
            {"text": tid, "title": tr(CONTENT["texts"][tid]["title"], lang), "max": len(CONTENT["texts"][tid]["final"])}
            for tid in TEXT_IDS
        ]
    elif step["type"] == "final":
        payload["questions"] = [{"qid": q["id"], "q": tr(q["q"], lang)} for q in t["final"]]
    if step["type"] == "feedback":
        dur = max(FEEDBACK_MIN_SEC, BLOCK_SEC - (p["test_used"] or 0))
    else:
        dur = step.get("dur")
    if dur:
        payload["dur"] = dur
        payload["remaining"] = max(0, dur - (now - p["step_started"]))
    return payload


@app.get("/api/state")
def state(x_token: str = Header(None)):
    now = time.time()
    with db() as con:
        p = get_participant(con, x_token)
        stage, unlock = current_stage(p, now)
        base = {"nickname": p["nickname"], "lang": p["lang"], "server_now": now}
        if stage == "wait":
            nxt = "day2" if p["day2_done"] is None else "day9"
            return {**base, "stage": "wait", "next": nxt, "unlock_at": unlock}
        if stage == "done":
            s = scores_for(con, p["id"])
            preds = {r["condition"]: dict(r) for r in con.execute("SELECT * FROM predictions WHERE pid=?", (p["id"],))}
            return {**base, "stage": "done", "scores": s,
                    "predicted": {c: preds[c]["predicted"] for c in preds}}
        if p["stage"] != stage:
            con.execute("UPDATE participants SET stage=?, step=0, step_started=?, test_used=NULL WHERE id=?",
                        (stage, now, p["id"]))
            con.execute("INSERT INTO events (pid,stage,step,type,ts) VALUES (?,?,?,?,?)",
                        (p["id"], stage, "", "start", now))
            p = con.execute("SELECT * FROM participants WHERE id=?", (p["id"],)).fetchone()
        steps = plan(p, stage)
        step = steps[p["step"]]
        if p["step_started"] is None:
            con.execute("UPDATE participants SET step_started=? WHERE id=?", (now, p["id"]))
            p = con.execute("SELECT * FROM participants WHERE id=?", (p["id"],)).fetchone()
        return {**base, "stage": stage, "step_index": p["step"], "step_count": len(steps),
                "step": step_payload(con, p, step, now)}


@app.post("/api/complete")
async def complete(request: Request, x_token: str = Header(None)):
    body = await request.json()
    now = time.time()
    with db() as con:
        p = get_participant(con, x_token)
        stage, _ = current_stage(p, now)
        if stage not in ("day0", "day2", "day9") or p["stage"] != stage:
            raise HTTPException(409, "not in an active session")
        steps = plan(p, stage)
        step = steps[p["step"]]
        if body.get("step_id") != step["id"]:
            raise HTTPException(409, "step mismatch, reload")
        elapsed = now - (p["step_started"] or now)
        data = body.get("data") or {}
        lang = p["lang"]
        conds = conditions(p["grp"])

        if step["type"] in ("read", "highlight") and elapsed < step["dur"] - TOLERANCE:
            raise HTTPException(409, "time not up yet")
        if step["type"] == "feedback":
            need = max(FEEDBACK_MIN_SEC, BLOCK_SEC - (p["test_used"] or 0))
            if elapsed < need - TOLERANCE:
                raise HTTPException(409, "time not up yet")

        if step["type"] == "highlight":
            sel = sorted({int(i) for i in data.get("selected", [])})
            con.execute("INSERT INTO highlights (pid,round,text_id,sentences,ts) VALUES (?,?,?,?,?)",
                        (p["id"], step["round"], step["text"], json.dumps(sel), now))
        elif step["type"] in ("test", "final"):
            t = CONTENT["texts"][step["text"]]
            items = {q["id"]: q for q in (t["practice"] if step["type"] == "test" else t["final"])}
            given = {a.get("qid"): a for a in data.get("answers", [])}
            kind = "practice" if step["type"] == "test" else "final"
            for qid, q in items.items():
                a = given.get(qid, {})
                conf = a.get("confidence")
                con.execute(
                    "INSERT INTO answers (pid,stage,kind,text_id,condition,qid,fact,answer,confidence,auto_correct,rt_ms,ts)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (p["id"], stage, kind, step["text"], conds[step["text"]], qid, q.get("fact", qid),
                     (a.get("answer") or "")[:500], int(conf) if conf is not None else None,
                     auto_score(a.get("answer"), q, lang), a.get("rt_ms"), now),
                )
            if step["type"] == "test":
                con.execute("UPDATE participants SET test_used=? WHERE id=?", (min(elapsed, TEST_MAX_SEC), p["id"]))
        elif step["type"] == "predict":
            for tid in TEXT_IDS:
                mx = len(CONTENT["texts"][tid]["final"])
                val = max(0, min(mx, int(data.get("pred", {}).get(tid, 0))))
                con.execute("INSERT INTO predictions (pid,text_id,condition,predicted,max,ts) VALUES (?,?,?,?,?,?)",
                            (p["id"], tid, conds[tid], val, mx, now))
            belief = data.get("belief")
            if belief in ("highlight", "test", "same"):
                con.execute("UPDATE participants SET belief=? WHERE id=?", (belief, p["id"]))

        nxt = p["step"] + 1
        if nxt >= len(steps):
            col = {"day0": "day0_done", "day2": "day2_done", "day9": "day9_done"}[stage]
            con.execute(f"UPDATE participants SET {col}=?, stage=NULL, step=0, step_started=NULL WHERE id=?",
                        (now, p["id"]))
        else:
            con.execute("UPDATE participants SET step=?, step_started=? WHERE id=?", (nxt, now, p["id"]))
        return {"ok": True}


@app.post("/api/event")
async def event(request: Request, x_token: str = Header(None)):
    body = await request.json()
    if body.get("type") not in ("hidden",):
        raise HTTPException(400, "unknown event type")
    with db() as con:
        p = get_participant(con, x_token)
        con.execute("INSERT INTO events (pid,stage,step,type,ts) VALUES (?,?,?,?,?)",
                    (p["id"], p["stage"], str(body.get("step_id", ""))[:40], body["type"], time.time()))
    return {"ok": True}


# ---------------------------------------------------------------- admin API
def participant_summary(con, p, now):
    stage, unlock = current_stage(p, now)
    s = scores_for(con, p["id"])
    preds = {r["condition"]: r["predicted"] for r in con.execute("SELECT * FROM predictions WHERE pid=?", (p["id"],))}
    hidden = con.execute("SELECT COUNT(*) c FROM events WHERE pid=? AND type='hidden'", (p["id"],)).fetchone()["c"]
    starts = {r["stage"]: r["ts"] for r in con.execute(
        "SELECT stage, MIN(ts) ts FROM events WHERE pid=? AND type='start' GROUP BY stage", (p["id"],))}

    def minutes(stage_name, done):
        return round((done - starts[stage_name]) / 60, 1) if done and stage_name in starts else None
    if stage == "wait":
        status, due = ("waiting for Day 2" if p["day2_done"] is None else "waiting for Day 9"), unlock
    elif stage == "done":
        status, due = "done", None
    else:
        status = f"{stage} open" + (" (in progress)" if p["stage"] == stage else "")
        due = (p["day0_done"] + DAY2_GAP) if stage == "day2" else (p["day2_done"] + DAY9_GAP) if stage == "day9" else p["created_at"]
    return {
        "id": p["id"], "nickname": p["nickname"], "lang": p["lang"], "group": p["grp"],
        "excluded": bool(p["excluded"]), "status": status, "stage": stage, "due_at": due,
        "created_at": p["created_at"], "day0_done": p["day0_done"], "day2_done": p["day2_done"], "day9_done": p["day9_done"],
        "gap_day0_day2_h": round((p["day2_done"] - p["day0_done"]) / 3600, 1) if p["day2_done"] and p["day0_done"] else None,
        "gap_day2_day9_h": round((p["day9_done"] - p["day2_done"]) / 3600, 1) if p["day9_done"] and p["day2_done"] else None,
        "routine_correct": s["test"]["correct"], "routine_n": s["test"]["n"],
        "usual_correct": s["highlight"]["correct"], "usual_n": s["highlight"]["n"],
        "pred_routine": preds.get("test"), "pred_usual": preds.get("highlight"),
        "belief": p["belief"], "tab_switches": hidden,
        "s1_min": minutes("day0", p["day0_done"]), "s2_min": minutes("day2", p["day2_done"]),
        "s3_min": minutes("day9", p["day9_done"]),
    }


@app.get("/api/admin/overview")
def admin_overview(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    now = time.time()
    with db() as con:
        rows = [participant_summary(con, p, now) for p in con.execute("SELECT * FROM participants ORDER BY id")]
    return {"server_now": now, "participants": rows,
            "config": {"DAY2_GAP_SEC": DAY2_GAP, "DAY9_GAP_SEC": DAY9_GAP, "READ_SEC": READ_SEC,
                       "HIGHLIGHT_SEC": HIGHLIGHT_SEC, "TEST_MAX_SEC": TEST_MAX_SEC, "BLOCK_SEC": BLOCK_SEC}}


@app.get("/api/admin/answers")
def admin_answers(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    with db() as con:
        rows = con.execute(
            "SELECT a.*, p.nickname, p.lang FROM answers a JOIN participants p ON p.id=a.pid"
            " WHERE a.kind='final' ORDER BY a.text_id, a.qid, a.auto_correct, a.id"
        ).fetchall()
    out = []
    for r in rows:
        q = next((x for x in CONTENT["texts"][r["text_id"]]["final"] if x["id"] == r["qid"]), {})
        out.append({**dict(r), "question": tr(q.get("q", ""), r["lang"]), "key": tr(q.get("answer", ""), r["lang"]),
                    "effective": effective(r)})
    return {"answers": out}


@app.post("/api/admin/grade")
async def admin_grade(request: Request, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    body = await request.json()
    val = body.get("override")
    if val not in (0, 1, None):
        raise HTTPException(400, "override must be 0, 1 or null")
    with db() as con:
        con.execute("UPDATE answers SET override=? WHERE id=?", (val, int(body["id"])))
    return {"ok": True}


@app.post("/api/admin/exclude")
async def admin_exclude(request: Request, x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    body = await request.json()
    with db() as con:
        con.execute("UPDATE participants SET excluded=? WHERE id=?", (1 if body.get("excluded") else 0, int(body["id"])))
    return {"ok": True}


@app.post("/api/admin/reload-content")
def admin_reload(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    try:
        problems = load_content()
    except (ValueError, KeyError, RuntimeError) as e:  # bad JSON: keep the old content
        raise HTTPException(400, f"content.json not loaded: {e}")
    return {"ok": True, "langs": [l for l in LANGS if lang_available(l)], "problems": problems}


@app.get("/api/admin/content-check")
def admin_content_check(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    raw = json.loads(CONTENT_FILE.read_text(encoding="utf-8"))
    return {"problems": validate_content(raw)}


@app.get("/api/admin/backup.db")
def admin_backup(x_admin_key: str = Header(None), key: str = ""):
    """Consistent copy of the live SQLite database (safe while participants are active)."""
    require_admin(x_admin_key or key)
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    src = connect()
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    stamp = time.strftime("%Y-%m-%d_%H%M")
    return FileResponse(tmp, media_type="application/octet-stream", filename=f"study-{stamp}.db",
                        background=BackgroundTask(os.unlink, tmp))


def csv_response(name, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    data = "﻿" + buf.getvalue()  # BOM so Excel opens Korean/Russian correctly
    return StreamingResponse(iter([data]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/admin/export/{what}.csv")
def admin_export(what: str, key: str = "", x_admin_key: str = Header(None)):
    require_admin(x_admin_key or key)
    now = time.time()
    with db() as con:
        if what == "participants":
            rows = [participant_summary(con, p, now) for p in con.execute("SELECT * FROM participants ORDER BY id")]
            header = list(rows[0].keys()) if rows else ["id"]
            return csv_response("participants.csv", header, [[r[h] for h in header] for r in rows])
        if what == "answers":
            rows = con.execute(
                "SELECT a.id, p.nickname, p.grp, p.lang, p.excluded, a.stage, a.kind, a.text_id, a.condition, a.qid, a.fact,"
                " a.answer, a.confidence, a.auto_correct, a.override,"
                " COALESCE(a.override, a.auto_correct) AS correct, a.rt_ms, a.ts"
                " FROM answers a JOIN participants p ON p.id=a.pid ORDER BY a.pid, a.id"
            ).fetchall()
            header = rows[0].keys() if rows else ["id"]
            return csv_response("answers.csv", header, [list(r) for r in rows])
        if what == "highlights":
            rows = con.execute(
                "SELECT h.id, p.nickname, p.grp, p.lang, h.round, h.text_id, h.sentences, h.ts"
                " FROM highlights h JOIN participants p ON p.id=h.pid ORDER BY h.pid, h.id"
            ).fetchall()
            header = rows[0].keys() if rows else ["id"]
            return csv_response("highlights.csv", header, [list(r) for r in rows])
        if what == "events":
            rows = con.execute(
                "SELECT e.id, p.nickname, e.stage, e.step, e.type, e.ts FROM events e JOIN participants p ON p.id=e.pid ORDER BY e.id"
            ).fetchall()
            header = rows[0].keys() if rows else ["id"]
            return csv_response("events.csv", header, [list(r) for r in rows])
    raise HTTPException(404, "unknown export")


# ---------------------------------------------------------------- pages
@app.middleware("http")
async def security_headers(request: Request, call_next):
    res = await call_next(request)
    res.headers.setdefault("X-Content-Type-Options", "nosniff")
    res.headers.setdefault("Referrer-Policy", "no-referrer")
    res.headers.setdefault("X-Frame-Options", "DENY")
    if request.url.path.startswith("/api/"):
        res.headers.setdefault("Cache-Control", "no-store")
    elif request.url.path.startswith("/static/"):
        res.headers.setdefault("Cache-Control", "no-cache")  # revalidate, so updates reach phones
    return res


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/admin")
def admin_page():
    return FileResponse(BASE / "static" / "admin.html", headers={"Cache-Control": "no-store"})


REMINDER_TEXT = {
    "en": ("Memory study – session {n}", "Your next memory study session is open. Log in with your nickname and PIN: {url}"),
    "ru": ("Исследование памяти – сессия {n}", "Следующая сессия исследования памяти открыта. Войдите с никнеймом и PIN: {url}"),
    "ko": ("기억 연구 – 세션 {n}", "다음 기억 연구 세션이 열렸습니다. 닉네임과 PIN으로 로그인하세요: {url}"),
}


@app.get("/reminder.ics")
def reminder(request: Request, at: int, n: int = 2, lang: str = "en"):
    """Calendar event for the next session (no personal data: just a time and the study URL).
    Served as a file so iPhone/iPad Safari offers 'Add to Calendar' directly."""
    if n not in (2, 3) or not (time.time() - 86400 < at < time.time() + 30 * 86400):
        raise HTTPException(400, "bad reminder")
    title, body = REMINDER_TEXT.get(lang, REMINDER_TEXT["en"])
    url = str(request.base_url).rstrip("/")
    if request.headers.get("x-forwarded-proto") == "https":
        url = url.replace("http://", "https://", 1)
    fmt = lambda ts: time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts))
    esc = lambda x: x.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//memory-study//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:session{n}-{at}@memory-study", f"DTSTAMP:{fmt(time.time())}",
        f"DTSTART:{fmt(at)}", f"DTEND:{fmt(at + 1800)}",
        f"SUMMARY:{esc(title.format(n=n))}", f"DESCRIPTION:{esc(body.format(url=url))}", f"URL:{url}",
        "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:PT0M", f"DESCRIPTION:{esc(title.format(n=n))}", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR",
    ]
    return StreamingResponse(iter(["\r\n".join(lines) + "\r\n"]), media_type="text/calendar; charset=utf-8",
                             headers={"Content-Disposition": f'inline; filename="memory-study-session{n}.ics"'})


@app.get("/healthz")
def health():
    return JSONResponse({"ok": True})
