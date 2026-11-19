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
| 1 (`day0`) | At sign-up | `intro` how the platform works → `read1` read T1 (240 s) → `read2` read T2 (240 s) → `test` self-test on the routine text (max 180 s) → `feedback` correct answers (rest of a 240 s block, min 60 s) | ~15 min |
| 2 (`day2`) | 48 h after Session 1 ends | `intro` how highlighting works, with a practice paragraph → `hl1` highlight + reread the usual text (240 s) → `hl2` reread highlights, can edit (240 s) → `test` self-test (routine text) → `feedback` → `predict` predicted final score per text + "which method works better?" | ~15 min |
| 3 (`day9`) | 7 days after Session 2 ends | `intro` → `final1` final test on T1 → `final2` final test on T2 (answer + confidence 1–5 per question) → `survey` short questionnaire → results page with own scores + debrief | ~7–10 min |

Every session starts with an untimed instructions screen (`intro`): Session 1 explains the platform (timers, Continue unlocks when time is up, no going back, text hidden in self-tests, confidence scale, stay on the page, reloads are safe, no notes, nickname + PIN, calendar reminder); Session 2 explains highlighting and lets participants try it on a practice paragraph that isn't saved; Session 3 explains the final test. The durations shown come from the server config, so they're always right. Time spent on each instructions screen is in `steps.csv`.

**Highlighting** marks whole sentences (the unit used to link facts to highlights):
- finger: tap a sentence to highlight it, tap again to remove; swiping scrolls as usual
- Apple Pencil or mouse: press and drag over the text to highlight every sentence passed over; a drag that starts on a highlighted sentence erases instead (Pencil strokes don't scroll the page)
- keyboard: Enter/Space on a focused sentence

Every change is logged with its method (`touch` / `pen` / `mouse` / `key`).

Rules the app enforces:
- Timers on reading, highlighting and feedback steps can't be skipped. The server rejects early "continue".
- The text is never sent to the browser during `test` or `final` steps.
- Steps can't be skipped or reordered (`step mismatch` → reload).
- Leaving the page during a session (`visibilitychange`) is logged as a "tab switch" for the researcher.
- Reloading resumes the same step, and the timer continues from server time.
- Typed answers, confidence ratings and highlights are saved on the device as the participant works, so a reload or a Safari tab discard doesn't lose them. If saving a step fails (bad connection), the app retries and then shows a "Try again" banner. Nothing is lost.
- The prediction sliders must be moved before continuing. An untouched slider is not recorded as a prediction of 0.
- The waiting screen has an "Add a reminder to my calendar" button (`.ics`, opens straight into Calendar on iPhone/iPad).

### What is collected (and what it's for)

| Question | Data | Where |
|---|---|---|
| H1: does the routine beat highlighting? | Final answers per text/condition, auto-marked + your overrides | `items.csv`, `answers.csv` |
| H2: do people believe highlighting works better? | Predicted score per text (end of Session 2); "which method works better?" asked at the end of Session 2 **and again after the final test**; "which felt harder?" | `items.csv` (`predicted_for_text`), `participants.csv` (`belief`, `survey_belief_after`, `survey_harder`), `predictions.csv` |
| Does highlighting a fact help remember it? | Which sentences were highlighted (each round, plus every tap with time), and which sentence holds each fact → per final question `highlighted_r1` / `highlighted_r2` | `items.csv`, `highlights.csv`, `highlight_log.csv` |
| Confident errors (hypercorrection) | Session 1 and 2 self-test answers with confidence, linked to the same fact in the final test → `s1_error_type` = correct / high-conf error / low-conf error | `items.csv` |
| Confidence vs. accuracy (calibration, feeling of knowing) | Confidence 1–5 on every final answer, for **both** methods | `items.csv` (`final_confidence`), `participants.csv` (`final_conf_routine`, `final_conf_usual`) |
| Data quality / exclusions | Time away from the page per step, tab switches, scroll depth on reading, auto-submitted self-tests, minutes per session, gaps between sessions, "did you review the texts / take notes?", device | `steps.csv`, `participants.csv`, `survey.csv`, `events.csv` |
| Covariates | Prior knowledge of each topic (1–5), usual study habit, language, group, which text was read first, answer timing (first keystroke, last edit, number of edits) | `items.csv`, `participants.csv` |

**Questionnaire items** (Session 3, after the final test, before results): `belief_after` (highlight/test/same), `harder` (highlight/test/same), `prior_T1`, `prior_T2` (1–5), `reviewed` (no/little/yes), `notes` (no/yes), `habit` (reread/highlight/selftest/notes/other). Defined in `SURVEY` in `app/main.py`, labels in `i18n.js`.

**`items.csv`** is the file to analyse: one row per participant × final question, with the final answer, correctness, confidence and timing; the Session 1/2 self-test answer, correctness and confidence for the same fact; whether the fact's sentence was highlighted; the prediction for that text; prior knowledge; and the belief. The admin page's **Insights** section shows the headline numbers live: accuracy and confidence per method, prediction vs. reality, beliefs before/after, highlighted vs. non-highlighted facts, confident errors, and calibration.

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
| `answers` | One row per answer: `stage`, `kind` (`practice`/`final`), `text_id`, `condition` (`test`/`highlight`), `qid`, `fact`, `answer`, `confidence` (1–5, practice **and final**), `auto_correct`, `override` (admin mark or NULL), `rt_ms` (last edit), `first_ms` (first keystroke), `edits` (keystrokes), `ts` |
| `highlights` | Per highlight round: `round` (1/2), `text_id`, `sentences` (JSON list of sentence indices at the end of the round) |
| `highlight_log` | Every highlight change: `round`, `text_id`, `sentence`, `action` (`on`/`off`), `method` (`touch`/`pen`/`mouse`/`key`), `t_ms` since the round started |
| `predictions` | Predicted number correct per text/condition, out of `max` |
| `steps` | One row per completed step: server `started`/`finished`/`elapsed_sec`, `hidden_n` + `away_sec` (time the page was in the background), `extra` (`scroll_max` = how far down the text they scrolled, 0–1; `auto_submit` = self-test ran out of time) |
| `survey` | End-of-study questionnaire, one row per item (see below) |
| `events` | `hidden` / `visible` (tab/app switches), `start` (session opened), `device` (`detail` JSON: phone/tablet/desktop, screen size, time zone, browser) |

Effective correctness = `override` if set, else `auto_correct`.

### API

Participant endpoints authenticate with the header `X-Token: <token>`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/meta` | Available languages, server time |
| POST | `/api/join` | `{nickname, pin, lang, consent}` → `{token}`. Same nickname + PIN resumes. |
| POST | `/api/login` | `{nickname, pin}` → `{token}`. After 8 wrong PINs for a nickname: `429` for 15 min. |
| GET | `/api/state` | Current stage + step payload, or `wait` (`unlock_at`), or `done` (scores) |
| POST | `/api/complete` | `{step_id, data}`. Data per step type: highlight `{selected:[i]}`, test `{answers:[{qid, answer, confidence, rt_ms}]}`, final `{answers:[{qid, answer, confidence, rt_ms, first_ms, edits}]}`, predict `{pred:{T1:n, T2:n}, belief}`, survey `{answers:{item: value}}`; highlight also takes `log:[{i, a:"on"|"off", t, m:"touch"|"pen"|"mouse"|"key"}]`, every step takes `scroll_max` |
| POST | `/api/event` | `{type: "hidden"\|"visible"\|"device", step_id, detail?}` |
| GET | `/reminder.ics?at=<unix>&n=2\|3&lang=` | Calendar event for the next session (no personal data) |

Admin endpoints authenticate with the header `X-Admin-Key: <ADMIN_KEY>` (the admin page downloads files with this header, so the key never appears in URLs). If `ADMIN_KEY` is unset, a placeholder or shorter than 12 characters, all admin endpoints return `503`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/admin/overview` | Participants summary + timing config |
| GET | `/api/admin/answers` | All answers (practice + final) with question, key, auto mark, override |
| POST | `/api/admin/grade` | `{id, override: 1 / 0 / null}` |
| POST | `/api/admin/exclude` | `{id, excluded: true/false}` |
| POST | `/api/admin/reload-content` | Re-read `content.json` without restarting; returns validation warnings |
| GET | `/api/admin/content-check` | Validation warnings for `content.json` |
| GET | `/api/admin/export/{items,participants,answers,highlights,highlight_log,steps,survey,predictions,events}.csv` | UTF-8 CSV with BOM (opens correctly in Excel) |
| GET | `/api/admin/insights` | Headline analyses (finished, non-excluded participants) |
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
- `source` (optional) links a question to the sentence that contains the answer: `{"en": "a few words from that sentence", ...}` (or a list of snippets). Without it the sentence is found through the `accept` keywords. The admin page warns when a final question matches no sentence or several, so the "was this fact highlighted?" columns are reliable.
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

`test_api.py` (39 tests) covers sign-up/login and PIN lockout, group balancing, timers and step order with the real 240 s / 48 h / 7 d values, the text being hidden during tests, duplicate submits, the feedback time rule, a full 3-session run with predictions, scores, admin re-marking and CSV exports, keyword marking (numbers, Russian stems, ё, Korean, full-width digits), admin auth, backups, content validation and the calendar file. It also checks every analysis column: `items.csv` links each final answer to its practice history and its highlighted sentence, the highlight tap log, per-step timing/time away/scroll depth/auto-submit, the questionnaire, device info, the Insights numbers, re-marking practice answers, and upgrading a database created by the older version.

`ui_walk.py` clicks through all three sessions in a real browser and saves a screenshot of each screen. It also goes through the instructions screen of each session, highlights by tapping and by dragging with the mouse (on the practice paragraph and the real text), checks that a reload in the middle of the self-test keeps answers and confidence ratings, that untouched prediction sliders are refused, that the questionnaire can't be skipped, and that the calendar button is shown. It exits non-zero on any JavaScript error. It assumes the sample content (4 + 4 questions).

### Verified
- All 39 API tests pass.
- Highlighting with a simulated Apple Pencil (`pointerType: pen` via Chrome DevTools): drag paints, a drag starting on a highlight erases, a finger tap toggles exactly once, and each change is logged with its method. Real iPad + Pencil still to be tried.
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
git clone https://github.com/pak0vskiy/Memory_experiment.git /opt/memory-study
# private repo: use a GitHub token as password, or copy it from your laptop instead:
#   scp -r ./Memory_experiment root@<server>:/opt/memory-study
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

**Updating the code later:** `cd /opt/memory-study && git pull && docker compose up -d --build`. Participants keep their progress; drafts in their browsers survive. Don't change the session steps (`plan()`) while real participants are mid-session.

**Backups:** automatic nightly, or `bash deploy/backup.sh` any time, or "Download database backup" on the admin page. CSVs are on the admin page too.

**Logs:** `docker compose logs -f`. Content warnings and a disabled admin key are logged at startup.

---

## 5. Running the study

- **Target sample:** about 20–25 people finishing all 3 sessions, so recruit about 30–35 (expect around a third to drop out). With a paired design, that detects a medium-to-large difference (dz ≈ 0.6–0.8, α = .05, power = .80).
- **Reminders:** yellow rows on the admin page = next session open but not started. Send these people a reminder on KakaoTalk.
- **Exclusions:** decide the rules before looking at results. Candidates: `survey_reviewed` = yes, `survey_notes` = yes, a lot of `away_sec` during tests, sessions that took far too long ("Session min" column), or a big deviation from the 48 h / 7 d gaps. Excluded people stay in the CSVs with `excluded=1`. They stay in the CSVs with `excluded=1`.
- **Never edit `content.json` after real participants have started.**

### Analysis starter (`items.csv`)

```python
import pandas as pd
from scipy import stats

it = pd.read_csv("items.csv")
it = it[it.excluded == 0]

# H1: % correct per person and method, paired test
per = it.pivot_table(index="nickname", columns="condition", values="final_correct", aggfunc="mean").dropna() * 100
diff = per["test"] - per["highlight"]
print(per.describe())
print("routine better for", (diff > 0).sum(), "of", len(diff))
print(stats.ttest_rel(per["test"], per["highlight"]))
print(stats.wilcoxon(per["test"], per["highlight"]))
print("Cohen's dz:", diff.mean() / diff.std())

# H2: predicted vs actual (% of the text's questions), per method
pp = it.groupby(["nickname", "condition"]).agg(pred=("predicted_for_text", "first"), mx=("text_max", "first"),
                                               actual=("final_correct", "mean")).reset_index()
pp["pred_pct"] = 100 * pp.pred / pp.mx
pp["actual_pct"] = 100 * pp.actual
print(pp.groupby("condition")[["pred_pct", "actual_pct"]].mean())

# Did highlighting a fact help? (highlight text only; highlighted_r2 = sentence highlighted at the end of round 2)
hl = it[it.condition == "highlight"]
print(hl.groupby("highlighted_r2")["final_correct"].agg(["mean", "count"]))

# Hypercorrection: Session 1 self-test errors made with high confidence (4-5), corrected by feedback
print(it[it.condition == "test"].groupby("s1_error_type")["final_correct"].agg(["mean", "count"]))

# Calibration: accuracy by final-test confidence, per method; mean confidence per method
print(it.pivot_table(index="final_confidence", columns="condition", values="final_correct", aggfunc="mean"))
print(it.groupby("condition")["final_confidence"].mean())
```

`participants.csv` has one row per person: group, scores, predictions (`pred_routine`, `pred_usual`), `belief` (Session 2) and `survey_belief_after` (after the final test), the other questionnaire answers (`survey_*`), number of sentences highlighted (`hl_sentences_r1/r2`, `hl_share_r2`), mean final confidence per method, gaps between sessions (`gap_day0_day2_h` is measured from the end of Session 1 to the end of Session 2), minutes per session (`s1_min`, `s2_min`, `s3_min`; much longer than ~15/15/8 means it was interrupted), `away_sec` (total time the page was in the background), `tab_switches`, `tests_auto_submitted`, and device/time zone. `steps.csv` has the same timing per step, and `highlight_log.csv` has every highlight tap, e.g. for "how many sentences did people highlight, and when?".

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
