(() => {
  const $app = document.getElementById("app");
  const $timer = document.getElementById("timer");
  const $progress = document.getElementById("progress");
  const $brand = document.getElementById("brand");
  const $langsel = document.getElementById("langsel");
  let joinMode = "join";    // which form the sign-up page shows ("join" or "login")

  let lang = localStorage.getItem("ms_lang") || guessLang();
  let token = localStorage.getItem("ms_token");
  let tick = null;          // interval for the visible timer
  let current = null;       // current state from the server
  let clockOffset = 0;      // server_now - client_now (seconds)

  function guessLang() {
    const n = (navigator.language || "en").slice(0, 2);
    return ["en", "ru", "ko", "zh", "mn"].includes(n) ? n : "en";
  }

  function t(key, vars = {}) {
    const dict = I18N[lang] || I18N.en;
    let s = dict[key] ?? I18N.en[key] ?? key;
    if (typeof s === "string") s = s.replace(/\{(\w+)\}/g, (_, k) => (k in vars ? vars[k] : `{${k}}`));
    return s;
  }

  // Tiny DOM helper: h("div", {class: "x", onclick: fn}, "text", child, ...)
  function h(tag, attrs = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "class") el.className = v;
      else if (k === "html") el.innerHTML = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  async function api(path, body) {
    const opts = { headers: { "Content-Type": "application/json" } };
    if (token) opts.headers["X-Token"] = token;
    if (body !== undefined) { opts.method = "POST"; opts.body = JSON.stringify(body); }
    let res;
    try { res = await fetch(path, opts); } catch (e) { throw { status: 0, detail: t("errNet") }; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw { status: res.status, detail: data.detail || res.statusText };
    return data;
  }

  function setLang(l) {
    lang = l;
    localStorage.setItem("ms_lang", l);
    document.documentElement.lang = l;
    $brand.textContent = t("brand");
    document.title = t("brand");
    updateLangSelect();
  }

  // ------------------------------------------------------------ language switching
  // The language can be changed on the sign-up/login page and, after signing up, until the first
  // text is shown (the server decides: state.lang_locked). Afterwards the menu is hidden, because
  // texts and questions have to stay in one language for the results to be comparable.
  const canChangeLang = () => !token || (current && current.lang_locked === false);
  function updateLangSelect() {
    const langs = window.__langs || ["en", "ru", "ko", "zh", "mn"];
    if ($langsel.options.length !== langs.length) {
      $langsel.replaceChildren(...langs.map(l => h("option", { value: l }, I18N[l] ? I18N[l].langName : l)));
    }
    $langsel.value = lang;
    $langsel.classList.toggle("hidden", !canChangeLang());
  }
  async function changeLang(l) {
    if (l === lang) return;
    if (!token) { setLang(l); renderJoin(joinMode); return; }
    try { await api("/api/lang", { lang: l }); }
    catch (e) { /* locked meanwhile or offline: the refresh below shows the true state */ }
    refresh();
  }
  $langsel.addEventListener("change", () => changeLang($langsel.value));

  function stopTimer() {
    if (tick) clearInterval(tick);
    tick = null;
    $timer.classList.add("hidden");
  }

  function fmt(sec) {
    sec = Math.max(0, Math.ceil(sec));
    const m = Math.floor(sec / 60), s = sec % 60;
    return `${m}:${String(s).padStart(2, "0")}`;
  }

  // Runs a countdown to `seconds` from now; calls onEnd once at zero.
  function startTimer(seconds, onEnd) {
    stopTimer();
    const end = Date.now() + seconds * 1000;
    $timer.classList.remove("hidden");
    let fired = false;
    const update = () => {
      const left = (end - Date.now()) / 1000;
      $timer.textContent = `${t("timeLeft")} ${fmt(left)}`;
      $timer.classList.toggle("low", left <= 30);
      if (left <= 0 && !fired) { fired = true; stopTimer(); onEnd && onEnd(); }
    };
    update();
    tick = setInterval(update, 250);
  }

  // ------------------------------------------------------------ drafts
  // Work in progress (typed answers, confidence, highlights) is kept in localStorage so a reload,
  // a Safari tab discard or a failed save never loses it. Cleared once the server accepts the step.
  // The step's start time is part of the key, so if a session is cleared and redone (admin
  // "Redo session"), drafts typed in the earlier attempt aren't restored.
  const draftKey = (step) => `ms_draft:${token}:${current && current.stage}:${step.id}:${step.started || ""}`;
  function loadDraft(step) {
    try { return JSON.parse(localStorage.getItem(draftKey(step))) || null; } catch (e) { return null; }
  }
  function saveDraft(step, data) {
    try { localStorage.setItem(draftKey(step), JSON.stringify(data)); } catch (e) { /* storage full/blocked */ }
  }
  function clearDrafts() {
    try {
      for (const k of Object.keys(localStorage)) if (k.startsWith("ms_draft:")) localStorage.removeItem(k);
    } catch (e) { /* ignore */ }
  }
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  function msg(text, kind = "error") {
    return h("p", { class: `msg ${kind}`, role: kind === "error" ? "alert" : null }, text);
  }

  // ------------------------------------------------------------ boot
  async function boot() {
    setLang(lang);
    $app.replaceChildren(h("p", { class: "muted" }, t("loading")));
    let meta = { langs: ["en", "ru", "ko", "zh", "mn"] };
    try { meta = await api("/api/meta"); } catch (e) { /* keep defaults */ }
    window.__langs = meta.langs;
    if (token) return refresh();
    renderJoin("join");
  }

  async function refresh() {
    stopTimer();
    try {
      current = await api("/api/state");
    } catch (e) {
      if (e.status === 401) { logout(); return; }
      $app.replaceChildren(msg(e.detail || t("errNet")), h("button", { class: "btn", onclick: refresh }, "↻"));
      return;
    }
    clockOffset = current.server_now - Date.now() / 1000;
    setLang(current.lang);          // also shows/hides the language menu (current.lang_locked)
    const sessionNo = { day0: 1, day2: 2, day9: 3 }[current.stage];
    $progress.textContent = sessionNo ? `${t("session", { n: sessionNo })} · ${current.step_index + 1}/${current.step_count}` : "";
    document.body.classList.toggle("insession", !!sessionNo);
    window.scrollTo(0, 0);
    if (current.stage === "wait") return renderWait(current);
    if (current.stage === "done") return renderDone(current);
    const step = current.step;
    scrollMax = 0;
    sendDeviceOnce();
    ({ read: renderRead, highlight: renderHighlight, test: renderTest, feedback: renderFeedback,
       predict: renderPredict, final: renderFinal, survey: renderSurvey, intro: renderIntro })[step.type](step);
  }

  // ------------------------------------------------------------ behaviour logging
  // How far down the study text the participant scrolled (0-1), sent with each step.
  let scrollMax = 0;
  function trackScroll() {
    const art = document.querySelector("article.text");
    const vh = window.innerHeight;
    let f;
    if (art) {
      const r = art.getBoundingClientRect();
      f = r.height > 0 ? (vh - r.top) / r.height : 1;
    } else {
      const H = document.documentElement.scrollHeight;
      f = H > 0 ? (window.scrollY + vh) / H : 1;
    }
    scrollMax = Math.max(scrollMax, Math.min(1, Math.max(0, f)));
  }
  window.addEventListener("scroll", trackScroll, { passive: true });
  window.addEventListener("resize", trackScroll, { passive: true });

  // Device / time zone, once per page load (helps explain odd timings; no personal data).
  let deviceSent = false;
  function sendDeviceOnce() {
    if (deviceSent || !token) return;
    deviceSent = true;
    const ua = navigator.userAgent || "";
    const touch = (navigator.maxTouchPoints || 0) > 0;
    const kind = /iPad|Tablet/i.test(ua) || (/Macintosh/.test(ua) && touch) || (/Android/i.test(ua) && !/Mobile/i.test(ua))
      ? "tablet" : /Mobi|iPhone|Android/i.test(ua) ? "phone" : "desktop";
    let tz = "";
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { /* ignore */ }
    api("/api/event", { type: "device", step_id: current && current.step ? current.step.id : "", detail: {
      kind, ua: ua.slice(0, 300), sw: screen.width, sh: screen.height, vw: window.innerWidth, vh: window.innerHeight,
      dpr: window.devicePixelRatio, touch, tz, tz_off: -new Date().getTimezoneOffset(), nav_lang: navigator.language,
    } }).catch(() => {});
  }

  function logout() {
    clearDrafts();
    token = null;
    localStorage.removeItem("ms_token");
    stopTimer();
    $progress.textContent = "";
    document.body.classList.remove("insession");
    current = null;
    renderJoin("login");
  }

  // Sends a finished step. Network errors and server hiccups are retried a few times; if it
  // still fails, a banner with "Try again" appears (the draft stays in localStorage meanwhile).
  async function complete(step, data) {
    document.getElementById("savefail")?.remove();
    trackScroll();
    data = { ...data, scroll_max: Math.round(scrollMax * 1000) / 1000 };
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        await api("/api/complete", { step_id: step.id, data });
        try { localStorage.removeItem(draftKey(step)); } catch (e) { /* ignore */ }
        return refresh();
      } catch (e) {
        if (e.status === 401) return logout();
        // 409: the server is ahead of us (e.g. an earlier attempt did arrive) or the timer
        // isn't finished by server time. Either way, reload the true state.
        if (e.status === 409) return refresh();
        if (e.status >= 400 && e.status < 500) break;
        await sleep(800 * (attempt + 1));
      }
    }
    const bar = h("div", { class: "toast", id: "savefail", role: "alert" },
      h("span", {}, t("errSave")),
      h("button", { class: "btn", type: "button", onclick: () => complete(step, data) }, t("retry")));
    document.body.append(bar);
  }

  // Accidental reload / closing the tab during a test or highlighting: ask first (laptops; mobile
  // browsers mostly ignore this). Nothing is lost anyway: drafts are kept and the timer continues.
  window.addEventListener("beforeunload", (e) => {
    if (token && current && current.step && ["test", "final", "highlight", "survey"].includes(current.step.type)) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  // Log when the participant leaves the page during a session (tab/app switch) and comes back,
  // so the time spent away can be computed per step.
  document.addEventListener("visibilitychange", () => {
    if (token && current && current.step) {
      api("/api/event", { type: document.hidden ? "hidden" : "visible", step_id: current.step.id }).catch(() => {});
    }
  });

  // ------------------------------------------------------------ join / login
  function renderJoin(mode) {
    stopTimer();
    joinMode = mode;
    updateLangSelect();
    const langs = window.__langs || ["en", "ru", "ko", "zh", "mn"];
    const langSel = h("div", { class: "langs", role: "group", "aria-label": t("language") },
      langs.map(l => h("button", {
        class: "chip" + (l === lang ? " on" : ""), type: "button",
        onclick: () => changeLang(l),
      }, I18N[l].langName)));

    const nick = h("input", { id: "nick", autocomplete: "off", autocapitalize: "off", spellcheck: "false", maxlength: "30" });
    const pin = h("input", { id: "pin", inputmode: "numeric", pattern: "[0-9]*", maxlength: "4", autocomplete: "off" });
    const errBox = h("div");
    const form = h("form", { class: "card" });

    form.append(
      h("label", { for: "nick" }, t("nickname")), nick, h("small", {}, t("nicknameHint")),
      h("label", { for: "pin" }, t("pin")), pin, h("small", {}, t("pinHint")),
    );
    let consent, pledge;
    if (mode === "join") {
      consent = h("input", { type: "checkbox", id: "consent" });
      pledge = h("input", { type: "checkbox", id: "pledge" });
      form.append(
        h("label", { class: "check" }, consent, h("span", {}, t("consent"))),
        h("label", { class: "check" }, pledge, h("span", {}, t("pledge"))),
      );
    }
    form.append(errBox, h("button", { class: "btn primary", type: "submit" }, mode === "join" ? t("start") : t("login")));
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      errBox.replaceChildren();
      const nickname = nick.value.trim(), p = pin.value.trim();
      if (nickname.length < 2 || nickname.length > 30 || !/^\d{4}$/.test(p)) return errBox.append(msg(t("errNick")));
      try {
        let res;
        if (mode === "join") {
          if (!consent.checked || !pledge.checked) return errBox.append(msg(t("errConsent")));
          res = await api("/api/join", { nickname, pin: p, lang, consent: true });
        } else {
          res = await api("/api/login", { nickname, pin: p });
        }
        token = res.token;
        localStorage.setItem("ms_token", token);
        refresh();
      } catch (e) {
        errBox.append(msg(e.status === 409 ? t("errTaken") : e.status === 401 ? t("errLogin") : e.status === 429 ? t("errLocked")
          : (e.status === 0 ? t("errNet") : String(e.detail))));
      }
    });

    $app.replaceChildren(
      h("section", { class: "intro" },
        h("h1", {}, t("introTitle")),
        mode === "join" ? h("p", {}, t("introBody")) : null,
        h("p", { class: "label" }, t("language")), langSel),
      form,
      h("p", { class: "center" }, h("button", { class: "link", type: "button", onclick: () => renderJoin(mode === "join" ? "login" : "join") },
        mode === "join" ? t("haveAccount") : t("newAccount"))),
    );
  }

  // ------------------------------------------------------------ steps
  function header(title, hint) {
    return [h("h1", {}, title), h("p", { class: "hint" }, hint)];
  }

  // Highlight units for text without server-made tokens (the practice paragraph): words, or for
  // Chinese single characters with digit/Latin runs kept together (same rule as the server).
  const tokenize = (x) => (lang === "zh" ? x.match(/[0-9A-Za-z]+|\S/g) : x.split(/\s+/).filter(Boolean)) || [];

  // The study text. If `selectable`, words can be highlighted like with a highlighter pen:
  //  - Apple Pencil / mouse: press and drag; every word from where the stroke started to the word
  //    under the pen (in reading order) is highlighted, whatever path the pen takes. Moving back
  //    un-does words that leave the range. A stroke that starts on a highlighted word erases.
  //  - finger: tap a word to toggle it; press and hold (~0.35 s), then drag to mark a range;
  //    a quick swipe scrolls the page as usual
  //  - keyboard: Enter/Space on a focused sentence toggles the whole sentence
  // onChange(selectedWordIndices, change) is called after every tap/stroke, with
  // change = {a: "on"|"off", f: firstWord, to: lastWord, m: "touch"|"pen"|"mouse"|"key"}.
  function textBlock(step, selectable, selected, onChange) {
    const sel = new Set(selected || []);
    const paras = [], words = [], gaps = [], sentWords = [];
    const zh = lang === "zh";
    step.sentences.forEach((s, si) => {
      if (!paras[s.p]) paras[s.p] = h("p", {});
      if (!selectable) {
        paras[s.p].append(h("span", { class: "sent" }, s.s), /[。！？]$/.test(s.s) || zh ? "" : " ");
        return;
      }
      const sent = h("span", { class: "sent", "data-s": si, tabindex: "0", role: "group" });
      sentWords[si] = [];
      (s.w || tokenize(s.s)).forEach((tok, j) => {
        const k = words.length;
        if (j > 0 && !zh) { const g = h("span", { class: "gap" }, " "); gaps[k - 1] = g; sent.append(g); }
        const w = h("span", { class: "w" + (sel.has(k) ? " hl" : ""), "data-k": k }, tok);
        words.push(w); sentWords[si].push(k); sent.append(w);
      });
      // the space between the previous sentence (same paragraph) and this one
      const prev = step.sentences[si - 1];
      if (prev && prev.p === s.p && sentWords[si].length && !zh && !/[。！？]$/.test(prev.s)) {
        const g = h("span", { class: "gap" }, " ");
        gaps[sentWords[si][0] - 1] = g;
        paras[s.p].append(g);
      }
      paras[s.p].append(sent);
    });
    const article = h("article", { class: "text" + (selectable ? " selectable" : "") }, paras.filter(Boolean));
    if (!selectable) return { el: article, sel };

    // gaps between two highlighted words are highlighted too, so a phrase looks like one stroke
    const paintGap = (k) => { if (gaps[k]) gaps[k].classList.toggle("hl", sel.has(k) && sel.has(k + 1)); };
    const set = (k, on) => {
      if (sel.has(k) === on) return;
      if (on) sel.add(k); else sel.delete(k);
      words[k].classList.toggle("hl", on);
      paintGap(k - 1); paintGap(k);
    };
    gaps.forEach((_, k) => paintGap(k));
    const wordAt = (x, y) => {
      const el = document.elementFromPoint(x, y);
      if (!el || !article.contains(el)) return null;
      if (el.classList.contains("w")) return +el.dataset.k;
      if (el.classList.contains("gap")) {          // the space after a word counts as that word
        const pv = el.previousElementSibling;
        const w = pv && (pv.classList.contains("w") ? pv : pv.querySelector(".w:last-child"));
        return w ? +w.dataset.k : null;
      }
      return null;
    };
    const report = (on, f, to, m) => onChange && onChange([...sel].sort((x, y) => x - y), { a: on ? "on" : "off", f, to, m });

    // ---- strokes (shared by Pencil/mouse and finger long-press)
    let drag = null;
    const begin = (k, method) => {
      drag = { on: !sel.has(k), m: method, anchor: k, lo: k, hi: k, orig: new Map([[k, sel.has(k)]]) };
      set(k, drag.on);
      article.classList.add("dragging");
    };
    const extendTo = (k) => {
      if (!drag || k === null) return;
      const lo = Math.min(drag.anchor, k), hi = Math.max(drag.anchor, k);
      for (let j = drag.lo; j <= drag.hi; j++) if (j < lo || j > hi) set(j, drag.orig.get(j));
      for (let j = lo; j <= hi; j++) {
        if (!drag.orig.has(j)) drag.orig.set(j, sel.has(j));
        set(j, drag.on);
      }
      drag.lo = lo; drag.hi = hi;
    };
    const end = () => {
      if (!drag) return;
      const d = drag; drag = null;
      article.classList.remove("dragging");
      report(d.on, d.lo, d.hi, d.m);
    };

    // Apple Pencil and mouse: pointer events
    article.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "touch" || e.button > 0) return;
      const k = wordAt(e.clientX, e.clientY);
      if (k === null) return;
      e.preventDefault();
      begin(k, e.pointerType === "pen" ? "pen" : "mouse");
    });
    article.addEventListener("pointermove", (e) => {
      if (!drag || drag.m === "touch") return;
      e.preventDefault();
      extendTo(wordAt(e.clientX, e.clientY));
    });
    for (const ev of ["pointerup", "pointercancel"]) article.addEventListener(ev, () => { if (drag && drag.m !== "touch") end(); });
    window.addEventListener("pointerup", () => { if (drag && drag.m !== "touch") end(); });

    // Finger: tap toggles a word; press-and-hold then drag marks a range; a swipe scrolls.
    const HOLD_MS = 350, SLOP = 10;
    let touch = null;
    article.addEventListener("touchstart", (e) => {
      const t0 = e.touches[0];
      if ([...e.touches].some(tp => tp.touchType === "stylus")) { e.preventDefault(); return; }   // Pencil: no scrolling
      if (e.touches.length > 1) { touch = null; return; }
      const k = wordAt(t0.clientX, t0.clientY);
      touch = { x: t0.clientX, y: t0.clientY, k, moved: false, timer: null };
      if (k !== null) touch.timer = setTimeout(() => {
        if (touch && !touch.moved) { touch.holding = true; begin(k, "touch"); }
      }, HOLD_MS);
    }, { passive: false });
    article.addEventListener("touchmove", (e) => {
      if ([...e.touches].some(tp => tp.touchType === "stylus")) { e.preventDefault(); return; }
      if (!touch) return;
      const t0 = e.touches[0];
      if (touch.holding) { e.preventDefault(); extendTo(wordAt(t0.clientX, t0.clientY)); return; }
      if (Math.hypot(t0.clientX - touch.x, t0.clientY - touch.y) > SLOP) { touch.moved = true; clearTimeout(touch.timer); }
    }, { passive: false });
    article.addEventListener("touchend", (e) => {
      if (!touch) return;
      clearTimeout(touch.timer);
      const tc = touch; touch = null;
      if (tc.holding) { e.preventDefault(); end(); return; }
      if (!tc.moved && tc.k !== null) {          // a tap: toggle one word
        e.preventDefault();                      // no synthetic click / double-tap zoom
        const on = !sel.has(tc.k);
        set(tc.k, on);
        report(on, tc.k, tc.k, "touch");
      }
    }, { passive: false });
    article.addEventListener("touchcancel", () => { if (touch) clearTimeout(touch.timer); touch = null; if (drag && drag.m === "touch") end(); });

    // Keyboard: a focused sentence toggles as a whole
    article.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const si = e.target.dataset && e.target.dataset.s;
      if (si === undefined) return;
      e.preventDefault();
      const ks = sentWords[+si];
      const on = !ks.every(k => sel.has(k));
      ks.forEach(k => set(k, on));
      report(on, ks[0], ks[ks.length - 1], "key");
    });
    return { el: article, sel };
  }

  function timedContinue(step, getData) {
    const btn = h("button", { class: "btn primary", disabled: true }, t("continue"));
    const note = h("p", { class: "muted center" }, t("waitTimer"));
    btn.addEventListener("click", () => { btn.disabled = true; complete(step, getData ? getData() : {}); });
    startTimer(step.remaining, () => { btn.disabled = false; note.remove(); });
    if (step.remaining <= 0) { btn.disabled = false; note.remove(); }
    return [note, btn];
  }

  function renderRead(step) {
    const { el } = textBlock(step, false);
    $app.replaceChildren(...header(t("readTitle", { title: step.title }), t("readHint")), el,
      h("div", { class: "actions" }, timedContinue(step)));
  }

  function renderHighlight(step) {
    const r1 = step.round === 1;
    const draft = loadDraft(step);
    const started = Date.now() - stepMs(step);
    // every tap/stroke: word range f..to, on/off, ms since the round started, method (touch/pen/mouse/key)
    const log = (draft && draft.log) || [];
    const start = draft && draft.words ? draft.words : (step.selected_words || []);
    const { el, sel } = textBlock(step, true, start, (words, ch) => {
      log.push({ ...ch, t: Date.now() - started });
      saveDraft(step, { words, log });
    });
    $app.replaceChildren(
      ...header(t(r1 ? "hlTitle1" : "hlTitle2", { title: step.title }), t(r1 ? "hlHint1" : "hlHint2")), el,
      h("div", { class: "actions" }, timedContinue(step, () => ({ words: [...sel].sort((x, y) => x - y), log }))));
  }

  // Elapsed ms within the step, measured from server step start (so it survives reloads).
  const stepMs = (step) => Math.round(((step.dur || 0) - (step.remaining || 0)) * 1000);

  // One card per question: text answer + 1-5 confidence. Tracks first keystroke, last edit and
  // number of edits, and keeps a draft so a reload doesn't lose anything.
  function answerCards(step, started) {
    const draft = loadDraft(step) || {};
    const rows = step.questions.map((q, n) => {
      const d = draft[q.qid] || {};
      const input = h("input", { class: "answer", autocomplete: "off", autocapitalize: "off", spellcheck: "false", "aria-label": t("yourAnswer") });
      input.value = d.a || "";
      let conf = d.c ?? null, lastEdit = d.rt ?? null, firstKey = d.f ?? null, edits = d.e ?? 0;
      const btns = t("conf").map((label, k) => h("button", {
        type: "button", class: "conf" + (conf === k + 1 ? " on" : ""), "aria-pressed": conf === k + 1 ? "true" : "false",
        onclick: (e) => {
          conf = k + 1;
          [...e.currentTarget.parentNode.children].forEach((b, j) => {
            b.classList.toggle("on", j === k); b.setAttribute("aria-pressed", j === k ? "true" : "false");
          });
          persist();
        },
      }, h("b", {}, String(k + 1)), h("span", {}, label)));
      input.addEventListener("input", () => {
        lastEdit = Date.now() - started;
        if (firstKey === null) firstKey = lastEdit;
        edits += 1;
        persist();
      });
      const card = h("div", { class: "q card" },
        h("p", { class: "qtext" }, `${n + 1}. ${q.q}`), input,
        h("p", { class: "label" }, t("sure")), h("div", { class: "confs" }, btns));
      return { q, input, card, get conf() { return conf; }, get rt() { return lastEdit; },
               get first() { return firstKey; }, get edits() { return edits; } };
    });
    function persist() {
      const out = {};
      for (const r of rows) out[r.q.qid] = { a: r.input.value, c: r.conf, rt: r.rt, f: r.first, e: r.edits };
      saveDraft(step, out);
    }
    const collect = () => rows.map(r => ({ qid: r.q.qid, answer: r.input.value, confidence: r.conf,
                                            rt_ms: r.rt, first_ms: r.first, edits: r.edits }));
    return { rows, collect };
  }

  function renderTest(step) {
    const { rows, collect } = answerCards(step, Date.now() - stepMs(step));
    const errBox = h("div");
    const submit = h("button", { class: "btn primary" }, t("submit"));
    let sent = false;
    const send = (force) => {
      if (sent) return;
      if (!force && rows.some(r => r.conf === null)) { errBox.replaceChildren(msg(t("needConf"))); return; }
      sent = true; submit.disabled = true; stopTimer();
      complete(step, { answers: collect(), auto: !!force });
    };
    submit.addEventListener("click", () => send(false));
    $app.replaceChildren(...header(t("testTitle", { title: step.title }), t("testHint")),
      ...rows.map(r => r.card), errBox, h("div", { class: "actions" }, submit));
    startTimer(step.remaining, () => send(true));
  }

  function renderFeedback(step) {
    const cards = step.items.map((it, n) => h("div", { class: "q card " + (it.correct ? "ok" : "bad") },
      h("p", { class: "qtext" }, `${n + 1}. ${it.q}`),
      h("p", {}, h("span", { class: "label" }, t("yourAnswer") + ": "), it.yours || t("noAnswer"),
        it.confidence ? h("span", { class: "muted" }, ` · ${t("yourConf")}: ${t("conf")[it.confidence - 1]}`) : null),
      h("p", { class: "key" }, h("span", { class: "label" }, t("correctAnswer") + ": "), h("strong", {}, it.answer))));
    $app.replaceChildren(...header(t("fbTitle", { title: step.title }), t("fbHint")),
      ...(step.auto_submitted ? [msg(t("timeUp"), "info")] : []), ...cards,
      h("div", { class: "actions" }, timedContinue(step)));
  }

  function renderPredict(step) {
    // Sliders start in the middle and must be moved: a default of 0 would be recorded as a real
    // prediction by anyone who skips them.
    const values = {};
    const sliders = step.texts.map(tx => {
      const out = h("output", { class: "muted" }, t("notSet"));
      const range = h("input", { type: "range", min: "0", max: String(tx.max), step: "1", value: String(Math.round(tx.max / 2)),
        class: "unset", "aria-label": tx.title,
        oninput: (e) => {
          e.target.classList.remove("unset");
          out.classList.remove("muted");
          out.textContent = `${e.target.value} ${t("of")} ${tx.max}`; values[tx.text] = +e.target.value;
        } });
      return h("div", { class: "card" }, h("p", { class: "qtext" }, t("quote", { title: tx.title })), range, out);
    });
    let belief = null;
    const beliefs = t("beliefs");
    const opts = h("div", { class: "options" }, Object.entries(beliefs).map(([k, label]) => h("button", {
      type: "button", class: "chip wide",
      onclick: (e) => { belief = k; [...opts.children].forEach(b => b.classList.toggle("on", b === e.currentTarget)); },
    }, label)));
    const errBox = h("div");
    const btn = h("button", { class: "btn primary", onclick: () => {
      if (step.texts.some(tx => values[tx.text] === undefined)) return errBox.replaceChildren(msg(t("needPred")));
      if (!belief) return errBox.replaceChildren(msg(t("needBelief")));
      btn.disabled = true;
      complete(step, { pred: values, belief });
    } }, t("continue"));
    $app.replaceChildren(h("h1", {}, t("predTitle")), h("p", { class: "hint" }, t("predHint", { max: step.texts[0].max })),
      ...sliders, h("p", { class: "qtext" }, t("beliefQ")), opts, errBox, h("div", { class: "actions" }, btn));
  }

  function renderFinal(step) {
    // no time limit: measure from when the page was shown
    const { rows, collect } = answerCards(step, Date.now());
    const errBox = h("div");
    const btn = h("button", { class: "btn primary", onclick: () => {
      if (rows.some(r => r.conf === null)) return errBox.replaceChildren(msg(t("needConf")));
      btn.disabled = true;
      complete(step, { answers: collect() });
    } }, t("submit"));
    $app.replaceChildren(...header(t("finalTitle", { title: step.title }), t("finalHint")), ...rows.map(r => r.card),
      errBox, h("div", { class: "actions" }, btn));
  }

  // End-of-study questionnaire (after the final test, before results). All items single choice.
  function renderSurvey(step) {
    const draft = loadDraft(step) || {};
    const answers = { ...draft };
    const S = t("survey");
    const titles = Object.fromEntries(step.texts.map(x => [x.text, x.title]));
    const errBox = h("div");
    const choice = (item, values, labels, wide) => {
      const box = h("div", { class: wide ? "options" : "scale", role: "radiogroup" }, values.map((v, k) => h("button", {
        type: "button", class: (wide ? "chip wide" : "chip") + (answers[item] === v ? " on" : ""), role: "radio",
        "aria-checked": answers[item] === v ? "true" : "false",
        onclick: (e) => {
          answers[item] = v;
          errBox.replaceChildren();
          [...box.children].forEach(b => { const on = b === e.currentTarget; b.classList.toggle("on", on); b.setAttribute("aria-checked", on ? "true" : "false"); });
          saveDraft(step, answers);
        },
      }, labels[k])));
      return box;
    };
    const cards = Object.entries(step.items).map(([item, values]) => {
      let q, labels, wide = true;
      if (item.startsWith("prior_")) {
        q = S.prior.replace("{title}", t("quote", { title: titles[item.slice(6)] }));
        labels = values.map((v, k) => `${v} · ${S.priorScale[k]}`);
      } else {
        q = S[item].q; labels = values.map(v => S[item].o[v]);
      }
      return h("div", { class: "card q", "data-item": item }, h("p", { class: "qtext" }, q),
        S[item] && S[item].note ? h("small", { class: "note" }, S[item].note) : null, choice(item, values, labels, wide));
    });
    const btn = h("button", { class: "btn primary", onclick: () => {
      if (Object.keys(step.items).some(k => !answers[k])) return errBox.replaceChildren(msg(S.needAll));
      btn.disabled = true;
      complete(step, { answers });
    } }, t("continue"));
    $app.replaceChildren(h("h1", {}, S.title), h("p", { class: "hint" }, S.hint), ...cards, errBox,
      h("div", { class: "actions" }, btn));
  }

  // "How this works" screen at the start of each session (untimed). Session 2's has a practice
  // paragraph to try highlighting on; nothing typed or highlighted there is saved.
  function renderIntro(step) {
    const I = t("intro")[step.variant];
    const dur = (sec) => sec >= 60 ? t("durMin", { n: Math.round(sec / 6) / 10 }) : t("durSec", { n: sec });
    const vars = Object.fromEntries(Object.entries(step.timings || {}).map(([k, v]) => [k, dur(v)]));
    const fill = (x) => x.replace(/\{(\w+)\}/g, (_, k) => (k in vars ? vars[k] : `{${k}}`));
    const kids = [h("h1", {}, I.title)];
    if (step.variant === "s1" && current && current.lang_locked === false) {
      // last chance to switch language: nothing has been shown in the chosen language yet
      const langs = window.__langs || ["en", "ru", "ko", "zh", "mn"];
      kids.push(h("div", { class: "card langpick" }, h("p", { class: "label" }, t("langHint")),
        h("div", { class: "langs" }, langs.map(l => h("button", {
          type: "button", class: "chip" + (l === lang ? " on" : ""), onclick: () => changeLang(l),
        }, I18N[l].langName)))));
    }
    kids.push(h("ol", { class: "howto" }, I.items.map(x => h("li", {}, fill(x)))));
    if (I.sandbox) {
      const { el } = textBlock({ sentences: I.sandbox.map(x => ({ s: x, p: 0 })) }, true, []);
      kids.push(h("div", { class: "card try" }, h("p", { class: "label" }, I.tryTitle), el));
    }
    const btn = h("button", { class: "btn primary", onclick: () => { btn.disabled = true; complete(step, {}); } }, I.button);
    kids.push(h("div", { class: "actions" }, btn));
    $app.replaceChildren(...kids);
  }

  // ------------------------------------------------------------ between sessions / results
  function renderWait(st) {
    const unlockMs = (st.unlock_at - clockOffset) * 1000;
    const when = new Date(unlockMs).toLocaleString({ ko: "ko-KR", ru: "ru-RU", zh: "zh-CN", mn: "mn-MN" }[lang] || "en-GB",
      { weekday: "long", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
    const left = h("p", { class: "muted center" });
    const reopen = h("button", { class: "btn primary hidden", onclick: refresh }, t("continue"));
    const upd = () => {
      const sec = (unlockMs - Date.now()) / 1000;
      if (sec <= 0) { left.textContent = t("openNow"); reopen.classList.remove("hidden"); clearInterval(iv); return; }
      const d = Math.floor(sec / 86400), hrs = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
      left.textContent = t("opensIn", { time: d > 0 ? t("durDH", { d, h: hrs }) : t("durHM", { h: hrs, m }) });
    };
    const iv = setInterval(upd, 1000);
    tick = iv;
    upd();
    const n = st.next === "day2" ? 2 : 3;
    const ics = `/reminder.ics?at=${Math.round(st.unlock_at)}&n=${n}&lang=${lang}`;
    $app.replaceChildren(h("section", { class: "card center" },
      h("h1", {}, "✓ " + t("sessionDone")),
      h("p", {}, t("nextOpens")), h("p", { class: "big" }, when), left, reopen,
      h("p", {}, h("a", { class: "btn cal", href: ics }, "📅 " + t("addCal"))),
      h("p", { class: "hint" }, t("comeBack"))),
      h("p", { class: "center" }, h("button", { class: "link", onclick: logout }, t("logout"))));
  }

  function bar(label, s, pred) {
    const pct = s.n ? Math.round((100 * s.correct) / s.n) : 0;
    return h("div", { class: "result" },
      h("div", { class: "rlabel" }, label),
      h("div", { class: "rbar" }, h("div", { class: "rfill", style: `width:${pct}%` })),
      h("div", { class: "rnum" }, `${s.correct} ${t("of")} ${s.n}`,
        pred !== undefined && pred !== null ? h("span", { class: "muted" }, ` (${t("predicted", { n: pred })})`) : null));
  }

  function renderDone(st) {
    $app.replaceChildren(
      h("h1", {}, t("resultsTitle")),
      h("section", { class: "card" },
        bar(t("routineLabel"), st.scores.test, st.predicted.test),
        bar(t("usualLabel"), st.scores.highlight, st.predicted.highlight)),
      h("section", { class: "card" }, h("h2", {}, t("debriefTitle")), h("p", {}, t("debrief"))),
      h("p", { class: "center" }, h("button", { class: "link", onclick: logout }, t("logout"))));
  }

  boot();
})();
