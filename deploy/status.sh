#!/usr/bin/env bash
# Compact study status, readable in a screenshot of the server console.
#   bash deploy/status.sh
# Shows: code version and timings, counts, one line per participant, and the Insights numbers.
set -euo pipefail
cd "$(dirname "$0")/.."
URL="${STATUS_URL:-http://127.0.0.1:8090}"
KEY="${STATUS_KEY:-$(grep '^ADMIN_KEY=' .env | cut -d= -f2-)}"

UP=$(docker inspect -f '{{.State.StartedAt}}' memory-study 2>/dev/null | cut -c1-16 || true)
echo "== version: $(git log -1 --format='%h %cd' --date=short)   container up since: ${UP:-?}"
echo "== timings: $(grep -E '^(READ_SEC|HIGHLIGHT_SEC|TEST_MAX_SEC|BLOCK_SEC|DAY2_GAP_SEC|DAY9_GAP_SEC)=' .env | tr '\n' ' ')"

OV=$(curl -fsS -H "X-Admin-Key: $KEY" "$URL/api/admin/overview")
IN=$(curl -fsS -H "X-Admin-Key: $KEY" "$URL/api/admin/insights")
python3 - "$OV" "$IN" <<'PY'
import json, sys, collections
ov, ins = json.loads(sys.argv[1]), json.loads(sys.argv[2])
ps = ov["participants"]
real = [p for p in ps if not p.get("tester") and not p.get("excluded")]
short = lambda s: (s.replace("waiting for Day 2", "wait S2").replace("waiting for Day 9", "wait S3")
                    .replace("day0 open", "S1").replace("day2 open", "S2").replace("day9 open", "S3")
                    .replace(" (in progress)", " now"))
c = collections.Counter(short(p["status"]) for p in real)
print(f"== {len(ps)} signed up | {len(real)} real (not test/excluded) | " + ", ".join(f"{k}: {v}" for k, v in sorted(c.items())))
print("   groups: " + ", ".join(f"{g} {n}" for g, n in sorted(collections.Counter(p["group"] for p in real).items()))
      + " | langs: " + ", ".join(f"{l} {n}" for l, n in sorted(collections.Counter(p["lang"] for p in real).items())))
print()
f = lambda v, w: ("-" if v is None or v == "" else str(v))[:w].ljust(w)
print(f("nickname", 12), f("lg", 2), "G", f("status", 9), "TX", f("min S1/S2/S3", 14), f("rout", 5), f("hl", 5),
      f("predR/H", 7), f("belief b/a", 13), f("tabs", 4), "away")
for p in ps:
    flags = ("T" if p.get("tester") else ".") + ("X" if p.get("excluded") else ".")
    mins = "/".join("-" if p.get(k) is None else f"{p[k]:g}" for k in ("s1_min", "s2_min", "s3_min"))
    sc = lambda c, n: f"{c}/{n}" if n else "-"
    pred = f"{p.get('pred_routine') if p.get('pred_routine') is not None else '-'}/{p.get('pred_usual') if p.get('pred_usual') is not None else '-'}"
    bel = f"{(p.get('belief') or '-')[:5]}/{(p.get('survey_belief_after') or '-')[:5]}"
    print(f(p["nickname"], 12), f(p["lang"], 2), p["group"], f(short(p["status"]), 9), flags, f(mins, 14),
          f(sc(p["routine_correct"], p["routine_n"]), 5), f(sc(p["usual_correct"], p["usual_n"]), 5),
          f(pred, 7), f(bel, 13), f(p.get("tab_switches") or 0, 4), f"{p.get('away_sec') or 0:g}s")
print()
pc = lambda x: "-" if x is None else f"{100 * x:.0f}%"
print(f"== INSIGHTS (finished, real): {ins.get('participants', 0)} people, {ins.get('items', 0)} final answers")
if ins.get("participants"):
    b = ins["by_condition"]
    print(f"   1 final score    routine {pc(b['test']['accuracy'])} (conf {b['test']['mean_confidence']})   highlight {pc(b['highlight']['accuracy'])} (conf {b['highlight']['mean_confidence']})")
    pv = ins["prediction_vs_actual"]
    r = lambda x: "-" if x is None else f"{x:.0f}%"
    print(f"   2 predicted/real routine {r(pv['test']['predicted_pct'])}/{r(pv['test']['actual_pct'])}   highlight {r(pv['highlight']['predicted_pct'])}/{r(pv['highlight']['actual_pct'])}")
    be = ins.get("beliefs", {})
    print(f"   3 beliefs before {be.get('before_test', {})}   after {be.get('after_test', {})}")
    h = ins["highlighting"]
    print(f"   4 highlighted facts {pc(h['highlighted']['accuracy'])} (n {h['highlighted']['n']})   not highlighted {pc(h['not_highlighted']['accuracy'])} (n {h['not_highlighted']['n']})")
    e = ins["s1_errors"]
    print("   5 S1 answers -> week later  " + "   ".join(f"{k} {pc(v['accuracy'])} (n {v['n']})" for k, v in e.items()))
    cal = ins["calibration"]
    print("   6 calibration routine  " + " ".join(f"{lv}:{pc(cal['test'][lv]['accuracy'])}" for lv in "12345")
          + "   highlight " + " ".join(f"{lv}:{pc(cal['highlight'][lv]['accuracy'])}" for lv in "12345"))
PY
