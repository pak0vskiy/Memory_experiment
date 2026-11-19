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
READ_SEC = env_int("READ_SEC", 210)          # Day 0: first reading of each text (~2-3 reads of ~240 words)
HIGHLIGHT_SEC = env_int("HIGHLIGHT_SEC", 210)  # Day 2: each highlight + reread round (2 rounds)
TEST_MAX_SEC = env_int("TEST_MAX_SEC", 150)  # self-test answering limit (10 questions)
BLOCK_SEC = env_int("BLOCK_SEC", 210)        # self-test + feedback block total (matches one reading round)
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
    with db() as con:
        migrate(con)
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
  stage TEXT, step TEXT, type TEXT NOT NULL, ts REAL NOT NULL, detail TEXT
);
-- one row per completed step: server-side timing + small client extras (scroll depth, auto-submit)
CREATE TABLE IF NOT EXISTS steps (
  id INTEGER PRIMARY KEY, pid INTEGER NOT NULL REFERENCES participants(id),
  stage TEXT NOT NULL, step_id TEXT NOT NULL, type TEXT NOT NULL, text_id TEXT, condition TEXT,
  started REAL, finished REAL NOT NULL, elapsed_sec REAL, hidden_n INTEGER, away_sec REAL, extra TEXT
);
-- every highlight tap: which sentence, on/off, ms since the round started
CREATE TABLE IF NOT EXISTS highlight_log (
  id INTEGER PRIMARY KEY, pid INTEGER NOT NULL REFERENCES participants(id),
  round INTEGER NOT NULL, text_id TEXT NOT NULL, sentence INTEGER NOT NULL, action TEXT NOT NULL,
  t_ms INTEGER, ts REAL NOT NULL, method TEXT
);
-- end-of-study questionnaire, one row per item
CREATE TABLE IF NOT EXISTS survey (
  id INTEGER PRIMARY KEY, pid INTEGER NOT NULL REFERENCES participants(id),
  item TEXT NOT NULL, value TEXT, ts REAL NOT NULL
);
"""

# Columns added after the first version: added in place to an existing database.
MIGRATIONS = {
    "answers": {"first_ms": "INTEGER", "edits": "INTEGER"},
    "events": {"detail": "TEXT"},
    "highlight_log": {"method": "TEXT"},
}


def migrate(con):
    for table, cols in MIGRATIONS.items():
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


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
                        if len(nk) == 1 and not nk.isdigit() and not ("\uac00" <= nk <= "\ud7a3") and not k.startswith("="):
                            out.append(f"{where}: keyword '{k}' ({l}) is a single letter and will match almost anything")
                if kind == "practice":
                    practice_facts.add(q.get("fact", q.get("id")))
                elif q.get("fact") not in practice_facts:
                    out.append(f"{where}: fact '{q.get('fact')}' has no matching practice question (hypercorrection analysis needs one)")
    return out


def source_sentences(t, q, lang):
    """Indices of the sentence(s) in text `t` that contain the answer to question `q`.
    Uses q["source"][lang] (a snippet of the sentence, or a list of snippets) if given, otherwise the
    sentences that match the question's accept keywords. Used to tell whether a fact was highlighted."""
    sents = t["sentences"].get(lang) or []
    src = (q.get("source") or {}).get(lang)
    if src:
        snippets = [src] if isinstance(src, str) else src
        return [i for i, x in enumerate(sents) if any(norm(sn) and norm(sn) in norm(x["s"]) for sn in snippets)]
    keys = (q.get("accept") or {}).get(lang) or []
    return [i for i, x in enumerate(sents) if any(key_matches(x["s"], k) for k in keys)]


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
    for tid in TEXT_IDS:
        t = texts[tid]
        for q in t.get("final", []) + t.get("practice", []):
            q["_src"] = {l: source_sentences(t, q, l) for l in LANGS if t["sentences"].get(l)}
            if q in t.get("final", []):
                for l, idx in q["_src"].items():
                    how = "'source' snippet" if (q.get("source") or {}).get(l) else "accept keywords"
                    if not idx:
                        problems.append(f"{tid}/final/{q['id']}: no sentence found for this fact in '{l}' via {how};"
                                        f" add \"source\": {{\"{l}\": \"<words from the sentence>\"}} so highlights can be linked")
                    elif len(idx) > 1 and how == "accept keywords":
                        problems.append(f"{tid}/final/{q['id']}: keywords match {len(idx)} sentences in '{l}' {idx};"
                                        f" add a 'source' snippet to pick the right one")
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
    whole number in the answer: '8' matches '8 legs' but not '18' or '2008'. A key starting with
    '=' must equal the whole answer: '=암' accepts '암' but not '암컷' (useful for one-syllable words)."""
    if key.startswith("="):
        return bool(norm(key[1:])) and norm(answer) == norm(key[1:])
    k = norm(key)
    if not k:
        return False
    if k.isdigit():
        # numbers in the answer, with decimal/thousands separators dropped: "0.5" -> "05", "2,007" -> "2007"
        nums = re.findall(r"\d+(?:[.,]\d+)*", unicodedata.normalize("NFKC", answer or ""))
        return k in [re.sub(r"[.,]", "", n) for n in nums]
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
    # Each session opens with an untimed "how this works" screen (time spent on it is logged).
    if stage == "day0":
        return [
            {"id": "intro", "type": "intro", "variant": "s1"},
            {"id": "read1", "type": "read", "text": "T1", "dur": READ_SEC},
            {"id": "read2", "type": "read", "text": "T2", "dur": READ_SEC},
            {"id": "test", "type": "test", "text": routine, "dur": TEST_MAX_SEC},
            {"id": "feedback", "type": "feedback", "text": routine},
        ]
    if stage == "day2":
        return [
            {"id": "intro", "type": "intro", "variant": "s2"},
            {"id": "hl1", "type": "highlight", "text": usual, "dur": HIGHLIGHT_SEC, "round": 1},
            {"id": "hl2", "type": "highlight", "text": usual, "dur": HIGHLIGHT_SEC, "round": 2},
            {"id": "test", "type": "test", "text": routine, "dur": TEST_MAX_SEC},
            {"id": "feedback", "type": "feedback", "text": routine},
            {"id": "predict", "type": "predict"},
        ]
    if stage == "day9":
        return [
            {"id": "intro", "type": "intro", "variant": "s3"},
            {"id": "final1", "type": "final", "text": "T1"},
            {"id": "final2", "type": "final", "text": "T2"},
            {"id": "survey", "type": "survey"},
        ]
    return []


# End-of-study questionnaire (after the final test, before the results are shown).
# item -> allowed values. Labels live in i18n.js.
SURVEY = {
    "belief_after": ("highlight", "test", "same"),     # which method worked better (asked again)
    "harder": ("highlight", "test", "same"),           # which felt harder / more effortful
    "prior_T1": ("1", "2", "3", "4", "5"),             # prior knowledge of each topic
    "prior_T2": ("1", "2", "3", "4", "5"),
    "reviewed": ("no", "little", "yes"),               # looked up / reviewed / discussed between sessions
    "notes": ("no", "yes"),                            # wrote things down / screenshots
    "habit": ("reread", "highlight", "selftest", "notes", "other"),  # usual way of studying for exams
}


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
    elif step["type"] == "intro":
        payload["variant"] = step["variant"]
        # real durations, so the instructions never state the wrong time (pilot timings differ)
        payload["timings"] = {"read": READ_SEC, "highlight": HIGHLIGHT_SEC, "test": TEST_MAX_SEC, "block": BLOCK_SEC}
    elif step["type"] == "survey":
        payload["texts"] = [{"text": tid, "title": tr(CONTENT["texts"][tid]["title"], lang)} for tid in TEXT_IDS]
        payload["items"] = {k: list(v) for k, v in SURVEY.items()}
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

        if step["type"] == "survey":
            answers = data.get("answers") or {}
            missing = [k for k, ok in SURVEY.items() if str(answers.get(k)) not in ok]
            if missing:
                raise HTTPException(400, f"survey incomplete: {', '.join(missing)}")
            for k in SURVEY:
                con.execute("INSERT INTO survey (pid,item,value,ts) VALUES (?,?,?,?)", (p["id"], k, str(answers[k]), now))

        if step["type"] == "highlight":
            n_sent = len(CONTENT["texts"][step["text"]]["sentences"][lang])
            sel = sorted({int(i) for i in data.get("selected", []) if 0 <= int(i) < n_sent})
            con.execute("INSERT INTO highlights (pid,round,text_id,sentences,ts) VALUES (?,?,?,?,?)",
                        (p["id"], step["round"], step["text"], json.dumps(sel), now))
            for ev in (data.get("log") or [])[:2000]:
                try:
                    i, action, t_ms = int(ev.get("i")), ev.get("a"), int(ev.get("t") or 0)
                except (TypeError, ValueError, AttributeError):
                    continue
                method = ev.get("m") if ev.get("m") in ("touch", "pen", "mouse", "key") else None
                if 0 <= i < n_sent and action in ("on", "off"):
                    con.execute("INSERT INTO highlight_log (pid,round,text_id,sentence,action,t_ms,ts,method)"
                                " VALUES (?,?,?,?,?,?,?,?)",
                                (p["id"], step["round"], step["text"], i, action, t_ms, now, method))
        elif step["type"] in ("test", "final"):
            t = CONTENT["texts"][step["text"]]
            items = {q["id"]: q for q in (t["practice"] if step["type"] == "test" else t["final"])}
            given = {a.get("qid"): a for a in data.get("answers", [])}
            kind = "practice" if step["type"] == "test" else "final"
            for qid, q in items.items():
                a = given.get(qid, {})
                con.execute(
                    "INSERT INTO answers (pid,stage,kind,text_id,condition,qid,fact,answer,confidence,auto_correct,"
                    "rt_ms,first_ms,edits,ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (p["id"], stage, kind, step["text"], conds[step["text"]], qid, q.get("fact", qid),
                     str(a.get("answer") or "")[:500], small_int(a.get("confidence"), 1, 5),
                     auto_score(a.get("answer"), q, lang), small_int(a.get("rt_ms"), 0, 10**8),
                     small_int(a.get("first_ms"), 0, 10**8), small_int(a.get("edits"), 0, 10**6), now),
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

        # timing + behaviour for every step
        started = p["step_started"]
        evs = con.execute("SELECT type, ts FROM events WHERE pid=? AND ts>=? AND type IN ('hidden','visible') ORDER BY ts",
                          (p["id"], started or now)).fetchall()
        extra = {}
        if isinstance(data.get("scroll_max"), (int, float)):
            extra["scroll_max"] = round(max(0.0, min(1.0, float(data["scroll_max"]))), 3)
        if step["type"] == "test":
            extra["auto_submit"] = bool(data.get("auto"))
        con.execute(
            "INSERT INTO steps (pid,stage,step_id,type,text_id,condition,started,finished,elapsed_sec,hidden_n,away_sec,extra)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (p["id"], stage, step["id"], step["type"], step.get("text"), conds.get(step.get("text")),
             started, now, round(elapsed, 2), sum(1 for e in evs if e["type"] == "hidden"),
             round(away_seconds(evs, now), 1), json.dumps(extra) if extra else None))

        nxt = p["step"] + 1
        if nxt >= len(steps):
            col = {"day0": "day0_done", "day2": "day2_done", "day9": "day9_done"}[stage]
            con.execute(f"UPDATE participants SET {col}=?, stage=NULL, step=0, step_started=NULL WHERE id=?",
                        (now, p["id"]))
        else:
            con.execute("UPDATE participants SET step=?, step_started=? WHERE id=?", (nxt, now, p["id"]))
        return {"ok": True}


def small_int(v, lo, hi):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def away_seconds(evs, now):
    """Total time between each 'hidden' event and the next 'visible' one (page in background)."""
    total, hid = 0.0, None
    for e in evs:
        if e["type"] == "hidden" and hid is None:
            hid = e["ts"]
        elif e["type"] == "visible" and hid is not None:
            total += e["ts"] - hid
            hid = None
    if hid is not None:
        total += now - hid
    return total


EVENT_TYPES = ("hidden", "visible", "device")


@app.post("/api/event")
async def event(request: Request, x_token: str = Header(None)):
    body = await request.json()
    if body.get("type") not in EVENT_TYPES:
        raise HTTPException(400, "unknown event type")
    detail = body.get("detail")
    detail = json.dumps(detail, ensure_ascii=False)[:1500] if isinstance(detail, dict) else None
    with db() as con:
        p = get_participant(con, x_token)
        con.execute("INSERT INTO events (pid,stage,step,type,ts,detail) VALUES (?,?,?,?,?,?)",
                    (p["id"], p["stage"], str(body.get("step_id", ""))[:40], body["type"], time.time(), detail))
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
    away = con.execute("SELECT COALESCE(SUM(away_sec),0) a FROM steps WHERE pid=?", (p["id"],)).fetchone()["a"]
    dev = con.execute("SELECT detail FROM events WHERE pid=? AND type='device' ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
    dev = json.loads(dev["detail"]) if dev and dev["detail"] else {}
    surv = {r["item"]: r["value"] for r in con.execute("SELECT item, value FROM survey WHERE pid=? ORDER BY id", (p["id"],))}
    hl = {}
    for r in con.execute("SELECT round, sentences FROM highlights WHERE pid=? ORDER BY id", (p["id"],)):
        hl[r["round"]] = len(json.loads(r["sentences"]))
    usual_text = text_for(p["grp"], "highlight")
    n_sent = len(CONTENT["texts"][usual_text]["sentences"].get(p["lang"]) or []) or None
    fconf = {c: con.execute("SELECT AVG(confidence) m FROM answers WHERE pid=? AND kind='final' AND condition=?",
                            (p["id"], c)).fetchone()["m"] for c in ("test", "highlight")}
    auto = con.execute("SELECT COUNT(*) c FROM steps WHERE pid=? AND type='test' AND extra LIKE '%\"auto_submit\": true%'",
                       (p["id"],)).fetchone()["c"]
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
        "away_sec": round(away, 1), "tests_auto_submitted": auto,
        "routine_text": text_for(p["grp"], "test"), "usual_text": usual_text,
        "hl_sentences_r1": hl.get(1), "hl_sentences_r2": hl.get(2), "text_sentences": n_sent,
        "hl_share_r2": round(hl[2] / n_sent, 3) if hl.get(2) is not None and n_sent else None,
        "final_conf_routine": round(fconf["test"], 2) if fconf["test"] is not None else None,
        "final_conf_usual": round(fconf["highlight"], 2) if fconf["highlight"] is not None else None,
        **{f"survey_{k}": surv.get(k) for k in SURVEY},
        "device": dev.get("kind"), "screen": f'{dev.get("sw")}x{dev.get("sh")}' if dev.get("sw") else None,
        "tz": dev.get("tz"), "user_agent": dev.get("ua"),
    }


def build_items(con):
    """Analysis-ready table: one row per participant x final question, with everything known about
    that fact for that person (practice history, confidence, whether its sentence was highlighted)."""
    out = []
    parts = {p["id"]: p for p in con.execute("SELECT * FROM participants")}
    hl = {}
    for r in con.execute("SELECT pid, round, text_id, sentences FROM highlights ORDER BY id"):
        hl[(r["pid"], r["round"])] = set(json.loads(r["sentences"]))
    preds = {(r["pid"], r["text_id"]): r["predicted"] for r in con.execute("SELECT * FROM predictions")}
    prior = {(r["pid"], r["item"]): r["value"] for r in con.execute("SELECT * FROM survey WHERE item LIKE 'prior_%'")}
    practice = {}
    for r in con.execute("SELECT * FROM answers WHERE kind='practice' ORDER BY id"):
        practice[(r["pid"], r["stage"], r["text_id"], r["fact"])] = r
    for a in con.execute("SELECT * FROM answers WHERE kind='final' ORDER BY pid, text_id, id"):
        p = parts[a["pid"]]
        t = CONTENT["texts"][a["text_id"]]
        q = next((x for x in t["final"] if x["id"] == a["qid"]), {})
        src = (q.get("_src") or {}).get(p["lang"], [])
        s0 = practice.get((p["id"], "day0", a["text_id"], a["fact"]))
        s2 = practice.get((p["id"], "day2", a["text_id"], a["fact"]))
        is_usual = a["condition"] == "highlight"

        def was_hl(rnd):
            if not is_usual or (p["id"], rnd) not in hl or not src:
                return None
            return int(any(i in hl[(p["id"], rnd)] for i in src))
        if s0 is None:
            hyper = None
        elif effective(s0):
            hyper = "correct"
        else:
            hyper = "high-conf error" if (s0["confidence"] or 0) >= 4 else "low-conf error"
        out.append({
            "nickname": p["nickname"], "group": p["grp"], "lang": p["lang"], "excluded": p["excluded"],
            "text_id": a["text_id"], "condition": a["condition"], "read_first": int(a["text_id"] == "T1"),
            "qid": a["qid"], "fact": a["fact"],
            "final_answer": a["answer"], "final_correct": effective(a), "final_confidence": a["confidence"],
            "final_rt_ms": a["rt_ms"], "final_first_ms": a["first_ms"], "final_edits": a["edits"],
            "s1_answer": s0["answer"] if s0 else None, "s1_correct": effective(s0) if s0 else None,
            "s1_confidence": s0["confidence"] if s0 else None,
            "s2_answer": s2["answer"] if s2 else None, "s2_correct": effective(s2) if s2 else None,
            "s2_confidence": s2["confidence"] if s2 else None,
            "s1_error_type": hyper,
            "source_sentences": json.dumps(src), "highlighted_r1": was_hl(1), "highlighted_r2": was_hl(2),
            "predicted_for_text": preds.get((p["id"], a["text_id"])), "text_max": len(t["final"]),
            "prior_knowledge": prior.get((p["id"], f"prior_{a['text_id']}")),
            "belief": p["belief"],
        })
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def insights(con):
    """Headline numbers for the presentation, from non-excluded participants who finished."""
    done = {p["nickname"] for p in con.execute("SELECT nickname FROM participants WHERE day9_done IS NOT NULL AND excluded=0")}
    items = [r for r in build_items(con) if r["nickname"] in done]
    by = lambda f: [r for r in items if f(r)]

    def acc(rows):
        return {"n": len(rows), "accuracy": _mean([r["final_correct"] for r in rows])}
    out = {"participants": len(done), "items": len(items)}
    out["by_condition"] = {c: {**acc(by(lambda r, c=c: r["condition"] == c)),
                               "mean_confidence": _mean([r["final_confidence"] for r in items if r["condition"] == c])}
                           for c in ("test", "highlight")}
    out["highlighting"] = {k: acc(by(lambda r, v=v: r["highlighted_r2"] == v))
                           for k, v in (("highlighted", 1), ("not_highlighted", 0))}
    out["s1_errors"] = {k: acc(by(lambda r, k=k: r["s1_error_type"] == k))
                        for k in ("correct", "high-conf error", "low-conf error")}
    # calibration: accuracy at each final confidence level, per condition
    out["calibration"] = {c: {str(lv): acc(by(lambda r, c=c, lv=lv: r["condition"] == c and r["final_confidence"] == lv))
                              for lv in range(1, 6)} for c in ("test", "highlight")}
    # prediction vs reality, per condition (as % of the text's questions)
    pr = {}
    for c in ("test", "highlight"):
        rows = by(lambda r, c=c: r["condition"] == c)
        people = {}
        for r in rows:
            people.setdefault(r["nickname"], r)
        pr[c] = {"predicted_pct": _mean([100 * x["predicted_for_text"] / x["text_max"] for x in people.values()
                                         if x["predicted_for_text"] is not None]),
                 "actual_pct": _mean([100 * a for a in [r["final_correct"] for r in rows]])}
    out["prediction_vs_actual"] = pr
    beliefs = {}
    for r in con.execute("SELECT p.belief b, s.value a FROM participants p LEFT JOIN survey s ON s.pid=p.id AND"
                         " s.item='belief_after' WHERE p.day9_done IS NOT NULL AND p.excluded=0"):
        beliefs.setdefault("before_test", {}).setdefault(r["b"] or "?", 0)
        beliefs["before_test"][r["b"] or "?"] += 1
        beliefs.setdefault("after_test", {}).setdefault(r["a"] or "?", 0)
        beliefs["after_test"][r["a"] or "?"] += 1
    out["beliefs"] = beliefs
    return out


@app.get("/api/admin/insights")
def admin_insights(x_admin_key: str = Header(None)):
    require_admin(x_admin_key)
    with db() as con:
        return insights(con)


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
            " ORDER BY a.kind, a.text_id, a.qid, a.auto_correct, a.id"
        ).fetchall()
    out = []
    for r in rows:
        pool = CONTENT["texts"][r["text_id"]]["final" if r["kind"] == "final" else "practice"]
        q = next((x for x in pool if x["id"] == r["qid"]), {})
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
                " COALESCE(a.override, a.auto_correct) AS correct, a.rt_ms, a.first_ms, a.edits, a.ts"
                " FROM answers a JOIN participants p ON p.id=a.pid ORDER BY a.pid, a.id"
            ).fetchall()
            header = rows[0].keys() if rows else ["id"]
            return csv_response("answers.csv", header, [list(r) for r in rows])
        if what == "items":
            rows = build_items(con)
            header = list(rows[0].keys()) if rows else ["nickname"]
            return csv_response("items.csv", header, [[r[h] for h in header] for r in rows])
        simple = {
            "highlight_log": "SELECT h.id, p.nickname, p.grp, p.lang, h.round, h.text_id, h.sentence, h.action, h.method, h.t_ms, h.ts"
                             " FROM highlight_log h JOIN participants p ON p.id=h.pid ORDER BY h.pid, h.id",
            "steps": "SELECT s.id, p.nickname, p.grp, p.lang, p.excluded, s.stage, s.step_id, s.type, s.text_id, s.condition,"
                     " s.started, s.finished, s.elapsed_sec, s.hidden_n, s.away_sec, s.extra"
                     " FROM steps s JOIN participants p ON p.id=s.pid ORDER BY s.pid, s.id",
            "survey": "SELECT s.id, p.nickname, p.grp, p.lang, p.excluded, s.item, s.value, s.ts"
                      " FROM survey s JOIN participants p ON p.id=s.pid ORDER BY s.pid, s.id",
            "predictions": "SELECT r.id, p.nickname, p.grp, p.lang, p.excluded, r.text_id, r.condition, r.predicted, r.max, r.ts"
                           " FROM predictions r JOIN participants p ON p.id=r.pid ORDER BY r.pid, r.id",
        }
        if what in simple:
            rows = con.execute(simple[what]).fetchall()
            header = rows[0].keys() if rows else ["id"]
            return csv_response(f"{what}.csv", header, [list(r) for r in rows])
        if what == "highlights":
            rows = con.execute(
                "SELECT h.id, p.nickname, p.grp, p.lang, h.round, h.text_id, h.sentences, h.ts"
                " FROM highlights h JOIN participants p ON p.id=h.pid ORDER BY h.pid, h.id"
            ).fetchall()
            header = rows[0].keys() if rows else ["id"]
            return csv_response("highlights.csv", header, [list(r) for r in rows])
        if what == "events":
            rows = con.execute(
                "SELECT e.id, p.nickname, e.stage, e.step, e.type, e.ts, e.detail FROM events e JOIN participants p ON p.id=e.pid ORDER BY e.id"
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
