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

ROOT = Path(__file__).resolve().parent.parent.parent
SITE = ROOT / "site"

MODEL_LABELS = {
    "claude-sonnet-5": "Claude Sonnet 5",
    "claude-opus-5": "Claude Opus 5",
    "claude-haiku-4-5-20251001": "Claude Haiku 4.5",
    "gpt-5": "GPT-5",
    "gemini-3-pro": "Gemini 3 Pro",
}

CSS = """
:root{--bg:#0b0d10;--panel:#13161b;--line:#232830;--text:#e9edf4;--dim:#8d96a5;
      --accent:#5b8cff;--ok:#3ddc84;--warn:#ffb020;--err:#ff5c5c;
      --mono:ui-monospace,SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
     font:16px/1.65 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;
     -webkit-font-smoothing:antialiased}
.wrap{max-width:940px;margin:0 auto;padding:0 24px}
header{border-bottom:1px solid var(--line);padding:22px 0;position:sticky;top:0;
       background:rgba(11,13,16,.86);backdrop-filter:blur(10px);z-index:5}
header .wrap{display:flex;align-items:center;gap:14px}
.logo{font-weight:700;letter-spacing:-.2px}
.logo span{color:var(--accent)}
nav{margin-left:auto;display:flex;gap:20px;font-size:14px}
nav a{color:var(--dim);text-decoration:none}
nav a:hover{color:var(--text)}
h1{font-size:clamp(30px,5vw,46px);line-height:1.12;letter-spacing:-1px;margin:56px 0 18px}
h2{font-size:24px;letter-spacing:-.4px;margin:56px 0 14px;padding-top:8px}
h3{font-size:17px;margin:28px 0 8px}
p{color:#c6ccd8;margin:0 0 16px}
.lede{font-size:19px;color:#cfd6e2;max-width:70ch}
.dim{color:var(--dim)}
.tag{display:inline-block;font:12px/1 var(--mono);color:var(--accent);
     border:1px solid #26406f;background:#111a2e;border-radius:99px;padding:6px 11px;margin-bottom:20px}
table{width:100%;border-collapse:collapse;margin:18px 0;font-size:15px}
th,td{text-align:left;padding:11px 12px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:600;font-size:13px;text-transform:uppercase;letter-spacing:.4px}
td.num,th.num{text-align:right;font-family:var(--mono)}
tr:hover td{background:#10141a}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin:16px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin:22px 0}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px}
.stat b{display:block;font-size:30px;font-family:var(--mono);letter-spacing:-1px}
.stat span{color:var(--dim);font-size:13px}
pre{background:#0e1115;border:1px solid var(--line);border-radius:10px;padding:16px;overflow-x:auto;
    font:13px/1.55 var(--mono);color:#cfe0ff}
code{font:13px var(--mono);background:#161a20;border:1px solid var(--line);
     border-radius:5px;padding:1px 5px;color:#d7e2f5}
pre code{background:none;border:none;padding:0}
.pass{color:var(--ok)} .fail{color:var(--err)} .mid{color:var(--warn)}
.bar{height:6px;border-radius:99px;background:#1d222a;overflow:hidden;min-width:90px}
.bar i{display:block;height:100%;background:var(--accent)}
footer{border-top:1px solid var(--line);margin-top:72px;padding:28px 0 60px;color:var(--dim);font-size:14px}
ul{color:#c6ccd8}
li{margin-bottom:7px}
.muted-note{font-size:14px;color:var(--dim);border-left:2px solid var(--line);padding-left:14px;margin:18px 0}
@media(max-width:640px){.wrap{padding:0 16px}nav{display:none}}
"""


def _esc(text: str) -> str:
    return html.escape(str(text))


def build(run_name: str = "v1") -> Path:
    tasks = load_all(ROOT / "environments")
    board = score(run_name)
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
            lb.append(
                f"<tr><td>{_esc(label)}</td>"
                f"<td class='num'><b>{r['pass_rate']}%</b><div class='bar'>"
                f"<i style='width:{r['pass_rate']}%'></i></div></td>"
                f"<td class='num'>{r['passed']}/{r['tasks']}</td>"
                f"<td class='num'>{r['avg_turns']}</td>"
                f"<td class='num'>{r['avg_seconds']:.0f}s</td>"
                f"<td class='num'>${per:.3f}</td></tr>"
            )
        lb.append("</tbody></table>")
        leaderboard = "\n".join(lb)
    else:
        leaderboard = "<p class='dim'>No scored run yet.</p>"

    # ---------------------------------------------------------- task table
    results_by_task = {r["task_id"]: r for r in rows}
    tt = ["<table><thead><tr><th>Task</th><th>App</th><th>Difficulty</th>"
          "<th>Checks</th><th class='num'>Result</th></tr></thead><tbody>"]
    for t in tasks:
        r = results_by_task.get(t.id)
        if r is None:
            verdict = "<span class='dim'>not run</span>"
        else:
            verdict = "<span class='pass'>pass</span>" if r["passed"] else "<span class='fail'>fail</span>"
        app = (t.app or "").rsplit(".", 1)[-1] or "system"
        tt.append(
            f"<tr><td><code>{_esc(t.id)}</code><br><span class='dim'>{_esc(t.instruction[:88])}</span></td>"
            f"<td>{_esc(app)}</td><td>{_esc(t.difficulty)}</td>"
            f"<td class='num'>{len(t.checks)}</td><td class='num'>{verdict}</td></tr>"
        )
    tt.append("</tbody></table>")
    task_table = "\n".join(tt)

    # ---------------------------------------------------------- example task
    example = by_id.get("calculator.arithmetic") or tasks[0]
    example_yaml = example.source.read_text() if example.source else ""

    counts = {"easy": 0, "medium": 0, "hard": 0}
    for t in tasks:
        counts[t.difficulty] = counts.get(t.difficulty, 0) + 1
    total_checks = sum(len(t.checks) for t in tasks)
    best = max(board.values(), key=lambda r: r["pass_rate"])["pass_rate"] if board else None

    page = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>blolabel — the iOS agent benchmark</title>
<meta name="description" content="A benchmark for AI agents that operate a real iPhone. Real device, real apps, automatic verification.">
<link rel="stylesheet" href="/style.css">
</head><body>
<header><div class="wrap">
  <div class="logo">blo<span>label</span></div>
  <nav>
    <a href="#leaderboard">Leaderboard</a><a href="#how">How it works</a>
    <a href="#tasks">Tasks</a><a href="#run">Run it</a><a href="#labs">For labs</a>
  </nav>
</div></header>

<div class="wrap">
  <span class="tag">iOS · real device · automatic verification</span>
  <h1>Can an AI agent actually use an iPhone?</h1>
  <p class="lede">
    Agents that operate computers are measured on desktops and on Android, because both can be
    run in software, thousands at a time, for free. An iPhone cannot. It needs a physical device,
    a Mac, a signed developer build and a harness that does not lie to you.
    So nobody measures it. This is the missing benchmark.
  </p>

  <div class="grid">
    <div class="stat"><b>{len(tasks)}</b><span>tasks on a real iPhone</span></div>
    <div class="stat"><b>{total_checks}</b><span>automatic checks</span></div>
    <div class="stat"><b>{'—' if best is None else str(best) + '%'}</b><span>best score so far</span></div>
    <div class="stat"><b>0</b><span>humans grading</span></div>
  </div>

  <h2 id="leaderboard">Leaderboard</h2>
  <p>Every task is run on a physical iPhone running iOS 26. Nothing is simulated, and no human
     decides whether a run passed.</p>
  {leaderboard}
  <div class="muted-note">
    A task passes only if the phone itself ends in the required state. What the agent says it did
    counts for nothing: an agent that reports "I have enabled that setting" and an agent that
    enabled it are different things, and only the second one passes.
  </div>

  <h2 id="how">How it works</h2>
  <p>Each task is three parts. The first and third involve no model at all, which is what makes a
     score reproducible.</p>
  <div class="card">
    <h3>1. Setup</h3>
    <p class="dim">Deterministic steps put the phone in a known state, so every attempt starts identically.</p>
    <h3>2. Instruction</h3>
    <p class="dim">One sentence is handed to the agent. It sees the phone through an accessibility
       tree and a screenshot, and acts with taps, typing and gestures.</p>
    <h3>3. Checks</h3>
    <p class="dim">Assertions read the phone directly and decide pass or fail. The agent never sees them.</p>
  </div>
  <p>A complete task, exactly as it is stored in the repository:</p>
  <pre><code>{_esc(example_yaml)}</code></pre>

  <h2 id="tasks">The tasks</h2>
  <p>{counts.get('easy',0)} easy, {counts.get('medium',0)} medium, {counts.get('hard',0)} hard.
     All of them run against Apple's own applications: Settings, Safari, Notes, Reminders, Clock,
     Calculator, Weather, Contacts and the home screen itself. No third-party app is automated,
     so no third party's terms are involved.</p>
  {task_table}

  <h2 id="run">Run it yourself</h2>
  <p>You need a Mac, an iPhone, a cable and an Apple developer account. The harness builds and
     installs the automation runner onto the phone for you.</p>
  <pre><code>git clone https://github.com/&lt;org&gt;/phoneshell
cd phoneshell
bin/phoneshell doctor      # tells you exactly what is missing
bin/phoneshell setup       # builds and installs the runner on your iPhone
bin/phoneshell up          # brings the bridge up

bin/phoneshell bench --list
bin/phoneshell bench --model claude-sonnet-5</code></pre>

  <h2 id="labs">Who this is for</h2>

  <h3>Teams running agents against real iPhones</h3>
  <p>The harness is free and open, forever. What breaks it is not your code: it is an iOS point
     release, an Xcode update, or a change in WebDriverAgent, any of which can stop the bridge
     working on a Tuesday morning. We keep a tested build working and there is a person to call.
     That is what is sold, and nothing else is.</p>

  <h3>Teams whose flows a device farm cannot run</h3>
  <p>Cloud device farms give you clean, ephemeral phones. Some things cannot be tested on one: an
     OTP to a real SIM, a payment through a local wallet, region-locked content, an account with
     real history behind it. Those run on a real handset or they do not run. If that is your
     release checklist, this is built for exactly that.</p>

  <h3>Labs training phone-use models</h3>
  <p>Every published mobile-agent number is Android, because Android runs in software and iOS does
     not. If you want your model measured on iOS instead of assumed to transfer, the suite above is
     free to run today, and held-out tasks and scored runs on real hardware can be arranged.</p>

  <footer>
    Built on phoneshell, an open harness for driving a real iPhone from a Mac, Apache-2.0.
    Results generated {time.strftime('%d %B %Y', time.gmtime())} from run <code>{_esc(run_name)}</code>
    on a physical iPhone 17 Pro Max running iOS 26.6. Tasks the model was never asked, because of a
    rate limit or a broken bridge, are excluded from scoring rather than counted as failures.
  </footer>
</div>
</body></html>"""
    out = SITE / "index.html"
    out.write_text(page)
    (SITE / "results.json").write_text(json.dumps({"board": board, "runs": rows}, indent=1))
    return out
