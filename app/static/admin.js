(() => {
  const $main = document.getElementById("admin");
  const $updated = document.getElementById("updated");
  let key = localStorage.getItem("ms_admin_key") || "";
  let contentProblems = [];

  function h(tag, attrs = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "class") el.className = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    return el;
  }
  const svgEl = (tag, attrs = {}) => {
    const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    return el;
  };

  async function api(path, body) {
    const opts = { headers: { "X-Admin-Key": key, "Content-Type": "application/json" } };
    if (body !== undefined) { opts.method = "POST"; opts.body = JSON.stringify(body); }
    const res = await fetch(path, opts);
    if (res.status === 403) throw new Error("bad key");
    if (res.status === 503) throw new Error("The admin page is disabled on the server: set ADMIN_KEY (12+ characters) in .env and restart the container.");
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res.json();
  }

  function askKey(err) {
    const input = h("input", { type: "password", placeholder: "Admin key", autocomplete: "current-password" });
    const form = h("form", { class: "card" }, h("h1", {}, "Admin login"), input,
      err ? h("p", { class: "msg error" }, err) : null, h("button", { class: "btn primary" }, "Open dashboard"));
    form.addEventListener("submit", (e) => { e.preventDefault(); key = input.value; localStorage.setItem("ms_admin_key", key); load(); });
    $main.replaceChildren(form);
  }

  const when = (ts) => ts ? new Date(ts * 1000).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
  const rel = (ts, now) => {
    if (!ts) return "";
    const d = ts - now, a = Math.abs(d);
    const s = a >= 86400 ? `${(a / 86400).toFixed(1)} d` : a >= 3600 ? `${(a / 3600).toFixed(1)} h` : `${Math.round(a / 60)} min`;
    return d > 0 ? `in ${s}` : `${s} ago`;
  };
  const pct = (c, n) => n ? Math.round((100 * c) / n) : null;
  const mean = (xs) => xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;

  // Downloads go through fetch with the key in a header (not in the URL, where it would end up
  // in browser history and proxy logs).
  async function download(path, fallbackName) {
    try {
      const res = await fetch(path, { headers: { "X-Admin-Key": key } });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
      const cd = res.headers.get("Content-Disposition") || "";
      const name = (cd.match(/filename="([^"]+)"/) || [])[1] || fallbackName;
      const url = URL.createObjectURL(await res.blob());
      const a = h("a", { href: url, download: name });
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (e) { alert("Download failed: " + e.message); }
  }

  async function load() {
    if (!key) return askKey();
    let ov, ans, chk;
    try { [ov, ans, chk] = await Promise.all([api("/api/admin/overview"), api("/api/admin/answers"), api("/api/admin/content-check")]); }
    catch (e) { return askKey(e.message === "bad key" ? "Wrong admin key." : e.message); }
    contentProblems = chk.problems;
    render(ov, ans.answers);
  }

  function render(ov, answers) {
    const now = ov.server_now;
    const ps = ov.participants;
    const finished = ps.filter(p => p.stage === "done" && !p.excluded && p.routine_n && p.usual_n);
    const rP = finished.map(p => pct(p.routine_correct, p.routine_n));
    const uP = finished.map(p => pct(p.usual_correct, p.usual_n));
    const better = finished.filter(p => p.routine_correct / p.routine_n > p.usual_correct / p.usual_n).length;
    const worse = finished.filter(p => p.routine_correct / p.routine_n < p.usual_correct / p.usual_n).length;
    const dueNow = ps.filter(p => !p.excluded && (p.stage === "day2" || p.stage === "day9") && !p.status.includes("in progress"));
    const fmtPct = (x) => x === null ? "–" : `${Math.round(x)}%`;

    const stats = h("div", { class: "stats" },
      [["Signed up", ps.length], ["Finished all 3", finished.length], ["Due now (remind)", dueNow.length],
       ["Mean: routine", fmtPct(mean(rP))], ["Mean: highlight", fmtPct(mean(uP))],
       ["Routine better / tie / worse", `${better} / ${finished.length - better - worse} / ${worse}`]]
        .map(([label, val]) => h("div", { class: "stat" }, h("b", {}, val), h("span", {}, label))));

    const exports = h("div", { class: "row" }, "Export CSV:",
      ["participants", "answers", "highlights", "events"].map(w => h("a", { href: "#", onclick: (e) => { e.preventDefault(); download(`/api/admin/export/${w}.csv`, `${w}.csv`); } }, `${w}.csv`)),
      h("button", { class: "btn small", onclick: () => download("/api/admin/backup.db", "study.db") }, "Download database backup"),
      h("button", { class: "btn small", onclick: load }, "Refresh"),
      h("button", { class: "btn small", onclick: async () => {
        if (ps.length && !confirm("Participants have already started. Changing content.json now will break the comparison. Reload anyway?")) return;
        try {
          const r = await api("/api/admin/reload-content", {});
          alert(`Content reloaded. Languages: ${r.langs.join(", ")}` + (r.problems.length ? `\n\nWarnings:\n- ${r.problems.join("\n- ")}` : "\n\nNo problems found."));
          load();
        }
        catch (e) { alert("Reload failed: " + e.message); }
      } }, "Reload content.json"));
    const warn = contentProblems.length ? h("div", { class: "card", style: "border-color: var(--bad)" },
      h("strong", {}, "content.json warnings"), h("ul", {}, contentProblems.map(x => h("li", {}, x)))) : null;

    const head = ["Nickname", "Lang", "Grp", "Status", "Opens / due", "Gap 1→2 (h)", "Gap 2→3 (h)", "Session min (1/2/3)", "Routine", "Highlight", "Pred R", "Pred H", "Belief", "Tab switches", ""];
    const table = h("table", {}, h("thead", {}, h("tr", {}, head.map(x => h("th", {}, x)))),
      h("tbody", {}, ps.map(p => {
        const isDue = dueNow.includes(p);
        return h("tr", { class: (isDue ? "due " : "") + (p.excluded ? "excluded" : "") },
          h("td", {}, p.nickname), h("td", {}, p.lang), h("td", {}, p.group), h("td", {}, p.status),
          h("td", { title: when(p.due_at) }, p.due_at ? `${when(p.due_at)} (${rel(p.due_at, now)})` : ""),
          h("td", { class: "num" }, p.gap_day0_day2_h ?? ""), h("td", { class: "num" }, p.gap_day2_day9_h ?? ""),
          h("td", { class: "num", title: "Minutes from opening a session to finishing it. Much longer than ~15/15/5 means it was interrupted." },
            [p.s1_min, p.s2_min, p.s3_min].map(x => x ?? "–").join(" / ")),
          h("td", { class: "num" }, p.routine_n ? `${p.routine_correct}/${p.routine_n}` : ""),
          h("td", { class: "num" }, p.usual_n ? `${p.usual_correct}/${p.usual_n}` : ""),
          h("td", { class: "num" }, p.pred_routine ?? ""), h("td", { class: "num" }, p.pred_usual ?? ""),
          h("td", {}, p.belief ?? ""), h("td", { class: "num" }, p.tab_switches || ""),
          h("td", {}, h("button", { class: "btn small", onclick: async () => {
            if (!p.excluded && !confirm(`Exclude ${p.nickname} from the analysis? (Data is kept; you can include them again.)`)) return;
            await api("/api/admin/exclude", { id: p.id, excluded: !p.excluded }); load(); } },
            p.excluded ? "Include" : "Exclude")));
      })));

    $main.replaceChildren(
      h("h1", {}, "Overview"), ...(warn ? [warn] : []), stats, exports,
      h("h2", {}, "Participants"),
      h("p", { class: "legend" }, "Yellow rows have a session open now and haven't started it: send them a reminder."),
      h("div", { class: "tablewrap" }, table),
      h("h2", { style: "margin-top:28px" }, "Final scores per person"),
      h("p", { class: "legend" }, "Each line is one finished participant: left dot = highlight + reread, right dot = self-test routine (% correct)."),
      h("div", { class: "card chartcard" }, pairedChart(finished)),
      h("h2", { style: "margin-top:28px" }, "Check final answers"),
      h("p", { class: "legend" }, "Answers marked wrong by keyword matching are listed first. Click ✓ or ✗ to override, 'auto' to undo."),
      gradeTable(answers),
      h("p", { class: "legend" }, `Timings (s): reading ${ov.config.READ_SEC}, highlight round ${ov.config.HIGHLIGHT_SEC}, self-test max ${ov.config.TEST_MAX_SEC}, test+feedback block ${ov.config.BLOCK_SEC}; gaps: Day 2 after ${ov.config.DAY2_GAP_SEC}s, Day 9 after ${ov.config.DAY9_GAP_SEC}s.`),
    );
    $updated.textContent = "updated " + new Date().toLocaleTimeString("en-GB");
  }

  function pairedChart(finished) {
    const W = 520, H = 300, x1 = 150, x2 = 370, top = 20, bottom = 260;
    const y = (v) => bottom - (v / 100) * (bottom - top);
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img", "aria-label": "Paired scores per participant" });
    for (const v of [0, 25, 50, 75, 100]) {
      svg.append(svgEl("line", { x1: 90, x2: 430, y1: y(v), y2: y(v), stroke: "currentColor", "stroke-opacity": 0.12 }));
      const tx = svgEl("text", { x: 82, y: y(v) + 4, "text-anchor": "end" }); tx.textContent = `${v}%`; svg.append(tx);
    }
    const jitter = (i) => ((i * 37) % 11) - 5;
    finished.forEach((p, i) => {
      const u = 100 * p.usual_correct / p.usual_n, r = 100 * p.routine_correct / p.routine_n;
      const color = r > u ? "#2f5bd3" : r < u ? "#b23b2e" : "#8a8a85";
      svg.append(svgEl("line", { x1: x1, x2: x2, y1: y(u) + jitter(i), y2: y(r) + jitter(i), stroke: color, "stroke-opacity": 0.55, "stroke-width": 2 }));
      svg.append(svgEl("circle", { cx: x1, cy: y(u) + jitter(i), r: 5, fill: color }));
      svg.append(svgEl("circle", { cx: x2, cy: y(r) + jitter(i), r: 5, fill: color }));
    });
    const mean = (xs) => xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
    const mu = mean(finished.map(p => 100 * p.usual_correct / p.usual_n)), mr = mean(finished.map(p => 100 * p.routine_correct / p.routine_n));
    if (mu !== null) {
      svg.append(svgEl("line", { x1: x1 - 30, x2: x1 + 30, y1: y(mu), y2: y(mu), stroke: "currentColor", "stroke-width": 3 }));
      svg.append(svgEl("line", { x1: x2 - 30, x2: x2 + 30, y1: y(mr), y2: y(mr), stroke: "currentColor", "stroke-width": 3 }));
    }
    for (const [x, label] of [[x1, "Highlight + reread"], [x2, "Self-test routine"]]) {
      const tx = svgEl("text", { x, y: 288, "text-anchor": "middle", "font-weight": 600 }); tx.textContent = label; svg.append(tx);
    }
    if (!finished.length) { const tx = svgEl("text", { x: 260, y: 140, "text-anchor": "middle" }); tx.textContent = "No finished participants yet"; svg.append(tx); }
    return svg;
  }

  function gradeTable(answers) {
    const sorted = [...answers].sort((a, b) => (a.effective - b.effective) || a.text_id.localeCompare(b.text_id) || a.qid.localeCompare(b.qid));
    if (!sorted.length) return h("p", { class: "muted" }, "No final answers yet.");
    return h("div", { class: "tablewrap" }, h("table", { class: "grade" },
      h("thead", {}, h("tr", {}, ["Who", "Text", "Question", "Key", "Answer", "Auto", "Your call"].map(x => h("th", {}, x)))),
      h("tbody", {}, sorted.map(a => h("tr", {},
        h("td", {}, `${a.nickname} (${a.lang})`), h("td", {}, `${a.text_id} · ${a.condition === "test" ? "routine" : "highlight"}`),
        h("td", {}, a.question), h("td", {}, a.key), h("td", {}, a.answer || "—"),
        h("td", {}, a.auto_correct ? "✓" : "✗"),
        h("td", {}, [[1, "✓"], [0, "✗"], [null, "auto"]].map(([v, label]) => h("button", {
          class: "btn small" + (a.override === v && v !== null ? " on" : "") + (v === null && a.override === null ? " on" : ""),
          onclick: async () => { await api("/api/admin/grade", { id: a.id, override: v }); load(); },
        }, label))))))));
  }

  load();
  setInterval(() => { if (key && document.visibilityState === "visible") load(); }, 60000);
})();
