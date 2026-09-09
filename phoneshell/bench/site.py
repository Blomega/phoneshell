"""Generate the public benchmark site from real results.

Everything on the page comes from the task files and the results file. Nothing
is written by hand, so the site cannot drift from what was actually measured,
and a re-run republishes an honest page.
"""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

from .runner import RESULTS, score
from .schema import Task, load_all
from .stats import mcnemar_exact, required_n, wilson

ROOT = Path(__file__).resolve().parent.parent.parent
SITE = ROOT / "site"

# The run the leaderboard is drawn from. The newest complete cross-vendor sweep
# wins, so a finished xv2 supersedes xv without a code change, and a half-built
# one does not: a sweep in progress has fewer models than it will end with, and
# publishing it mid-flight would show a board that changes shape hourly.
def _board_run() -> str:
    best, best_cells = "xv", 0
    for name in ("xv", "xv2", "xv3"):
        path = RESULTS / f"{name}.jsonl"
        if not path.exists():
            continue
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        models = {r["model"] for r in rows if not r.get("skipped")}
        cells = len({(r["model"], r["task_id"]) for r in rows if not r.get("skipped")})
        if len(models) >= 6 and cells > best_cells:
            best, best_cells = name, cells
    return best

MODEL_LABELS = {
    "deepseek/deepseek-v3.2": "DeepSeek v3.2",
    "qwen/qwen3-max": "Qwen3-Max",
    "anthropic/claude-opus-4.8": "Claude Opus 4.8",
    "moonshotai/kimi-k3": "Kimi K3",
    "google/gemini-3.1-pro-preview": "Gemini 3.1 Pro",
    "openai/gpt-5.1": "GPT-5.1",
    "claude-sonnet-5": "Claude Sonnet 5",
    "claude-opus-5": "Claude Opus 5",
    "claude-haiku-4-5-20251001": "Claude Haiku 4.5",
    "gpt-5": "GPT-5",
    "gemini-3-pro": "Gemini 3 Pro",
}

LOGO_URI = "data:image/png;base64,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"

# The blolabel identity, taken from the live site: Montserrat, a white ground,
# slate-950 ink, and a teal-to-blue-to-purple gradient reserved for one line of
# the headline. Deliberately single-theme, because the brand is.
CSS = """
:root{
  --ground:#ffffff; --panel:#f8fafc; --raise:#f1f5f9;
  --ink:#020817; --muted:#64748b; --nav:#374151; --faint:#94a3b8;
  --line:#e2e8f0; --line-soft:#eef2f7;
  --dark:#0f172a; --blue:#2563eb; --teal:#0d9488; --purple:#7c3aed;
  --emerald:#10b981; --red:#ef4444; --amber:#d97706;
  --pass-bg:#ecfdf5; --fail-bg:#fef2f2; --idle-bg:#f1f5f9;
  --r:8px;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--ground);color:var(--ink);
  font-family:Montserrat,ui-sans-serif,system-ui,-apple-system,sans-serif;
  font-size:16px;line-height:1.65;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:0 28px}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace}
.num{font-variant-numeric:tabular-nums}
h1,h2,h3{margin:0;font-weight:700;letter-spacing:-.02em;text-wrap:balance}
h1{font-size:clamp(36px,6vw,72px);line-height:1.06}
h2{font-size:clamp(26px,3.4vw,36px);line-height:1.2}
h3{font-size:18px;letter-spacing:-.01em}
p{margin:0}
a{color:var(--blue);text-decoration:none}
a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--blue);outline-offset:2px;border-radius:4px}

header{position:sticky;top:0;z-index:40;background:rgba(255,255,255,.88);
  backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
.bar{display:flex;align-items:center;gap:28px;height:88px}
.bar img{height:52px;width:auto;display:block}
.bar nav{display:flex;gap:24px;margin-left:auto}
.bar nav a{color:var(--nav);font-size:14px;font-weight:500;text-decoration:none}
.bar nav a:hover{color:var(--ink)}
@media(max-width:760px){.bar nav{display:none}}

.hero{padding:78px 0 56px;text-align:center;
  background-image:radial-gradient(var(--line) 1px,transparent 1px);
  background-size:22px 22px}
.hero .inner{max-width:860px;margin:0 auto;display:flex;flex-direction:column;
  align-items:center;gap:24px}
.eyebrow{display:inline-flex;align-items:center;gap:8px;font-size:12px;font-weight:600;
  letter-spacing:.04em;color:var(--nav);background:#fff;border:1px solid var(--line);
  border-radius:999px;padding:6px 14px}
.grad{background:linear-gradient(90deg,var(--teal) 0%,var(--blue) 52%,var(--purple) 100%);
  -webkit-background-clip:text;background-clip:text;color:transparent}
.lede{font-size:clamp(16px,2vw,20px);color:var(--muted);max-width:60ch}
.cta{display:inline-flex;align-items:center;gap:10px;background:var(--dark);color:#fff;
  font-weight:600;font-size:15px;padding:13px 26px;border-radius:var(--r);text-decoration:none}
.cta:hover{background:#1e293b;text-decoration:none}
.dots{display:flex;gap:26px;flex-wrap:wrap;justify-content:center;
  font-family:ui-monospace,Menlo,monospace;font-size:12.5px;color:var(--muted)}
.dots span{display:inline-flex;align-items:center;gap:8px}
.dots i{width:7px;height:7px;border-radius:50%;display:inline-block}

.strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  border-top:1px solid var(--line);border-bottom:1px solid var(--line);background:#fff}
.strip div{padding:22px 28px;border-right:1px solid var(--line-soft)}
.strip div:last-child{border-right:0}
.strip b{display:block;font-size:30px;font-weight:700;letter-spacing:-.025em;line-height:1.1;
  font-variant-numeric:tabular-nums}
.strip span{display:block;font-size:12.5px;color:var(--muted);margin-top:4px}
.strip .hi b{color:var(--emerald)}

section{padding:64px 0}
section+section{border-top:1px solid var(--line-soft)}
.sh{max-width:64ch;margin-bottom:30px;display:flex;flex-direction:column;gap:12px}
.sh p{color:var(--muted)}

table{width:100%;border-collapse:collapse;font-size:14.5px}
th{text-align:left;font-size:11px;font-weight:600;letter-spacing:.09em;text-transform:uppercase;
  color:var(--faint);padding:0 14px 10px;border-bottom:1px solid var(--line)}
td{padding:12px 14px;border-bottom:1px solid var(--line-soft);vertical-align:top}
th.r,td.r{text-align:right}
.scroll{overflow-x:auto}
tbody tr:hover{background:var(--panel)}

.pill{display:inline-block;font-family:ui-monospace,Menlo,monospace;font-size:11px;
  font-weight:600;padding:3px 10px;border-radius:999px}
.p-pass{background:var(--pass-bg);color:#047857}
.p-fail{background:var(--fail-bg);color:#b91c1c}
.p-idle{background:var(--idle-bg);color:var(--muted)}
.chip{display:inline-block;font-family:ui-monospace,Menlo,monospace;font-size:11px;
  padding:2px 8px;border-radius:5px;background:var(--raise);color:var(--nav);margin:0 4px 4px 0}
.bar-mini{height:5px;background:var(--line-soft);border-radius:3px;overflow:hidden;
  min-width:70px;margin-top:6px}
.bar-mini i{display:block;height:100%;border-radius:3px;
  background:linear-gradient(90deg,var(--teal),var(--blue))}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:18px}
.card{border:1px solid var(--line);border-radius:12px;padding:22px;background:#fff;
  display:flex;flex-direction:column;gap:9px}
.card h3{font-size:16px}
.card p{font-size:14px;color:var(--muted)}
.card .n{font-family:ui-monospace,Menlo,monospace;font-size:11px;font-weight:600;
  letter-spacing:.08em;color:var(--blue)}
pre{background:var(--dark);color:#e2e8f0;border-radius:12px;padding:20px 22px;
  overflow-x:auto;font-size:13px;line-height:1.6;margin:0}
pre code{font-family:ui-monospace,Menlo,monospace}
.note{border-left:3px solid var(--emerald);background:var(--panel);padding:16px 20px;
  border-radius:0 var(--r) var(--r) 0;color:var(--muted);font-size:14.5px}
.warn{border-left:3px solid var(--amber);background:#fffbeb;padding:18px 22px;
  border-radius:0 var(--r) var(--r) 0;font-size:14.5px;color:#78350f}
.warn b{color:#451a03}
.ci{display:block;font-size:11.5px;color:var(--faint);font-weight:500;
  letter-spacing:.01em;margin-top:2px}
.pairs{width:100%;font-size:13px}
.pairs td,.pairs th{padding:7px 10px}
.pairs .ns{color:var(--faint)}

footer{border-top:1px solid var(--line);padding:34px 0 130px;color:var(--faint);font-size:13px}
footer a{color:var(--muted)}

#cookie{position:fixed;left:0;right:0;bottom:0;z-index:60;background:#fff;
  border-top:1px solid var(--line);box-shadow:0 -6px 24px -18px rgba(2,8,23,.4);
  padding:18px 0}
#cookie .in{display:flex;gap:22px;align-items:center;flex-wrap:wrap}
#cookie h3{font-size:16px;display:flex;align-items:center;gap:9px}
#cookie p{font-size:13.5px;color:var(--muted);max-width:60ch}
#cookie .btns{display:flex;gap:10px;margin-left:auto;flex-wrap:wrap}
#cookie button{font:inherit;font-size:13.5px;font-weight:600;padding:10px 18px;
  border-radius:var(--r);border:1px solid var(--line);background:#fff;color:var(--nav);cursor:pointer}
#cookie button.solid{background:var(--blue);border-color:var(--blue);color:#fff}
#cookie button:hover{border-color:var(--nav)}
#cookie button.solid:hover{background:#1d4ed8}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
"""

SCRIPT = """
(function(){
  var KEY='blolabel-cookie-consent', el=document.getElementById('cookie');
  if(!el) return;
  try{ if(localStorage.getItem(KEY)) { el.remove(); return; } }catch(e){}
  function choose(v){ try{ localStorage.setItem(KEY,v); }catch(e){} el.remove(); }
  el.querySelectorAll('[data-consent]').forEach(function(b){
    b.addEventListener('click', function(){ choose(b.dataset.consent); });
  });
})();
"""

CAPABILITY_NOTES = {
    "app-resolve": "turning a spoken app name into the right bundle id",
    "back": "leaving a screen when the way out has no label",
    "context-menu": "holding a link until its preview menu opens",
    "date-navigate": "moving around a calendar rather than a list",
    "edge-gesture": "a swipe that must start at y=0, off the drawn screen",
    "hierarchy": "going up, not just down, through nested screens",
    "icon-only-control": "a control with a glyph and no text to match on",
    "lifecycle": "launching, backgrounding and returning to an app",
    "long-press": "a press held long enough to be a different gesture",
    "picker": "reaching a spinning wheel",
    "picker-multi": "addressing the right column when several are side by side",
    "picker-set": "turning a wheel to a value, which a swipe cannot do",
    "precise-taps": "hitting small targets laid out in a grid",
    "read-field": "reading a value back off the screen, not guessing it",
    "recover": "noticing the wrong app is open and fixing it",
    "scroll-end": "reaching something below the fold",
    "search-then-act": "using an app's own search instead of navigating",
    "slider": "reaching a continuous control",
    "switch": "reading and setting a toggle",
    "tabbar": "moving between tabs in an unfamiliar app",
    "text-exact": "typing a string that must match character for character",
    "text-long": "typing something long enough for autocorrect to interfere",
    "text-select": "selecting text that is already on screen",
}


def experiment_two() -> dict | None:
    """The paired vision ablation, computed from the run files.

    Nothing on the page is typed by hand. If the experiment is re-run the site
    re-states whatever the new data says, including if it says something less
    convenient.
    """
    import math
    from .schema import load_all as _load

    def rows(run: str) -> dict:
        path = RESULTS / f"{run}.jsonl"
        out: dict = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    r = json.loads(line)
                    if not r.get("skipped"):
                        out[(r["model"], r["task_id"])] = r
        return out

    seen, blind = rows("exp2-v"), rows("exp2-n")
    shared = sorted(set(seen) & set(blind))
    if not shared:
        return None
    stratum = {t.id: g.split(":", 1)[1]
               for t in _load(ROOT / "environments")
               for g in t.tags if g.startswith("stratum:")}

    def mcnemar(b: int, c: int) -> float:
        n = b + c
        if n == 0:
            return 1.0
        k = min(b, c)
        return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)

    def cell(keys: list) -> dict:
        a = sum(1 for k in keys if seen[k]["passed"])
        d = sum(1 for k in keys if blind[k]["passed"])
        b = sum(1 for k in keys if seen[k]["passed"] and not blind[k]["passed"])
        c = sum(1 for k in keys if blind[k]["passed"] and not seen[k]["passed"])
        cv = sum(seen[k].get("cost_usd") or 0 for k in keys) / max(len(keys), 1)
        cn = sum(blind[k].get("cost_usd") or 0 for k in keys) / max(len(keys), 1)
        return {"n": len(keys), "with": a, "without": d, "b": b, "c": c,
                "p": mcnemar(b, c), "tax": 100 * (cv - cn) / max(cn, 1e-9)}

    out = {"runs": len(shared) * 2, "models": sorted({m for m, _ in shared})}
    for name in ("render", "tree"):
        out[name] = cell([k for k in shared if stratum.get(k[1]) == name])
    out["per_model"] = [
        {"model": m,
         **cell([k for k in shared if stratum.get(k[1]) == "render" and k[0] == m])}
        for m in out["models"]
    ]
    return out


# Runs recorded before the harness stamped the device carry no model or OS in
# their rows. Naming that phone here is an assertion from the project record,
# not something read from the data, so it is written down once and labelled
# rather than interpolated silently into a sentence. Model and iOS version are
# public product facts, not anything personal.
LEGACY_DEVICE = ("iPhone 17 Pro Max", "26.6")
# WDA answers "iphone" for every iPhone ever made, so a row carrying it has an
# OS version but no usable model.
GENERIC_MODELS = {"", "iphone", "iPhone"}


def devices_used(*runs: str) -> str:
    """Name the phone from the result rows rather than from prose.

    The footer said "iPhone 17 Pro Max running iOS 26.6" for as long as that
    happened to be true. There are two phones on this desk now, and a sentence
    naming the wrong one is a quiet lie about which instrument produced the
    numbers.
    """
    seen: set[tuple[str, str]] = set()
    legacy = False
    for run in runs:
        path = RESULTS / f"{run}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            model = str(r.get("device_model") or "")
            os_v = str(r.get("device_os") or "")
            if model in GENERIC_MODELS and not os_v:
                legacy = True
            elif model in GENERIC_MODELS:
                seen.add(("iPhone", os_v))
            else:
                seen.add((model, os_v))
    if legacy:
        seen.add(LEGACY_DEVICE)
    if not seen:
        return "a physical iPhone"
    parts = [f"{m} running iOS {v}" if v else m for m, v in sorted(seen)]
    return "a physical " + " and ".join(parts)


def board_significance(run: str = "xv") -> dict | None:
    """Test every pair on the board instead of implying an order by sorting it.

    A leaderboard sorted by pass rate reads as a ranking whether or not the
    numbers support one. On this suite they do not: the models were run on the
    same tasks, which makes the comparison paired, and not one pair of them
    separates under McNemar. Publishing that fact next to the table is the
    difference between a benchmark and a scoreboard.
    """
    path = RESULTS / f"{run}.jsonl"
    if not path.exists():
        return None
    live = {t.id for t in load_all(ROOT / "environments", include_private=False)}
    res: dict[tuple[str, str], bool] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("skipped") or r["task_id"] not in live:
            continue
        res[(r["model"], r["task_id"])] = bool(r["passed"])
    models = sorted({m for m, _ in res})
    if len(models) < 2:
        return None
    # Only tasks every model attempted: an unbalanced set makes the percentages
    # incomparable before any test is run.
    shared = set.intersection(*({t for m, t in res if m == mm} for mm in models))
    if not shared:
        return None
    n = len(shared)
    passed = {m: sum(1 for t in shared if res[(m, t)]) for m in models}
    order = sorted(models, key=lambda m: -passed[m])

    pairs = []
    for a, b in ((x, y) for i, x in enumerate(order) for y in order[i + 1:]):
        wa = sum(1 for t in shared if res[(a, t)] and not res[(b, t)])
        wb = sum(1 for t in shared if res[(b, t)] and not res[(a, t)])
        pairs.append({
            "a": a, "b": b, "b_count": wa, "c_count": wb,
            "p": mcnemar_exact(wa, wb), "need": required_n(wa, wb, n),
        })
    solved = sum(1 for t in shared if all(res[(m, t)] for m in models))
    unsolved = sum(1 for t in shared if not any(res[(m, t)] for m in models))
    return {
        "n": n, "models": order, "passed": passed, "pairs": pairs,
        "per_task": {t: sum(1 for m in models if res[(m, t)]) for t in shared},
        "significant": [q for q in pairs if q["p"] < 0.05],
        "ceiling": solved, "floor": unsolved,
        "discriminating": n - solved - unsolved,
        "cheapest": min((q["need"] for q in pairs if q["need"]), default=None),
    }


def _esc(text: str) -> str:
    return html.escape(str(text))


def build(run_name: str = "v1") -> Path:
    BOARD_RUN = _board_run()
    # include_private=False: the held-out tasks must not be published, not even
    # their ids and instructions.
    tasks = load_all(ROOT / "environments", include_private=False)
    # Counted, not typed: the page said "60 of 76" long after it was 80 of 96.
    n_all = len(load_all(ROOT / "environments"))
    n_caps = len({g.split(':', 1)[1] for t in tasks for g in t.tags
                  if g.startswith('capability:')})
    # The leaderboard is the CROSS-VENDOR sweep when there is one. Scoring only
    # the run that happens to be named on the command line showed a single model
    # and made the page look like a one-horse race.
    board = score(BOARD_RUN, include_private=False) or score(run_name, include_private=False)
    rows = []
    path = RESULTS / f"{run_name}.jsonl"
    if path.exists():
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    by_id = {t.id: t for t in tasks}
    SITE.mkdir(exist_ok=True)
    (SITE / "style.css").write_text(CSS)

    # ---------------------------------------------------------- leaderboard
    if board:
        lb = ["<table><thead><tr><th>Model</th><th class='num'>Pass rate</th>"
              "<th class='num'>Passed</th><th class='num'>Avg steps</th>"
              "<th class='num'>Avg time</th><th class='num'>Cost/task</th></tr></thead><tbody>"]
        for model, r in sorted(board.items(), key=lambda kv: -kv[1]["pass_rate"]):
            label = MODEL_LABELS.get(model, model)
            per = r["total_cost_usd"] / max(r["tasks"], 1)
            # The interval, not the point estimate, is the honest number. Three
            # models sit on 83.3% and their intervals span 64-93%: printing only
            # the percentage invites a ranking the data cannot support.
            lo, hi = wilson(r["passed"], r["tasks"])
            lb.append(
                f"<tr><td>{_esc(label)}</td>"
                f"<td class='num'><b>{r['pass_rate']}%</b>"
                f"<span class='ci'>95% CI {lo:.0f}\u2013{hi:.0f}%</span>"
                f"<div class='bar'><i style='width:{r['pass_rate']}%'></i></div></td>"
                f"<td class='num'>{r['passed']}/{r['tasks']}</td>"
                f"<td class='num'>{r['avg_turns']}</td>"
                f"<td class='num'>{r['avg_seconds']:.0f}s</td>"
                f"<td class='num'>${per:.3f}</td></tr>"
            )
        lb.append("</tbody></table>")
        leaderboard = "\n".join(lb)
    else:
        leaderboard = "<p class='dim'>No scored run yet.</p>"

    # ---------------------------------------------------------- is it a ranking?
    # Sorting a table by pass rate makes it read as an order. Whether it IS one
    # is a separate question with a separate answer, and on this suite the
    # answer is no. Publishing the table without this panel would be the most
    # misleading thing on the page.
    device_line = devices_used(BOARD_RUN, run_name)
    sig = board_significance(BOARD_RUN)
    if sig:
        pair_rows = []
        for q in sorted(sig["pairs"], key=lambda q: (q["need"] or 10 ** 9)):
            a = MODEL_LABELS.get(q["a"], q["a"])
            b = MODEL_LABELS.get(q["b"], q["b"])
            need = (f"{q['need']} tasks" if q["need"]
                    else "<span class='ns'>never: the wins are symmetric</span>")
            pair_rows.append(
                f"<tr><td>{_esc(a)} <span class='ns'>vs</span> {_esc(b)}</td>"
                f"<td class='num'>{q['b_count']}\u2013{q['c_count']}</td>"
                f"<td class='num'>{q['p']:.2f}</td><td class='num'>{need}</td></tr>")
        verdict = (
            f"<b>None of the {len(sig['pairs'])} pairs of models on this board "
            f"separate.</b> Every model ran the same {sig['n']} tasks, which makes "
            f"this a paired comparison, and not one pair reaches p&lt;0.05 under "
            f"McNemar\u2019s exact test. The order of the table above is sorted, "
            f"not ranked."
            if not sig["significant"] else
            f"<b>{len(sig['significant'])} of {len(sig['pairs'])} pairs separate "
            f"at p&lt;0.05.</b> The rest of the ordering is not established.")
        cheapest = (f" The widest gap on the board would need about "
                    f"<b>{sig['cheapest']} tasks</b> to reach significance, "
                    f"against the {sig['n']} it has."
                    if sig["cheapest"] else "")
        board_note = f"""
  <div class="warn" style="margin-top:26px">{verdict}{cheapest}</div>
  <div class="sh" style="margin-top:34px"><h3>Every pair, tested</h3>
  <p class="dim">Wins are counted only on tasks where the two models disagreed;
     tasks they both passed or both failed carry no information about which is
     better. The last column holds each pair\u2019s observed disagreement rate
     fixed and asks how large the suite would have to be for that gap to be
     real.</p></div>
  <table class="pairs"><thead><tr><th>Pair</th><th class="num">Wins</th>
  <th class="num">p</th><th class="num">Tasks needed</th></tr></thead>
  <tbody>{''.join(pair_rows)}</tbody></table>
  <p class="note" style="margin-top:22px">Of the {sig['n']} tasks,
     <b>{sig['ceiling']}</b> were passed by every model and <b>{sig['floor']}</b>
     by none. Only <b>{sig['discriminating']}</b> tell the models apart, so 
     {100 - round(100 * sig['discriminating'] / sig['n'])}% of the suite is
     measuring nothing. Harder tasks, not more models, is what this benchmark
     needs next.</p>"""
    else:
        board_note = ""

    # ---------------------------------------------------------- task table
    # Newest attempt wins: a task re-run after a broken check was corrected must
    # not be reported by the result that broken check produced.
    results_by_task = {r["task_id"]: r for r in rows}
    # A per-task column showing ONE model's outcome, with no column saying which
    # model, is what made this table look like a one-horse race. What a reader
    # wants from a task list is the item's difficulty: of the models that tried
    # it, how many got it. That is also the number that says which tasks are
    # carrying the benchmark and which are decoration.
    per_task = (sig or {}).get("per_task", {})
    n_models = len((sig or {}).get("models", []))
    tt = ["<table><thead><tr><th>Task</th><th>App</th><th>Difficulty</th>"
          "<th class='num'>Checks</th><th class='num'>Steps</th><th class='num'>Time</th>"
          "<th class='num'>Solved by</th></tr></thead><tbody>"]
    for t in tasks:
        r = results_by_task.get(t.id)
        steps = took = "<span class='dim'>-</span>"
        if t.id in per_task:
            k = per_task[t.id]
            cls = "pass" if k == n_models else ("fail" if k == 0 else "")
            verdict = (f"<span class='{cls}'>{k}/{n_models}</span>"
                       f"<div class='bar-mini'><i style='width:"
                       f"{100 * k // max(n_models, 1)}%'></i></div>")
            if r and not r.get("skipped"):
                steps = str(r.get("turns") or "-")
                took = f"{r.get('seconds', 0):.0f}s"
            tt.append(
                f"<tr><td><code>{_esc(t.id)}</code><br><span class='dim'>"
                f"{_esc(t.instruction[:88])}</span></td>"
                f"<td>{_esc((t.app or '').rsplit('.', 1)[-1] or 'system')}</td>"
                f"<td>{_esc(t.difficulty)}</td>"
                f"<td class='num'>{len(t.checks)}</td><td class='num'>{steps}</td>"
                f"<td class='num'>{took}</td><td class='num'>{verdict}</td></tr>")
            continue
        if r is None:
            verdict = "<span class='dim'>not swept</span>"
        elif r.get("skipped"):
            # Never scored: the model was not asked, or the app is not on the
            # phone. Reporting these as failures would blame the model for the
            # device, which is how a benchmark starts lying.
            verdict = f"<span class='dim' title='{_esc(str(r.get('skip_reason',''))[:120])}'>not scored</span>"
        else:
            # Reference run only: one model, so it is evidence the task is
            # runnable, not a difficulty figure. Marked as such rather than
            # dressed up as a score under the same heading as k/6.
            mark = "pass" if r["passed"] else "fail"
            verdict = (f"<span class='{mark}' title='reference run only, "
                       f"not the multi-model sweep'>{mark}</span>"
                       f"<span class='ci'>1 model</span>")
            steps = str(r.get("turns") or "-")
            took = f"{r.get('seconds', 0):.0f}s"
        app = (t.app or "").rsplit(".", 1)[-1] or "system"
        tt.append(
            f"<tr><td><code>{_esc(t.id)}</code><br><span class='dim'>{_esc(t.instruction[:88])}</span></td>"
            f"<td>{_esc(app)}</td><td>{_esc(t.difficulty)}</td>"
            f"<td class='num'>{len(t.checks)}</td><td class='num'>{steps}</td>"
            f"<td class='num'>{took}</td><td class='num'>{verdict}</td></tr>"
        )
    tt.append("</tbody></table>")
    task_table = "\n".join(tt)

    # ------------------------------------------------------- capability table
    # What the suite is FOR. A pass rate says how often a model succeeded; this
    # says at what. Each capability is isolated by at least one task, so a
    # failure points at a missing skill rather than at a long task going wrong
    # somewhere in the middle.
    caps: dict[str, list] = {}
    for t in tasks:
        for tag in t.tags:
            if tag.startswith("capability:"):
                caps.setdefault(tag.split(":", 1)[1], []).append(t)
    ct = ["<table><thead><tr><th>Capability</th><th>What it takes</th>"
          "<th class='num'>Tasks</th><th class='num'>Passed</th></tr></thead><tbody>"]
    for name, ts in sorted(caps.items()):
        scored = [results_by_task.get(t.id) for t in ts]
        scored = [r for r in scored if r and not r.get("skipped")]
        got = sum(1 for r in scored if r["passed"])
        cell = (f"{got}/{len(scored)}" if scored else "<span class='dim'>-</span>")
        ct.append(f"<tr><td><code>{_esc(name)}</code></td>"
                  f"<td class='dim'>{_esc(CAPABILITY_NOTES.get(name, ''))}</td>"
                  f"<td class='num'>{len(ts)}</td><td class='num'>{cell}</td></tr>")
    ct.append("</tbody></table>")
    capability_table = "\n".join(ct)

    # ---------------------------------------------------------- example task
    example = by_id.get("calculator.arithmetic") or tasks[0]
    example_yaml = example.source.read_text() if example.source else ""

    counts = {"easy": 0, "medium": 0, "hard": 0}
    for t in tasks:
        counts[t.difficulty] = counts.get(t.difficulty, 0) + 1
    total_checks = sum(len(t.checks) for t in tasks)
    best = max(board.values(), key=lambda r: r["pass_rate"])["pass_rate"] if board else None

    exp = experiment_two()
    per_model_rows = "\n".join(
        f"<tr><td>{MODEL_LABELS.get(m['model'], m['model'].split('/')[-1])}</td>"
        f"<td class='r num'>{m['with']}/{m['n']}</td>"
        f"<td class='r num'>{m['without']}/{m['n']}</td>"
        f"<td class='r num'>{m['p']:.4f}</td></tr>"
        for m in exp["per_model"]) if exp else ""
    probes = sum(1 for t in tasks if any(g.startswith("capability:") for g in t.tags))
    jobs = len(tasks) - probes
    apps = len({(t.app or "").rsplit(".", 1)[-1] for t in tasks if t.app})
    # Count from the SAME rows the leaderboard scores: live tasks only, newest
    # attempt only, nothing skipped. Counting raw rows here put "71 of 78 scored
    # tasks passed" in the footer under a headline of 95.9% on the same page,
    # because retired tasks and superseded attempts were still in the file.
    live_ids = {t.id for t in tasks}
    newest = {}
    for r in rows:
        if r["task_id"] in live_ids:
            newest[r["task_id"]] = r
    counted = [r for r in newest.values() if not r.get("skipped")]
    scored_n = len(counted)
    passed_n = sum(1 for r in counted if r["passed"])
    turns = [r["turns"] for r in counted]
    secs = [r["seconds"] for r in counted]
    avg_turns = round(sum(turns) / len(turns), 1) if turns else 0
    avg_secs = round(sum(secs) / len(secs)) if secs else 0

    page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Blolabel &mdash; the iOS agent benchmark</title>
<meta name="description" content="A benchmark for AI agents that operate a real iPhone. Real device, real apps, graded by the phone itself.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700&display=swap">
<link rel="icon" href="{LOGO_URI}">
<link rel="stylesheet" href="/style.css">
</head><body>

<header><div class="wrap bar">
  <a href="/" aria-label="Blolabel"><img src="{LOGO_URI}" alt="Blolabel"></a>
  <nav>
    <a href="#finding">The result</a>
    <a href="#method">Method</a>
    <a href="#leaderboard">Leaderboard</a>
     <a href="/blog/">Notes</a>
     <a href="https://huggingface.co/datasets/blolabel/phoneshell-bench">Dataset</a>
    <a href="#tasks">Tasks</a>
    <a href="#run">Run it</a>
  </nav>
</div></header>

<div class="hero"><div class="wrap inner">
  <span class="eyebrow">pre-registered &middot; {exp['runs']} paired runs &middot; physical iPhone, iOS 26</span>
  <h1>Vision Is Worth<br><span class="grad">100 Points Or Zero.</span><br>Never In Between.</h1>
  <p class="lede">We gave three frontier models the same task on a real iPhone, once with a
     screenshot and once without, and varied only where the answer lived. When it was in the
     accessibility tree the picture changed nothing. When it existed only as pixels, the picture
     was the entire task.</p>
  <a class="cta" href="#finding">See the result &rarr;</a>
  <div class="dots">
    <span><i style="background:var(--emerald)"></i>{exp['render']['with']}/{exp['render']['n']} with the image</span>
    <span><i style="background:var(--red)"></i>{exp['render']['without']}/{exp['render']['n']} without it</span>
    <span><i style="background:var(--blue)"></i>p &lt; 0.0001</span>
  </div>
</div></div>

<div class="strip">
  <div class="hi"><b class="num">{100 * exp['render']['with'] // exp['render']['n']}%</b><span>with the screenshot</span></div>
  <div><b class="num">{100 * exp['render']['without'] // exp['render']['n']}%</b><span>without it</span></div>
  <div><b class="num">{exp['runs']}</b><span>paired runs</span></div>
  <div><b class="num">{len(exp['models'])}</b><span>frontier models</span></div>
  <div><b class="num">+{exp['render']['tax']:.0f}%</b><span>cost of the image</span></div>
  <div><b class="num">{len(tasks)}</b><span>public tasks</span></div>
</div>

<div class="wrap">

<section id="finding">
  <div class="sh"><h2>The result</h2>
  <p>Every task was run twice by the same model, back to back, with the order alternating. The only
     thing that differed between the two arms is whether the model was shown the screen.</p></div>

  <div class="scroll"><table>
    <thead><tr><th>Where the answer lives</th><th class="r">With the image</th>
      <th class="r">Tree only</th><th class="r">McNemar exact</th></tr></thead>
    <tbody>
      <tr><td><b>Only in the pixels</b><br><span class="dim">a word drawn into a picture, no alt text</span></td>
        <td class="r num"><b>{exp['render']['with']}/{exp['render']['n']}</b><br>
          <span class="pill p-pass">{100*exp['render']['with']//exp['render']['n']}%</span></td>
        <td class="r num"><b>{exp['render']['without']}/{exp['render']['n']}</b><br>
          <span class="pill p-fail">{100*exp['render']['without']//exp['render']['n']}%</span></td>
        <td class="r num"><b>p &lt; 0.0001</b></td></tr>
      <tr><td><b>In the accessibility tree</b><br><span class="dim">the control: same page, same question</span></td>
        <td class="r num">{exp['tree']['with']}/{exp['tree']['n']}<br>
          <span class="pill p-idle">{100*exp['tree']['with']//exp['tree']['n']}%</span></td>
        <td class="r num">{exp['tree']['without']}/{exp['tree']['n']}<br>
          <span class="pill p-idle">{100*exp['tree']['without']//exp['tree']['n']}%</span></td>
        <td class="r num">p = 1.0</td></tr>
    </tbody></table></div>

  <p class="note" style="margin-top:24px"><b>The interaction is the finding, not the headline
     number.</b> The control row shows nothing at all. Same pages, same navigation, same question
     shape, same resolution, same prompt: the only difference is where the answer sits. So the
     effect cannot be an artifact of the harness, and a model that scores 100% on one row scores
     zero on the other.</p>

  <h3 style="margin:34px 0 6px">The same effect in every model</h3>
  <p class="dim" style="margin:0 0 14px;font-size:14px">This is a replication check, not a
     ranking. The task is deliberately binary, so any model that can read gets everything with the
     image and nothing without it. Identical numbers are the expected result and the point of
     them: an effect that varied by model would suggest one model&rsquo;s eyesight rather than a
     property of the information. For an actual comparison between models see the
     <a href="#leaderboard">leaderboard</a>.</p>
  <div class="scroll"><table>
    <thead><tr><th>Model</th><th class="r">With the image</th><th class="r">Tree only</th>
      <th class="r">p</th></tr></thead><tbody>
    {per_model_rows}
    </tbody></table></div>

  <div class="cards" style="margin-top:34px">
    <div class="card"><span class="n">WHAT IT COSTS</span><h3>+{exp['render']['tax']:.0f}% per task</h3>
      <p>Carrying the screenshot is a pure token tax. Step counts were unchanged either way, so
         the model does not work harder with it, only more expensively.</p></div>
    <div class="card"><span class="n">THE RULE</span><h3>Send it for rendered content</h3>
      <p>Not as a global setting. On a labelled screen the image buys nothing; on a map tile, a
         chart, a book cover or a canvas it is the whole task.</p></div>
    <div class="card"><span class="n">THE LIMIT</span><h3>This shows existence, not degree</h3>
      <p>A binary task proves the effect crisply and says nothing about size. On a realistic screen
         the answer is rarely wholly present or wholly absent, and how much vision is worth in
         between is not measured here.</p></div>
    <div class="card"><span class="n">THE CATCH</span><h3>Apple labels its own apps well</h3>
      <p>Across thirteen first-party screens, eleven were fully described by their tree. Third-party
         apps are a different story: one chat app returns
         <code>WAMessageBubbleTableViewCell</code> where a button name should be.</p></div>
  </div>
</section>

<section id="method">
  <div class="sh"><h2>Why you can believe it</h2>
  <p>An earlier version of this experiment reported that vision was worth nothing, and it was wrong.
     The image had been downscaled to 235&times;512, so the vision arm was never a test of vision.
     That retraction is why the method below exists.</p></div>
  <div class="cards">
    <div class="card"><span class="n">01 PRE-REGISTERED</span><h3>The analysis was fixed first</h3>
      <p>The design, the primary endpoint, the minimum detectable effect and the falsification
         condition were committed to git before the run. The sequence is auditable.</p></div>
    <div class="card"><span class="n">02 CONTROLLED STIMULUS</span><h3>Not found, built</h3>
      <p>Ten pages, each with one word in HTML and another rendered into an image. Verified on the
         device: 0 of 10 image words appear anywhere in the tree, 10 of 10 written words do.</p></div>
    <div class="card"><span class="n">03 PAIRED AND INTERLEAVED</span><h3>Drift cancels</h3>
      <p>Each task runs both ways back to back with the order alternating, so device state cannot
         load onto one arm. A real phone changes underneath you.</p></div>
  </div>
</section>

<section id="leaderboard">
  <div class="sh"><h2>Leaderboard</h2>
  <p>Every task runs on a physical iPhone. Nothing is simulated, and no human decides whether a
     run passed.</p></div>
  {leaderboard}
  {board_note}
  <p class="note" style="margin-top:22px">A task passes only if the phone itself ends in the
     required state. What the agent <em>says</em> it did counts for nothing: an agent that reports
     &ldquo;I have enabled that setting&rdquo; and an agent that enabled it are different things,
     and only the second one passes.</p>
</section>

<section id="capabilities">
  <div class="sh"><h2>What it measures</h2>
  <p>A pass rate says how often a model succeeded. This says at what. Each capability is isolated
     by at least one short task, so a failure names a missing skill instead of pointing vaguely at
     a long task that went wrong somewhere in the middle.</p></div>
  <div class="scroll">{capability_table}</div>
</section>

<section id="anatomy">
  <div class="sh"><h2>How a task works</h2>
  <p>Three parts. The first and the third involve no model at all, which is what makes a score
     reproducible.</p></div>
  <div class="cards">
    <div class="card"><span class="n">01 SETUP</span><h3>Put the phone in a known state</h3>
      <p>Deterministic steps, no model involved, so every attempt starts identically.</p></div>
    <div class="card"><span class="n">02 INSTRUCTION</span><h3>Hand over one sentence</h3>
      <p>The agent sees an accessibility tree and a screenshot, and acts with taps, typing,
         gestures and picker wheels.</p></div>
    <div class="card"><span class="n">03 CHECKS</span><h3>Ask the phone, not the agent</h3>
      <p>Assertions read the device directly and decide pass or fail. The agent never sees them.</p></div>
  </div>
  <p style="margin:26px 0 14px;color:var(--muted)">A complete task, exactly as it is stored in the
     repository:</p>
  <pre><code>{_esc(example_yaml)}</code></pre>
</section>

<section id="tasks">
  <div class="sh"><h2>The {len(tasks)} public tasks</h2>
  <p>{counts.get('easy',0)} easy, {counts.get('medium',0)} medium, {counts.get('hard',0)} hard, all
     against Apple&rsquo;s own applications: Settings, Safari, Notes, Reminders, Clock, Calculator,
     Contacts, Maps, Calendar, Files, Books, Compass, Shortcuts, Voice Memos and the home screen
     itself. No third-party app is automated, so no third party&rsquo;s terms are involved.</p>
  <p class="note">These are {len(tasks)} of {n_all}. {n_all - len(tasks)} are held back and are in
     neither the public repository nor its history: a benchmark whose entire answer key is public
     becomes training data, and the score then measures memorisation rather than capability. The
     public {len(tasks)} cover all {n_caps} capabilities, so a score over them is comparable between
     models and you can run the whole thing today. <b>Steps and time</b> in the table come from the
     single-model reference run; <b>solved by</b> comes from the {n_models}-model sweep, which has
     so far covered {len(per_task)} of these tasks.</p></div>
  <div class="scroll">{task_table}</div>
</section>

<section id="run">
  <div class="sh"><h2>Run it yourself</h2>
  <p>A Mac, an iPhone, a cable and an Apple developer account. The harness builds and installs the
     automation runner onto the phone for you.</p></div>
  <pre><code>git clone https://github.com/Blomega/phoneshell
cd phoneshell
bin/phoneshell doctor      # tells you exactly what is missing
bin/phoneshell setup       # builds and installs the runner on your iPhone
bin/phoneshell up          # brings the bridge up

bin/phoneshell bench --list
bin/phoneshell bench --model claude-sonnet-5</code></pre>
  <p class="note" style="margin-top:22px">Or skip the hardware entirely. Every task definition and
     every scored run on this page is published as a dataset at
     <a href="https://huggingface.co/datasets/blolabel/phoneshell-bench">
     huggingface.co/datasets/blolabel/phoneshell-bench</a>, including the paired runs behind the
     result above, so the statistics can be recomputed from source rather than taken on trust.</p>
</section>

<section>
  <div class="sh"><h2>Who this is for</h2></div>
  <div class="cards">
    <div class="card"><h3>Teams running agents against real iPhones</h3>
      <p>The harness is free and open. What breaks it is not your code: it is an iOS point release,
         an Xcode update or a change in WebDriverAgent, any of which can stop the bridge working on
         a Tuesday morning. We keep a tested build working and there is a person to call.</p></div>
    <div class="card"><h3>Flows a device farm cannot run</h3>
      <p>Cloud farms give you clean, ephemeral phones. Some things need a real handset: an OTP to a
         real SIM, a payment through a local wallet, region-locked content, an account with history
         behind it.</p></div>
    <div class="card"><h3>Labs training phone-use models</h3>
      <p>Every published mobile-agent number is Android, because Android runs in software and iOS
         does not. If you want your model measured on iOS rather than assumed to transfer, the suite
         above is free to run today.</p></div>
  </div>
</section>

</div>

<footer><div class="wrap">
  Built on phoneshell, an open harness for driving a real iPhone from a Mac.
  Results generated {time.strftime('%d %B %Y', time.gmtime())} from run <code>{_esc(run_name)}</code>
  on {device_line}: {passed_n} of {scored_n} scored tasks passed,
  averaging {avg_turns} steps and {avg_secs} seconds each.
  Tasks the model was never asked, because of a rate limit or because the app was not installed,
  are shown as not scored rather than counted as failures.
</div></footer>

<div id="cookie"><div class="wrap in">
  <div>
    <h3>&#127850; Blolabel uses cookies</h3>
    <p>We use cookies to enhance your browsing experience and provide essential functionality.
       You can customize which cookies you allow us to use.</p>
  </div>
  <div class="btns">
    <button data-consent="essential">Essential Only</button>
    <button class="solid" data-consent="all">Accept All</button>
  </div>
</div></div>

<script>{SCRIPT}</script>
</body></html>"""
    out = SITE / "index.html"
    out.write_text(page)
    # Publish the measurements, not the transcript. `agent_said` is free text the
    # model wrote about a REAL phone, and it duly published the owner's wifi SSID
    # and home city. The structured result is what makes a benchmark checkable;
    # the commentary adds nothing and cannot be vetted line by line.
    public_fields = ("task_id", "model", "passed", "skipped", "skip_reason",
                     "turns", "seconds", "cost_usd", "checks", "scaffold", "started_at")
    public_rows = [{k: r[k] for k in public_fields if k in r} for r in newest.values()]
    (SITE / "results.json").write_text(
        json.dumps({"board": board, "runs": public_rows}, indent=1))
    return out
