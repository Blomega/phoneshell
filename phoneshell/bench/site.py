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
  --emerald:#10b981; --red:#ef4444;
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
.bar{display:flex;align-items:center;gap:28px;height:64px}
.bar img{height:26px;width:auto;display:block}
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


def _esc(text: str) -> str:
    return html.escape(str(text))


def build(run_name: str = "v1") -> Path:
    # include_private=False: the held-out tasks must not be published, not even
    # their ids and instructions.
    tasks = load_all(ROOT / "environments", include_private=False)
    board = score(run_name, include_private=False)
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
    # Newest attempt wins: a task re-run after a broken check was corrected must
    # not be reported by the result that broken check produced.
    results_by_task = {r["task_id"]: r for r in rows}
    tt = ["<table><thead><tr><th>Task</th><th>App</th><th>Difficulty</th>"
          "<th class='num'>Checks</th><th class='num'>Steps</th><th class='num'>Time</th>"
          "<th class='num'>Result</th></tr></thead><tbody>"]
    for t in tasks:
        r = results_by_task.get(t.id)
        steps = took = "<span class='dim'>-</span>"
        if r is None:
            verdict = "<span class='dim'>not run</span>"
        elif r.get("skipped"):
            # Never scored: the model was not asked, or the app is not on the
            # phone. Reporting these as failures would blame the model for the
            # device, which is how a benchmark starts lying.
            verdict = f"<span class='dim' title='{_esc(str(r.get('skip_reason',''))[:120])}'>not scored</span>"
        else:
            verdict = "<span class='pass'>pass</span>" if r["passed"] else "<span class='fail'>fail</span>"
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
    <a href="#leaderboard">Leaderboard</a>
    <a href="#capabilities">Capabilities</a>
    <a href="#tasks">Tasks</a>
    <a href="#method">Method</a>
    <a href="#run">Run it</a>
  </nav>
</div></header>

<div class="hero"><div class="wrap inner">
  <span class="eyebrow">iOS 26 &middot; physical iPhone &middot; graded by the device</span>
  <h1>Agents Are Measured<br><span class="grad">On Android.</span><br>We Measure iOS.</h1>
  <p class="lede">Android runs in software, free, thousands of phones at once, so that is where
     every published number comes from. An iPhone cannot. It needs real hardware, a Mac, a signed
     developer build and a harness that does not lie to you. So nobody measures it.</p>
  <a class="cta" href="#leaderboard">See the results &rarr;</a>
  <div class="dots">
    <span><i style="background:var(--emerald)"></i>{scored_n} of {len(tasks)} tasks scored</span>
    <span><i style="background:var(--blue)"></i>iPhone 17 Pro Max, iOS 26.6</span>
    <span><i style="background:var(--purple)"></i>0 humans grading</span>
  </div>
</div></div>

<div class="strip">
  <div class="hi"><b>{('&mdash;' if best is None else str(best) + '%')}</b><span>best score</span></div>
  <div><b class="num">{len(tasks)}</b><span>tasks</span></div>
  <div><b class="num">{probes}</b><span>capability probes</span></div>
  <div><b class="num">{jobs}</b><span>end-to-end jobs</span></div>
  <div><b class="num">{total_checks}</b><span>automatic checks</span></div>
  <div><b class="num">{apps}</b><span>Apple apps</span></div>
</div>

<div class="wrap">

<section id="leaderboard">
  <div class="sh"><h2>Leaderboard</h2>
  <p>Every task runs on a physical iPhone. Nothing is simulated, and no human decides whether a
     run passed.</p></div>
  {leaderboard}
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

<section id="method">
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
  <div class="sh"><h2>The {len(tasks)} tasks</h2>
  <p>{counts.get('easy',0)} easy, {counts.get('medium',0)} medium, {counts.get('hard',0)} hard, all
     against Apple&rsquo;s own applications: Settings, Safari, Notes, Reminders, Clock, Calculator,
     Contacts, Maps, Calendar, Files, Books, Compass, Shortcuts, Voice Memos and the home screen
     itself. No third-party app is automated, so no third party&rsquo;s terms are involved.</p></div>
  <div class="scroll">{task_table}</div>
</section>

<section id="run">
  <div class="sh"><h2>Run it yourself</h2>
  <p>A Mac, an iPhone, a cable and an Apple developer account. The harness builds and installs the
     automation runner onto the phone for you.</p></div>
  <pre><code>git clone https://github.com/blomega/phoneshell
cd phoneshell
bin/phoneshell doctor      # tells you exactly what is missing
bin/phoneshell setup       # builds and installs the runner on your iPhone
bin/phoneshell up          # brings the bridge up

bin/phoneshell bench --list
bin/phoneshell bench --model claude-sonnet-5</code></pre>
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
  on a physical iPhone 17 Pro Max running iOS 26.6: {passed_n} of {scored_n} scored tasks passed,
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
