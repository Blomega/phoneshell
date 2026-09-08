# Experiment 2: what is the screenshot worth on iOS?

Written **before** the run, so the analysis cannot be chosen after seeing the data.

Experiment 1 (`FINDINGS.md` §23) reported that vision was worth zero points. That result does not
stand, for four reasons, and this design exists to fix each one.

---

## Why experiment 1 failed

| # | fault | consequence |
| --- | --- | --- |
| 1 | The image was downscaled to **235x512**, 18% of native width | The vision arm was never a test of vision. Text on that image is unreadable. This alone invalidates the headline. |
| 2 | 24 tasks, **12 passed by every model** | Only 10 items discriminated. The instrument was half at ceiling. |
| 3 | One run per condition | No variance, no confidence interval. |
| 4 | Conditions run in blocks, hours apart | Device drift is confounded with condition. |

The statistics were also never computed. Done properly they say: 2 discordant pairs, one each way,
**McNemar exact p = 1.000**, and a 95% CI on 83.3% of **64% to 93%**. That is *no evidence of a
difference*, not *evidence of no difference*. The design could not have detected an effect under
about 29 points.

---

## The question

> On a physical iPhone, does giving an agent a legible screenshot alongside the accessibility tree
> change its task success rate, compared with the tree alone?

### Primary endpoint

Paired per-task pass/fail for the **same model** under two conditions, pooled across models,
tested with **McNemar's exact test**. Significance at p < 0.05, reported with the odds ratio and a
95% CI on the paired difference.

### Secondary endpoints

Steps per task, wall-clock seconds, and cost per task. These are reported with means and bootstrap
CIs and are explicitly *not* the primary claim.

---

## The design decision that matters most

**Testing vision on tasks that do not need vision cannot answer the question.**

Every task in experiment 1 ran against Apple's own apps, where every control carries a proper
accessibility label. On such a screen the tree is a complete description and the image is by
construction redundant. Finding "no effect" there is close to a tautology.

So the task set is rebuilt around a deliberate split:

| stratum | n | what it requires |
| --- | --- | --- |
| **tree-sufficient** | ~15 | Everything needed is a labelled control. The control stratum. |
| **render-dependent** | ~15 | The answer exists only as *pixels*: a value drawn rather than labelled, an unlabelled glyph, a colour or state shown visually, relative position, a count on a badge. |
| **spatial** | ~10 | Requires reasoning about layout: which of several identical controls, what is above/below something, what is partially obscured. |
| **known-hard** | ~10 | Currently unsolved or discriminating, kept so the set retains difficulty. |

If vision matters anywhere it will show in the render-dependent and spatial strata, and the
tree-sufficient stratum is the control that proves the effect is not an artifact of the harness.
**Reporting the strata separately is part of the pre-registration**, not a post-hoc slice.

Tasks passed by every model in experiment 1 are retired from the scored set. An item everyone
answers correctly carries no information.

---

## Design

* **Within-subject, paired.** Every task is run by the same model twice: `image` and `no-image`.
* **Interleaved, not blocked.** Order is `task -> condition A -> condition B` back to back, so
  device state drift affects both arms equally. Experiment 1 ran the arms hours apart.
* **Three vision-capable models**: `openai/gpt-5.1`, `google/gemini-3.1-pro-preview`,
  `moonshotai/kimi-k3`. Three independent within-model ablations, not one.
* **Two text-only models** (`qwen/qwen3-max`, `deepseek/deepseek-v3.2`) run once as an external
  reference point. They are not part of the paired test.

### Image condition

Long edge **1536px** (from 512). That is inside every provider's useful range and makes on-screen
text legible, which is the entire point. **The harness asserts the delivered image size at run
start and refuses to run if it is under 1200px**, because that is exactly the failure that
invalidated experiment 1.

Only the **last 3 images** are kept in the message history; older ones are replaced by a text
placeholder. Without this a 25-step task re-sends every screenshot on every turn, which is
quadratic in cost and will silently blow the context window on long tasks.

---

## Power

McNemar's power depends on the number of discordant pairs, not the sample size. To detect a
2:1 asymmetry with 80% power at p<0.05 needs roughly **25 discordant pairs**.

Assuming ~20% discordance on a discriminating task set:

```
50 tasks x 3 models = 150 paired observations
150 x 0.20          = 30 discordant pairs      -> adequately powered
```

Experiment 1 had **2**.

**Minimum detectable effect is stated in advance: about 8 percentage points.** An effect smaller
than that will be reported as "not detected at this sample size", never as "no effect".

---

## Every failure mode, and what stops it recurring

| risk | mitigation | verified by |
| --- | --- | --- |
| Image silently downscaled | assert delivered size >= 1200px before the run starts | pilot prints the actual size |
| A flag default overriding per-model detection | vision is tri-state (`None` = per model); the pilot runs one text-only and one vision model | pilot |
| Media type mismatch (jpeg declared as png) | real media type carried from the observation | already fixed, §23 |
| Image inside a tool result | image sent as its own user turn | already fixed, §23 |
| Context/cost blowup from image history | keep only the last 3 images | pilot measures cost per task |
| Provider rate limits read as task failures | API errors are recorded as `skipped`, never as a failed task | runner already distinguishes these |
| Phone locks mid-run | **passcode must be in the Keychain before starting** | hard gate, run refuses to start |
| Clock tasks measuring alarm-list length | delete the 617 residual alarms first | Clock tree read must be under 0.5s |
| Device left dirty between tasks | setup terminates and homes; teardown cleans alarms | existing |
| Task that times out scoring a pass | already fixed | §21 |
| Choosing the analysis after seeing data | this document, written first | git history |

---

## Stopping rules

* If the pilot shows cost per task above **$0.15** for any model, that model is dropped and the
  reason recorded, rather than quietly overspending.
* If a model errors on more than 20% of its tasks, its results are excluded as a harness problem,
  not reported as a low score.
* The run aborts if the input check fails, if the phone locks and cannot be unlocked, or if the
  delivered image is under 1200px.

## What would falsify the hypothesis

If vision genuinely helps, the render-dependent stratum shows a positive paired difference that
survives McNemar at p<0.05 while the tree-sufficient stratum shows none. **If both strata show
nothing, the honest conclusion is that a well-labelled accessibility tree makes the screenshot
redundant on iOS**, and that conclusion is then worth publishing because it was properly powered
and properly scoped.
