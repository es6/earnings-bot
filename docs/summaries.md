# Summary research and shared rules

The November 11 prototype is research, not a deployed automatic summary system. The bot
still posts the release immediately; summary generation is a separate optional follow-up.
No provider is enabled and no API call is made by the offline evaluation commands.

## What the feedback changed

The seven blind rounds informed a shared policy in `summary_policy.py`. The live writer's
prompt imports it; future structured experiments must import it too. Sonnet low and Muse
Ember low are the default comparison pair. Haiku is opt-in for specific cases; Opus and
Spark are excluded from the default writer/comparison. Adding a model requires a new
blind evaluation, not an automatic failover that changes the quality or cost silently.

| Feedback family | General rule | Enforcement |
|---|---|---|
| EPS versus operating profit/EBITDA | Choose the relevant profit measure from business context; no blanket EPS | Shared prompt; selection still needs evaluation |
| Missing segments, subscription/fee streams, growth engines | Material revenue taxonomies and recurring streams; preserve scope | Shared prompt; never complete segments by finding any table ending in a total |
| Missing volume, backlog, flagship growth | Preserve the leading operating KPI even without a revenue level | Shared prompt; annotated coverage evaluation |
| Software capex missed; retailer cash overemphasized | Comparable annual capex/revenue ≥15%, negative cash flow, or material current investment inflection | `cash_priority`; unknown inputs remain unknown, no sector ban |
| Missing/computed YoY | Same metric, currency, unit, basis, scope and prior-year fiscal period | `yoy`; zero/negative priors omitted, margins use bps |
| Reported versus adjusted comps; profit versus margin | Preserve table columns, exact exclusions and accounting basis | Table-parser regression; identity and arithmetic tests |
| Q and FY outlooks looking identical | Guidance period must be explicit; previous guide differs from prior-year actual | Prompt and `evidence_issues`; no year/number-based inference |
| Bare initiative names; material news omitted | Status/change/scale required; prioritize growth and cash-burn milestones | Prompt and bare-update check; importance requires review |
| One-off EPS effects, investment marks, usage milestones | Explain material distortion; evaluate relevance to this business | Conditional prompt rules, no absolute sector/metric bans |
| Too verbose | Usually 4–7 lines; combine related facts before deleting coverage | Shared prompt; presentation review |
| Call-only facts absent | Use a separately identified document when available; never invent it | Evidence checks plus human review; second-source ingestion not built here |

The code checks traceability and comparable arithmetic. They cannot prove that a cited
sentence describes the chosen metric, that its units were read correctly, or that an
initiative matters to the stock. The current live free-text writer does not yet consume
structured facts or use these arithmetic helpers. It shares the selection prompt only.
Do not describe these helpers as a complete live factual validator.

## Evaluation protocol

1. Freeze release documents, expected facts, prompt/policy version and model settings.
   Record content hashes and source URLs. Never include credentials or `.env` in artifacts.
2. Reserve entirely unseen companies and later quarters before tuning. Repeatedly reviewed
   companies are regression cases, not a holdout. Include different sectors, currencies,
   fiscal calendars, table layouts and 6-K filers. Synthetic tests verify invariants only.
3. Score factual correctness, important-fact coverage and presentation separately. Metric
   matching must distinguish amount/margin, GAAP/adjusted, actual/guidance and Q/FY. WSE
   helps discover candidate facts; it is neither a complete nor infallible ground truth.
4. Show anonymous, shuffled outputs with the same formatter. Record the user's preference
   before revealing models. Keep feedback with the source evidence and explain which
   general rule it changes. If feedback contradicts a rule, narrow the condition rather
   than adding a ticker exception. Re-run all regression cases and the frozen holdout.
5. Measure time from usable release body to validated/rendered brief, with failures and
   retries included; report p50/p95, per-release tokens/cost and repeated-run variance.
   Do not claim faster release detection from a faster summary. The first alert must not wait.
6. Before any paid batch, state its maximum spend and reserve a hard total across both
   providers and retries. Existing prototype price tables and timing reports are historical.
   Never start paid calls merely because a key exists on the computer.

The old scratchpad evaluator rewarded some approximate metric matches and searched for
numbers anywhere in a release. Its reported recall and "unsupported" rates are useful
exploration, not a factual-accuracy guarantee. Some blind losses were formatting failures;
the seven rounds also reused examples, so they do not establish unseen-company quality.

## How much can be code-only?

Extraction from known structured tables, fiscal/basis/unit checks, arithmetic, rounding,
deduplication, compact rendering, blind comparison, scoring and regression replay can be
deterministic. These should surround any model. Feedback can be saved as general rules,
test cases and preferences; the software can apply them to new inputs.

Choosing the relevant profit measure, recognizing an unfamiliar growth engine, interpreting
a new segment taxonomy and judging a novel strategic update require contextual judgment.
A fixed ranker can be a fast fallback on supported layouts, but cannot currently promise
the same quality for arbitrary new issuers. A profile library needs research/maintenance;
a learned ranker is still a model. Use a small LLM for selection and concise wording, with
deterministic checks around it, until a frozen unseen-company evaluation proves otherwise.

## Offline commands

`scripts/summary_prepare.py NEW1 NEW2 --docs cached-docs --out requests.json` freezes full
release inputs, a shared structured schema, the policy and the Sonnet/Ember low settings.
It makes no API calls, loads no keys, and does not silently truncate long releases.
Haiku requires `--include-haiku`. Keep expected facts out of these inputs. This prepares
fact-selection experiments; it is not a paid batch runner or the final compact renderer.

`scripts/summary_eval.py --results saved.json --gold gold.json --docs cached-docs
--out replay.json --blind anonymous.md` replays saved output with the same literal
formatter for every setting. The report records input hashes, editorial coverage, missing
facts, traceability/period issues and excluded issuer/run pairs. Only common cases are
compared. The anonymous briefs have a separate `.key.json`; record preferences before
opening it. Haiku is opt-in; code-only is included as a baseline.

Do not edit source artifacts during a batch or overwrite a previous round's annotations.
Commit general rules and small synthetic regressions. Keep complete release/model-output
caches outside git; do not copy a scratchpad wholesale because it may contain API keys.

### November 11 offline audit

Replaying the saved `experiment7_all.json` against its latest annotations found 11 common
issuer/run cases. Approximate editorial coverage was Sonnet 85%, Ember 73%, code-only 53%.
This conservative matcher distinguishes amount/margin, adjusted/reported, actual/guidance,
consolidated/segment revenue and word boundaries. It can undercount legitimate synonyms;
it does not check values or whether all requested growth rates were supplied. These numbers
are neither factual-accuracy scores nor a benchmark of the new policy. The cases were
already used for feedback and are not an unseen holdout.

Both LLMs missed the annotated Airbnb nights/bookings metrics. Code-only found none of its
three must-have measures, and it incorrectly selected expense rows as Snap revenue segments.
The replay flagged eight unlabeled code-only guides, plus bare LLM business updates and a
missing source passage. The original grading's numeric-occurrence check could not establish
metric/value correctness. Review those facts against their tables before treating any
model's reported coverage as acceptable quality.
