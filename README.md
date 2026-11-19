# Memory Study Platform

A small web app for running a 3-session memory experiment with real participants. It was built for a university team presentation, *"Remembering as Learning: Why Does Trying to Remember Strengthen Memory More Than Reviewing Notes?"*, in the Introduction to Neuroscience / Science of Learning course (Prof. Sungshin Kim, Hanyang University).

Participants use it on iPad, phone or laptop, in **Russian, English or Korean**. The researcher uses an admin page to track people, re-mark answers and export CSV data.

---

## 1. What the experiment tests

**Question:** does a study routine built from memory research beat the way most students actually study?

| | Usual way ("highlight") | Research routine ("test") |
|---|---|---|
| What the participant does | Highlights the text and rereads it, crammed into one day | Self-tests from memory, rates confidence (1–5), then checks the correct answers; repeated on two days |
| When practice happens | Session 2 only (8 min) | Session 1 (4 min) + Session 2 (4 min) |
| Research behind it | Students mostly reread and highlight (Karpicke, Butler & Roediger 2009); highlighting helps memory only a little (Ponce, Mayer & Méndez 2022) | Testing effect (Roediger & Karpicke 2006; Yang et al. 2021), feedback after a confident error (Chen et al. 2025), spacing across days with sleep (Cepeda 2008; Zou et al. 2025; Yang et al. 2025) |

**Hypotheses**
- **H1 (memory):** final-test scores are higher for the text studied with the routine.
- **H2 (feeling vs reality):** participants' predictions and beliefs favour highlighting, even when their scores don't.
- **Bonus:** answers given with high confidence but wrong, then corrected by feedback, are remembered well later (the hypercorrection effect).

**What it can and cannot show:** it compares the *whole routine* against the usual way. It can't say which part (testing, spacing or confidence) caused a difference; that would need many more groups.

### Design

- **Within-subject.** Every participant studies two texts, T1 and T2, one with each method.
- **Counterbalanced.** The app assigns group A or B at sign-up, balancing group sizes:

| Group | T1 | T2 |
|---|---|---|
| A | highlight | test (routine) |
| B | test (routine) | highlight |

- **Equal practice time.** Each text gets 8 minutes of practice, so the routine doesn't win just by getting more time.
- **Same retention interval.** Both texts get their last practice in Session 2, and the final test is 7 days after it.

### Session flow

| Session | Unlocks | Steps (in order) | Approx. time |
|---|---|---|---|
| 1 (`day0`) | At sign-up | `read1` read T1 (240 s) → `read2` read T2 (240 s) → `test` self-test on the routine text (max 180 s) → `feedback` correct answers (rest of a 240 s block, min 60 s) | ~15 min |
| 2 (`day2`) | 48 h after Session 1 ends | `hl1` highlight + reread the usual text (240 s) → `hl2` reread highlights, can edit (240 s) → `test` self-test (routine text) → `feedback` → `predict` predicted final score per text + "which method works better?" | ~15 min |
| 3 (`day9`) | 7 days after Session 2 ends | `final1` final test on T1 → `final2` final test on T2 → results page with own scores + debrief | ~5 min |

Rules the app enforces:
- Timers on reading, highlighting and feedback steps can't be skipped. The server rejects early "continue".
- The text is never sent to the browser during `test` or `final` steps.
- Steps can't be skipped or reordered (`step mismatch` → reload).
- Leaving the page during a session (`visibilitychange`) is logged as a "tab switch" for the researcher.
- Reloading resumes the same step, and the timer continues from server time.
- Typed answers, confidence ratings and highlights are saved on the device as the participant works, so a reload or a Safari tab discard doesn't lose them. If saving a step fails (bad connection), the app retries and then shows a "Try again" banner. Nothing is lost.
- The prediction sliders must be moved before continuing. An untouched slider is not recorded as a prediction of 0.
- The waiting screen has an "Add a reminder to my calendar" button (`.ics`, opens straight into Calendar on iPhone/iPad).

---

## 2. Project structure

```
memory-study/
├── app/
│   ├── main.py            # FastAPI server: design logic, session state machine, scoring, admin API, CSV export
│   └── static/
│       ├── index.html     # participant page shell
│       ├── app.js         # participant single-page app (vanilla JS, no build step)
│       ├── i18n.js        # all participant UI strings in en / ru / ko
│       ├── style.css      # shared styles (iPad-first, light + dark mode)
│       ├── admin.html     # admin page shell
│       └── admin.js       # admin dashboard: table, paired-score chart, answer grading, exports
├── content/
│   └── content.json       # the two texts + practice and final questions in 3 languages (SAMPLE content now)
├── tests/
│   ├── test_api.py        # pytest: every study rule with the REAL timings (fake clock), ~1 s
│   └── ui_walk.py         # Playwright: clicks through all 3 sessions (iPad or phone size), saves screenshots
├── deploy/
│   ├── setup-vps.sh       # one-shot server setup: remove old project, .env, build, Funnel, backups
│   ├── backup.sh          # consistent DB copy to data/backups (nightly via cron, keeps 30 days)
│   └── reset-data.sh      # start empty after the pilot (old DB is moved to backups, not deleted)
├── data/                  # created at runtime: study.db (SQLite) + backups/. Not in git.
├── Dockerfile             # with HEALTHCHECK
├── docker-compose.yml     # binds to 127.0.0.1:8090 only, mounts data/ and content/ (read-only)
├── requirements.txt       # runtime; requirements-dev.txt adds pytest, httpx, playwright
└── .env.example           # admin key + timings (real and pilot values)
```

**Stack:** Python 3.12 + FastAPI + SQLite (one file, WAL mode). The frontend is plain HTML/CSS/JS served by FastAPI, with no framework and no build step. Docker + Tailscale Funnel for deployment.

### Key parts of `app/main.py`

| Function / section | What it does |
|---|---|
| `load_content()` | Reads `content.json` and splits each body into sentences (`{s, p}`, `p` = paragraph index). Sentences are the highlight units. |
| `conditions(grp)` / `text_for()` | Maps group A/B to which text gets which condition. |
| `plan(p, stage)` | Returns the ordered step list for `day0` / `day2` / `day9`. **The experiment design lives here.** |
| `current_stage(p, now)` | State machine: `day0` → `wait` → `day2` → `wait` → `day9` → `done`, using `DAY2_GAP` / `DAY9_GAP`. |
| `step_payload()` | Builds what the browser needs for the current step (text only for read/highlight). |
| `auto_score()` / `key_matches()` | Keyword marking: answer is correct if, after `norm()` (NFKC, lowercase, ё→е, strip non-word chars), it contains any `accept` keyword. Number keywords must match a whole number (`8` matches "8 legs", not "18" or "2008"). |
| `validate_content()` | Checks `content.json` (missing translations/keywords, final questions without a matching practice `fact`, one-letter keywords). Warnings appear in the logs and at the top of the admin page. |
| `/api/complete` | Validates timer + step id, stores data, advances the step, stamps `dayX_done` at the end of a session. |
| `participant_summary()` | One row per participant for the admin page and `participants.csv`. |

### Data model (SQLite, `data/study.db`)

| Table | Contents |
|---|---|
| `participants` | nickname (unique, case-insensitive), PIN hash, token, lang, group (`A`/`B`), `day0_done` / `day2_done` / `day9_done` timestamps, current `stage` / `step` / `step_started`, `test_used` (seconds used in last self-test), `belief`, `excluded` |
| `answers` | One row per answer: `stage`, `kind` (`practice`/`final`), `text_id`, `condition` (`test`/`highlight`), `qid`, `fact`, `answer`, `confidence` (1–5, practice only), `auto_correct`, `override` (admin mark or NULL), `rt_ms`, `ts` |
| `highlights` | Per highlight round: `round` (1/2), `text_id`, `sentences` (JSON list of sentence indices) |
| `predictions` | Predicted number correct per text/condition, out of `max` |
| `events` | `hidden` events (tab/app switches) with stage + step, and `start` events (when each session was opened) |

Effective correctness = `override` if set, else `auto_correct`.

### API

Participant endpoints authenticate with the header `X-Token: <token>`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/meta` | Available languages, server time |
| POST | `/api/join` | `{nickname, pin, lang, consent}` → `{token}`. Same nickname + PIN resumes. |
| POST | `/api/login` | `{nickname, pin}` → `{token}`. After 8 wrong PINs for a nickname: `429` for 15 min. |
| GET | `/api/state` | Current stage + step payload, or `wait` (`unlock_at`), or `done` (scores) |
| POST | `/api/complete` | `{step_id, data}`. Data per step type: highlight `{selected:[i]}`, test `{answers:[{qid, answer, confidence, rt_ms}]}`, final `{answers:[{qid, answer, rt_ms}]}`, predict `{pred:{T1:n, T2:n}, belief}` |
| POST | `/api/event` | `{type:"hidden", step_id}` |
| GET | `/reminder.ics?at=<unix>&n=2\|3&lang=` | Calendar event for the next session (no personal data) |

Admin endpoints authenticate with the header `X-Admin-Key: <ADMIN_KEY>` (the admin page downloads files with this header, so the key never appears in URLs). If `ADMIN_KEY` is unset, a placeholder or shorter than 12 characters, all admin endpoints return `503`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/admin/overview` | Participants summary + timing config |
| GET | `/api/admin/answers` | All final answers with question, key, auto mark, override |
| POST | `/api/admin/grade` | `{id, override: 1 / 0 / null}` |
| POST | `/api/admin/exclude` | `{id, excluded: true/false}` |
| POST | `/api/admin/reload-content` | Re-read `content.json` without restarting; returns validation warnings |
| GET | `/api/admin/content-check` | Validation warnings for `content.json` |
| GET | `/api/admin/export/{participants,answers,highlights,events}.csv` | UTF-8 CSV with BOM (opens correctly in Excel) |
| GET | `/api/admin/backup.db` | Consistent copy of the whole SQLite database |

Pages: `/` (participants), `/admin` (researcher), `/healthz`.

### Content format (`content/content.json`)

```json
{
  "texts": {
    "T1": {
      "title":    {"en": "...", "ru": "...", "ko": "..."},
      "body":     {"en": "Paragraph one.\n\nParagraph two.", "ru": "...", "ko": "..."},
      "practice": [{"id": "p1", "fact": "legs", "q": {...}, "answer": {...}, "accept": {"en": ["8", "eight"], ...}}],
      "final":    [{"id": "f1", "fact": "legs", "q": {...}, "answer": {...}, "accept": {...}}]
    },
    "T2": { ... }
  }
}
```

- `fact` links a final question to the practice question about the same fact (used for the hypercorrection analysis). Final questions should be **reworded**, not copied from practice.
- `accept` = keywords. Use stems for Russian (`бочон` matches бочонок/бочонка). Avoid very short keywords that appear inside other words. Numbers are matched as whole numbers. After editing, check the warnings at the top of the admin page (or run `pytest -q`).
- A language is offered at sign-up only if **both** texts have a body in it.
- Recommended: two texts of similar length (~250 words) and difficulty, 10–15 final questions each.
- **Current content is SAMPLE** (water bears, bee dance; 4 practice + 4 final questions per text) for testing only.

### Configuration (environment variables)

| Variable | Real value | Meaning |
|---|---|---|
| `ADMIN_KEY` | long random string (12+ chars) | Admin password. `setup-vps.sh` generates one. |
| `READ_SEC` | 240 | Session 1 reading time per text |
| `HIGHLIGHT_SEC` | 240 | Each highlight round in Session 2 (2 rounds) |
| `TEST_MAX_SEC` | 180 | Self-test answering limit (auto-submits) |
| `BLOCK_SEC` | 240 | Self-test + feedback block total |
| `FEEDBACK_MIN_SEC` | 60 | Minimum feedback time |
| `DAY2_GAP_SEC` | 172800 | 48 h between Session 1 and 2 |
| `DAY9_GAP_SEC` | 604800 | 7 days between Session 2 and 3 |
| `LOGIN_MAX_FAILS`, `LOGIN_LOCK_SEC` | 8, 900 | PIN-guessing guard per nickname |
| `DATA_DIR`, `CONTENT_FILE` | set in Dockerfile | Paths for the database and content |

---

## 3. Run locally

```bash
pip install -r requirements-dev.txt
mkdir -p data
DATA_DIR=./data ADMIN_KEY=local-test-key-123 \
READ_SEC=3 HIGHLIGHT_SEC=3 TEST_MAX_SEC=8 BLOCK_SEC=10 FEEDBACK_MIN_SEC=2 \
DAY2_GAP_SEC=5 DAY9_GAP_SEC=5 \
uvicorn app.main:app --port 8077
```

- Participant page: http://localhost:8077, admin page: http://localhost:8077/admin (key `local-test-key-123`).
- With the fast timings above, a whole run takes about a minute.

### Tests

```bash
pytest -q                                   # API rules with the real timings, ~1 s, no server needed
playwright install chromium                 # once
python tests/ui_walk.py shots ru tester_ru  # needs the server above; also en / ko
VIEWPORT=phone python tests/ui_walk.py shots ko tester_ko2
```

`test_api.py` (30 tests) covers sign-up/login and PIN lockout, group balancing, timers and step order with the real 240 s / 48 h / 7 d values, the text being hidden during tests, duplicate submits, the feedback time rule, a full 3-session run with predictions, scores, admin re-marking and CSV exports, keyword marking (numbers, Russian stems, ё, Korean, full-width digits), admin auth, backups, content validation and the calendar file.

`ui_walk.py` clicks through all three sessions in a real browser and saves a screenshot of each screen. It also checks that a reload in the middle of the self-test keeps answers and confidence ratings, that untouched prediction sliders are refused, and that the calendar button is shown. It exits non-zero on any JavaScript error. It assumes the sample content (4 + 4 questions).

### Verified
- All 30 API tests pass.
- Browser walkthrough with no JavaScript errors in **en, ru and ko**, at **iPad (820×1180) and phone (390×844)** size. Russian confidence labels were shortened so they fit at 360 px width ("Наугад / Не очень / 50 на 50 / Скорее да / Точно знаю"). Korean text breaks between words (`word-break: keep-all`).
- Docker image builds, the container becomes `healthy`, and the browser walkthrough passes against the container.
- `deploy/setup-vps.sh` tested in a sandbox with a fake old `memory-service` compose project: removes it and its volume, creates `.env`, starts the app, installs the backup cron. A second run changes nothing. `reset-data.sh` moves the DB into backups.

### Not yet verified
- Tailscale Funnel on the real server (the sandbox has no Tailscale).
- Real iPad/iPhone Safari and Android Chrome: tapping sentences, keyboard covering inputs, sliders, the calendar button.

---

## 4. Deploy (VPS + Tailscale Funnel)

Requirements on the server: Docker with the compose plugin, Tailscale logged in to your tailnet.

**1. Get the code onto the server**

```bash
ssh root@<server>
git clone -b claude/platform-vps-deployment-fszjdw https://github.com/pak0vskiy/higgsfield_ai_challenge.git /opt/memory-study
# private repo: use a GitHub token as password, or copy it from your laptop instead:
#   scp -r ./higgsfield_ai_challenge root@<server>:/opt/memory-study
```

**2. Run the setup script**

```bash
cd /opt/memory-study
sudo bash deploy/setup-vps.sh
```

It asks before each destructive step:
1. Shows running containers and the Tailscale serve config.
2. Finds the old **memory-service** side project (by its Docker Compose labels) and offers to remove its containers, data volume and image. If the old project runs some other way (systemd, pm2, …), stop it by hand.
3. Creates `.env` with a random 32-character `ADMIN_KEY` (kept on later runs).
4. Builds and starts the app on `127.0.0.1:8090` (**not** reachable on the public IP) and waits for `/healthz`.
5. Publishes it with Tailscale Funnel: port 443 if free; if 443 is used by the old project it offers to reset that; otherwise it uses `:8443`.
6. Installs a nightly backup (`/etc/cron.d/memory-study-backup`, 04:17, keeps 30 days in `data/backups/`).

At the end it prints the admin key and `tailscale funnel status` shows the public URL (`https://<machine>.<tailnet>.ts.net`). The first Funnel run may print a link to enable Funnel for the tailnet. Open it and run the script again. New DNS names can take up to 10 minutes. Test from a phone with Wi-Fi off.

**3. Pilot, then reset**

Put the pilot timings from `.env.example` into `.env`, `docker compose up -d`, run all three sessions yourself (ideally on an iPad and a phone), then switch back to the real timings and:

```bash
sudo bash deploy/reset-data.sh     # warns if timings aren't the real ones; old DB is moved to data/backups/
```

**Updating the code later:** `git pull && docker compose up -d --build`. Participants keep their progress; drafts in their browsers survive.

**Backups:** automatic nightly, or `bash deploy/backup.sh` any time, or "Download database backup" on the admin page. CSVs are on the admin page too.

**Logs:** `docker compose logs -f`. Content warnings and a disabled admin key are logged at startup.

---

## 5. Running the study

- **Target sample:** about 20–25 people finishing all 3 sessions, so recruit about 30–35 (expect around a third to drop out). With a paired design, that detects a medium-to-large difference (dz ≈ 0.6–0.8, α = .05, power = .80).
- **Reminders:** yellow rows on the admin page = next session open but not started. Send these people a reminder on KakaoTalk.
- **Exclusions:** exclude people with many tab switches during tests, sessions that took far too long ("Session min" column), or who say they reviewed the texts. They stay in the CSVs with `excluded=1`.
- **Never edit `content.json` after real participants have started.**

### Analysis starter (`answers.csv`)

```python
import pandas as pd
from scipy import stats

a = pd.read_csv("answers.csv")
f = a[(a.kind == "final") & (a.excluded == 0)]
per = f.pivot_table(index="nickname", columns="condition", values="correct", aggfunc="mean").dropna() * 100
diff = per["test"] - per["highlight"]
print(per.describe())
print("routine better for", (diff > 0).sum(), "of", len(diff))
print(stats.ttest_rel(per["test"], per["highlight"]))
print(stats.wilcoxon(per["test"], per["highlight"]))

# Hypercorrection: Session 1 practice errors made with high confidence (4-5), then corrected by feedback
p = a[(a.kind == "practice") & (a.stage == "day0")][["nickname", "text_id", "fact", "confidence", "auto_correct"]]
m = p.merge(f[["nickname", "text_id", "fact", "correct"]], on=["nickname", "text_id", "fact"])
m["group"] = m.apply(lambda r: "correct" if r.auto_correct else ("high-conf error" if r.confidence >= 4 else "low-conf error"), axis=1)
print(m.groupby("group")["correct"].mean())
```

`participants.csv` holds predictions (`pred_routine`, `pred_usual`), `belief`, gaps between sessions (`gap_day0_day2_h` is measured from the end of Session 1 to the end of Session 2), tab switches, and minutes each session took (`s1_min`, `s2_min`, `s3_min`; much longer than ~15/15/5 means the session was interrupted). `highlights.csv` lists which sentence indices were highlighted. Use it to check whether highlighted facts were remembered better.

---

## 6. Open items / ideas

- [ ] Replace sample content with the real texts in en / ru / ko (equivalent translations, similar difficulty). Check the admin page for content warnings afterwards.
- [ ] Run `deploy/setup-vps.sh` on the VPS, check Funnel, then pilot with 1–2 people on real devices.
- [ ] Test on a real iPad and a phone (Safari): sentence tapping, keyboard, sliders, dark mode, calendar button.
- [ ] Optional: drag-to-highlight across several sentences (currently tap per sentence, to avoid scroll conflicts on touch).
- [ ] Optional: per-language breakdown on the admin page.

## Privacy

- Only a self-chosen nickname is stored, no real names or emails. PINs are stored as salted SHA-256 hashes, and PIN guessing is rate-limited.
- The app sends `X-Frame-Options`, `nosniff` and `no-referrer` headers, and the auto-generated API docs are switched off.
- Participants agree to a consent statement and a no-notes pledge, and see a debrief with their results at the end.
- The admin page is protected by `ADMIN_KEY` and stays disabled until a real key is set. Keep `.env` and `data/` out of git (both are in `.gitignore`).
