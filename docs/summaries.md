# Summaries and shared rules

## Source references: extraction and durable runtime handoff

`source_document.py` adds a version-scoped evidence representation. Retrieval now carries
that document through the first-text/PDF-replacement path to `Summarizer.write_document`.
By default the writer delegates to the existing text request and quote checker.
`SOURCE_REFERENCED_SUMMARIES=true` separately opts into the reference request, binder
and bound renderer. It defaults off: the evaluation below does not justify activation.
Summaries remain beta. A number appearing in a legacy quote does not prove the metric
or period is correct.

`document_from_html` uses the live HTML reader and retains its plain text. It gives each
text block and table a stable local ID, scoped to a SHA-256 of the extraction version,
source and format. Cells
retain their original row/column, blank/decorative cells, declared spans, and explicit HTML
header/scope tags. No period, currency, scale, or metric labels are invented. Nested outer
layout tables are marked as such; their text is not independent table-cell evidence.
`document_from_text` supplies block references for already extracted text, including PDF
text, without inferring table cells from spacing. It preserves the original text, including
spacing, for the existing request. It does not preserve PDF table geometry. A previously
classified SEC exhibit is treated as text rather than parsed as HTML a second time.

Before a model call, the summary job transaction saves versioned extraction JSON in
`summary_documents`, keyed by channel, release and document hash, and records that hash
on the job. Earlier versions stay addressable through `DeliveryStore.summary_document`
after a PDF/HTML replacement; retrying the same version cannot overwrite its evidence.
`SourceDocument.from_json` restores immutable blocks and cells and rejects unknown codec
versions. Legacy jobs have no saved document; their delivered/written states and results
continue to prevent repeat paid calls. A new extraction version changes the source hash.

Resolve a reference using both the document hash and local ID. A missing ID or a different
document version fails explicitly. The reference verifier must additionally check cell
headers, metric, period/duration, basis, units and scale against this observed source
structure. Merely returning an existing cell ID will not establish claim correctness.

## Item 6 unverified candidate schema

`summary_evidence.py` defines an immutable `UnverifiedBrief`. Each fact has separate
current actual, prior-year actual, reported growth, comparable growth, previous guidance
and new guidance slots. Unavailable values are null. Each present value carries a signed
decimal string, optional range endpoint, scale, explicit unit/currency, bounds or
approximation, period, duration, basis and material comparison qualifier. Updates carry
their own supported text and citations. Schema validation rejects malformed numbers,
reversed ranges, missing units, unknown fields and unsourced values or updates.

`parse_referenced_brief` checks reference integrity: the exact document version, a real
paragraph/table ID, cells belonging to that table, and a quote local to that block. It
rejects outer nested layout tables as independent bindings. It does **not** establish
that the metric, period, basis, unit or number match those cells. A wrong-metric or
wrong-period candidate with existing references can still pass this guard; tests record
that boundary explicitly. Candidates cannot be passed directly to the live renderer.

`referenced_request` serializes reading order and observed table geometry with compact
cell arrays. The schema is stable across reports; document-specific hashes/IDs are input
data, never per-report enum/const definitions. It keeps Ember low and the existing
selection policy. It rejects plain text above 200,000 characters or annotated input
above 400,000 characters without truncation. Those are input guards, not editorial
length limits or a guarantee about token count/cost. Both source text and advisory
issuer context are data; issuer context cannot supply evidence.

Prepare requests without credentials or API calls:

```powershell
python scripts/summary_prepare.py ADBE CMG GE JPM PLTR WMT --docs <saved-docs> --out requests.json --references
```

The preparer uses a saved `<ticker>.html` when present, otherwise plain blocks from the
JSON `release` text. It never infers PDF cells. Reference-schema preparation permits only
Ember; `--include-haiku` is rejected with `--references`. The opt-in writer uses the same
request and source-binding code. Default activation remains held for final coverage and
broader factual auditing; see the [frozen comparison](evaluation-2026-11-13.md).

The schema follows the raw `output_config.format` shape and uses the SDK's schema
transformation for unsupported constraints; local Pydantic validation retains those
constraints. See [Anthropic structured output documentation](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
and [Meta structured output documentation](https://dev.meta.ai/docs/structured-output).
Ember-low provider calls established wire compatibility. That does not establish model
quality or a latency improvement.

## Item 6 binding checks and limits

Item 6 also has a conservative binding library in `summary_binding.py` and
`binding_text.py`. `bind_brief` resolves the candidate's evidence first, then checks
metric identity against the source row or claim clause; table-column period/duration;
numeric amount, sign, scale and dimension; accounting basis; currency and physical
units; approximation/bounds; segment section; and actual/growth/old/new-guidance status.
Comparative values with unlabeled columns are held back. Source-local headers override
document context; another table's rows cannot supply units or period labels. Ambiguous
or unsupported interpretations are rejected with per-value reasons. Optional rejected
comparisons do not discard a separately bound current actual. Prior-year/guidance pairs
must also be comparable. Updates must be exact supported clauses, preserving signs and
punctuation; a label is not permission to infer an economic consequence.

This closes the two reproduced wrong-metric and wrong-quarter cases in the binding API,
with valid controls retained. It is not a universal financial-language verifier: unknown
metric aliases, incomplete headers and ambiguous context reduce retention. Saved-report
evaluation found retention too low for activation. The separately opt-in writer is
integrated; the deployed/default writer remains unchanged and summaries remain beta.

The opt-in path rejects malformed, unfinished and all-unbound candidates without falling
back to legacy validation. It uses no SDK retry. Existing durable job attempt/concurrency
and daily call limits apply, with no automatic repair request. Successful results persist
the candidate, accepted bound references, rejection reasons, model/schema/system context
and request hash in `summary_evidence`. Rows are scoped to channel, release, document and
attempt, so a later legacy attempt cannot inherit an older binding verdict. PDF and HTML
versions remain addressable after replacement. Saved delivered/written results still
resume without another model call; the journal names the validation mode.

### What a binding checks, after the first real evaluation

The first six real Ember-low answers kept no values. Their claims were mostly right, but
the checks read labels literally: "Total revenue" didn't match "record revenue of $6.76
billion", a unit of "dollars" wasn't money, and "year-over-year" read as "more than". The
binder now checks what a value asserts, and fails closed on any conflict:

- **The measure.** Its key words must name the cited row, or appear in the number's own
  clause with no other measure between them.
  - Words are stemmed, footnote digits dropped, and EPS or "earnings per share" are aliases.
  - A narrower measure doesn't bind: "subscription revenue" is not revenue, and "free
    cash flow margin" is not free cash flow.
  - A count can name what it counts after its number: "repurchased 9.5 million shares".
    So can an amount or a growth rate, when the words after "in" are exactly the claimed
    measure ("delivered $1.1 billion in revenue", "19% growth in organic sales", but not
    "$1.1 billion in revenue synergies"). Earnings per share can follow the net income it
    divides: "net income of $582 million, or $0.40 per diluted share".
  - A segment's row ("North America", "Neuromodulation") takes its measure from what the
    table says it measures: the value's column header ("Reported Net Revenues"), the nearest
    heading row above it ("Revenue:", and "Cost of revenue:" for the rows under that), or
    the sentence that introduces the table ("Net sales for the third quarter by business:").
    A label that already names the claim binds on its own.
  - An abbreviation the release defines for itself names the same thing ("Moody's Analytics
    (MA)" makes "MA Revenue" Moody's Analytics revenue). The issuer's own name or ticker is
    no part of a measure ("MCO Adjusted Diluted EPS" is adjusted diluted EPS).
  - "Net sales" and "sales", and "net operating revenues" and "net revenues", are one
    measure unless the release lists both as separate rows. "Diluted" can be left out of
    EPS.
  - "Guidance" and "growth" in a claim's name say which slot the value fills, not what it
    measures: "EPS guidance" binds to "adjusted EPS between $13.70 and $13.80".
- **The number.** Sign, scale and precision must match. "(37.5)" is negative, and "a 6.9%
  decrease" is −6.9%. A claim may round the stated number ("$6.8 billion" for $6.76
  billion), but it may not be more precise than the source.
- **The period.**
  - The column's year, quarter and duration decide. A fiscal label ("Q2 FY27") is compared
    with calendar dates ("July 31, 2026") through the fiscal offset the release itself
    states.
  - A change column is headed by the year it compares with ("3Q25 O/(U) %").
  - Running text uses the release's reporting year, the prior year for a prior-year value,
    or a year named nearby for guidance.
- **Position alone binds nothing.** A table row flattened into text ("Net income $ 1,331
  $ 1,438 $ 448 $ 421") and a "respectively" list are rejected.
- **The basis.** An adjusted measure's name ("Adjusted EPS") must match an adjusted
  source, and an adjusted source value must be claimed as adjusted.
- **Labels the source doesn't state are dropped, not shown:**
  - a non-GAAP tag only in the model's `basis` field;
  - a YoY label the clause doesn't say;
  - a currency on a count;
  - a count unit the text doesn't name;
  - a group the evidence doesn't name, such as "Profitability" or "Bookings";
  - a group that names the company itself ("MCO", "Boston Scientific") or only the fact's
    role ("guidance", "headline"). It is no segment, so it is neither required nor shown.
- **Business updates** show the source's own sentence. The model's version must agree with
  it: every number with its sign, every year, and most of its words.
- **One bad fact sinks only itself.** `bind_answer` validates each fact on its own, and an
  unresolvable reference rejects only its value.

Measured on Ember-low reference answers (values kept out of all values selected):

| Set | Releases | Kept |
|---|---|---|
| Tuning: the six frozen answers of Nov 13 | ADBE, CMG, GE, JPM, PLTR, WMT | 95/222 (43%), from 0 |
| Held out: never used to tune the binder | 16, BN and NBIS included | 113/440 (26%) |

- **Hand audit (measured):** every kept value of the six tuning releases, and those of 9 of
  the 16 held-out ones, checked against its cited evidence. No wrong number, period or
  basis was found. Two held-out values were right only by position, which is what
  produced the position rule above.
- **What the held-out batch cost:** 16 calls, $0.14 at list price, a median of 8.7 s and
  a maximum of 12.0 s. The live request takes about 5 s, so activating this request would
  delay summaries. That trade-off is for the owner to decide.
- **Where retention is still lowest:** IBKR (0/33), NET, AAPL, SNPS and NOW. The usual
  causes are tables whose column headers sit in a separate header table, and measure names
  that the release words differently.

**Nov 13, v0.21.18: measures named by their table, abbreviations and slots (measured,
offline).** The same saved answers replayed through the binder, no new model calls. Most
rejections were correct values refused by name: a segment row named only "Neuromodulation"
under a table introduced as "Net sales by business", "MA Revenue" against "Moody's Analytics
revenue", "Net sales" against "Sales in the quarter were", "EPS guidance" against "adjusted
EPS between", and groups such as "MCO" or "guidance" required as if they were segments.

| Set | Before | After |
|---|---|---|
| Held out, 16 releases (above) | 129/440 (29%) | 161/440 (37%) |
| Nov 13 unseen issuers: KO, MCO, ETN, BSX, 60 answers | 428/1,869 (23%) | 782/1,869 (42%) |
| Nov 13 development answers: ADBE, CMG, GE, JPM, PLTR, WMT | 81/184 | 82/184 |

- **Important topics kept** (the unseen study's frozen annotations, 12 per issuer and run):
  149 → 275 of 720.
- **Wrong values among them** (the study's own known-value checks): 0 of 141 before, 0 of
  268 after. Those checks cover only annotated values. Every distinct value newly kept in the
  unseen set was also read against its quote by hand; none was wrong.
- No held-out release kept fewer values. Arista's product and service revenue had passed
  only because the model's group "Revenue" stripped the word; they now bind through the
  "Revenue:" heading row above them.
- Still refused, mostly rightly: values the cited passage doesn't state (the model cited
  the wrong block), segment bullets nested under a heading in running text, and model
  paraphrases ("rolling average organic orders").
- Retention is still below what default activation needs, and the reference request's
  extra latency is unchanged. Activation stays off.

## Live summaries (v0.13.0)

`summary_bound_format.py` supplies the item 6 presentation path for `BindingReport`
objects only. It formats separate structured values, retains range endpoints, units,
currency, per-share precision and comparison qualifiers, and computes YoY only from
verified comparable exact actuals. Percentage-level changes use basis points. Zero or
negative priors, losses, bounds, ranges and approximate levels show the supported prior
instead of a precise growth calculation. Previous guidance is never treated as a prior
actual; old and new ranges retain their target period. Unsupported optional comparisons
are absent from the display. Exact supported business clauses are shown without relying
on a model-selected topic label as evidence. The existing relevance/ranking layout has
no editorial fact/word/line cap. Client integration and evaluation are still pending.

The bot posts the release immediately; the summary is a separate follow-up that never delays
the alert. When the release text arrives, `summary.py` sends Muse Ember low (Meta's Model API,
which speaks the Anthropic Messages format) the same structured request the experiments used
(`summary_request.py`). `summary_validation.py` keeps only facts whose numbers it can bind to
their own quoted passage, with matching units, period and duration; a fact whose optional
change or prior fails keeps its sourced value. `summary_format.discord_brief` lays out the
confirmed facts by period and kind, as in the reviewed Discord previews, and the card's footer
counts the facts held back. A failed or fully unconfirmed summary posts nothing, and the next
source's text can try again.

The brief puts the most important facts first, using the role the model gives each fact:

1. The quarter's headline results.
2. The outlook.
3. The quarter's other results, under "More results" when an outlook comes between them and
   the headline. They include balances at the quarter's end date, with each measure's
   segments on one line, largest first. A segment's level and its growth make one entry.
4. Results for other periods, such as year to date.
5. Business updates, under one heading, with their own period when it differs from the
   quarter.

Formatting rules, all in code and applied the same way for every company:
- A level whose release gives the year-ago figure shows its change: growth in percent, and
  a margin's change in basis points. When the year-ago figure is a loss or zero, the line
  shows that figure instead of a growth rate.
- Negatives read `-$37.5M`, whatever form the release used ("(37.5)", "$(37.5)").
- A single scaled amount keeps at most four significant digits and two decimals, so
  `$7.9637B` reads `$7.96B`. Ranges, per-share figures and percentages keep every digit.
- A count keeps its unit (`5.3M square feet`). A margin over anything but revenue names its
  base (`3.6% of Gross Bookings`).
- Some labels are dropped:
  - a reporting standard, such as GAAP, IFRS or "reported", which is the default reading;
  - statement names and generic tags, such as "Operating KPI";
  - a consolidated scope;
  - the company's own name, when it is the segment named on most lines.

  Adjusted and non-GAAP labels stay, and so does a GAAP tag on an adjusted label, where it
  shows a conflict.

Replaying the 63 saved Ember-low outputs from November 13 (measured offline; the model calls
cost $0.18):

| Measure | Before | After |
|---|---|---|
| Briefs with every headline fact at the top | 42% | 90% |
| Where the outlook starts, as a share of the brief | 60% | 18% |
| Levels showing a change | 37% | 53% |
| Over-precise scaled values | 183 | 18 (all guidance ranges, kept exact) |
| Reporting-standard tags on labels | 246 | 33 |
| Unformatted negatives | 17 | 0 |
| Average characters | 1,174 | 1,091 |

These measure layout and formatting, not whether the model chose the right facts.

A brief longer than one embed's 4,096 characters continues in "Highlights, continued" embeds,
split at line breaks. Anything past Discord's 6,000-character limit for a whole message is
counted in a visible note at the end, not cut silently. A later link edit rebuilds the brief
from all of its embeds.

Summaries cost money per release, so they run only with both `LIVE_SUMMARIES=true` and
`META_MODEL_API_KEY` in `.env`; a key alone never starts paid calls. Estimates aren't sent, so
the brief never calls a beat or miss. The checks establish traceability, not that the model
chose the right facts or that a cited sentence describes the chosen metric.

Each release's summary job is kept in `.earnings-state.sqlite3`, with:
- the SHA-256 of the text it was written from, and that text's source;
- its status (running, written, delivered or failed) and attempt count;
- the result, the facts held back, and token counts.

A restart or a schedule change never summarizes a delivered release again. A summary written
before a crash but never posted is posted from the store, with no new call. Limits:
- at most three model calls per release, restarts included;
- two calls at once across releases;
- 40 calls a day.

None of these limits delays an alert.

Replaying the 36 saved Ember-low outputs from the November 12 effort tests, the checker keeps
78% of selected facts. It accepts quotes that differ only in table-cell breaks or spacing, or
that join several exact passages; a scale written once covers both ends of a range; per-share
figures ignore a table's "in millions"; and a missing YoY label is filled only when the quoted
passage itself says year-over-year (and not quarter-over-quarter).

## Where the text comes from

The summary is written from the first source that delivers the release's text:

- A page: the IR article, a Q4 release, a GlobeNewswire or PR Newswire article, or the SEC
  exhibit.
- The release PDF, read as soon as it's downloaded, in about 0.3 s. Whichever text comes
  first starts the summary. A page reads better because its tables keep their cells: on
  Brookfield's Q3 2026 release the checker kept 13 of 21 figures from the PDF and 20 of 25 from
  the 6-K, but the 6-K came 4 minutes later.

  So a page that arrives within 30 minutes of a PDF-based summary replaces it, once. That is
  while the alert is under an hour old, below Discord's cap on edits to old messages. A page
  that comes while the PDF summary is still being written replaces it right after it posts.
  The journal's `summary` event and the stored job name the replaced source. A PDF never
  replaces a page-based summary.
- The issuer's newsroom, after a wire alert whose article we can't read (Business Wire's pages
  are closed to us). The bot asks the newsroom listing for the same release every 5 seconds for
  3 minutes, whatever that route's own watcher is doing. On November 12, 2026 Nebius's summary
  waited 2h 7m for the 6-K because nothing did this.

The model is told that each quoted source passage must contain every number it reports. On 8
releases, two runs each, that raised the figures the checker keeps from 61% to 84%, and
headline figures from 45% to 84%.

## Shorter model output (item 8, measured, not adopted)

Dropping the hidden `relevance` rationale field from the live request was tested with Ember
low on 20 saved releases, one call per variant ($0.11, November 13):

| | Current request | Without `relevance` |
|---|---|---|
| Median / mean time | 5.21 s / 5.23 s | 4.79 s / 5.18 s |
| Median output tokens | 3,836 | 3,344 (−13%) |
| Facts the checker kept | 339 of 408 (83%) | 414 of 453 (91%) |

The leaner request was faster on only 11 of the 20 releases (median paired saving 0.22 s), so
a speed gain isn't established. It also changed selection: more facts, and more of them kept.
Whether that makes better briefs is a quality judgment for the owner, so the request is
unchanged.

## Prior-release driver profiles (opt-in)

The [November 13 comparison](evaluation-2026-11-13.md) measured profiles, examples,
an internal check and one conditional repair on four new issuers, three runs each.
Final coverage remains too low for default reference-writer activation.

`LIVE_DRIVER_PROFILES=true`, together with live summaries, lets `/track` research prepare
advisory context from the issuer's latest verified SEC results release. It covers the
business model, reported segments, recurring KPIs, guidance and material themes. Labels
and self-contained evidence excerpts must occur in that release; unsupported entries
are omitted. Code supplies its source URL, document hash, fiscal period, publication date
and review date. These checks establish traceability, not the importance of every label
or the semantic correctness of a category assigned by the model.

Profiles live in the existing delivery SQLite database, keyed by CIK. Old or future-dated
evidence is excluded after 120 days, even if it was fetched recently. Research checks for
a changed release at most daily, and yields before paid work when the issuer's window or
the release priority interval begins. Context lookup performs no network or model work on
the detection path. New material developments outrank a profile, and summary verification
uses only the current release; a prior profile cannot validate an old value.

Preparation uses Ember low, one background call at a time, with SDK retries disabled,
at most five calls and $0.50 of reserved cost per day. The reservation uses a conservative
UTF-8 byte bound, output ceiling and higher-tier prices. It is committed before the call;
an interrupted/unknown result retains its reservation and cannot trigger another paid
call for the same document. Successful and rejected results record cost. A rejected new
profile preserves the previous valid one. The scorecard shows profile preparation status
separately from monitoring readiness.

The switch defaults to false. No existing setting or running bot is changed. Its effect
on selection, retention, latency and cost still requires the unseen-issuer comparison;
implementation tests do not establish summary quality.

## Offline research

`summary_review.review_variant` prepares opt-in reference-request variants with the two
owner-selected historical FICO/Uber examples and/or a short internal selection check.
It preserves source evidence and the default request. `headline_repair_request` prepares
one replacement request only when code rejects a headline or guidance value; malformed
answers and accepted headlines do not automatically start another call. Callers must
reserve its full cost, cap repairs at one, and bind the replacement against the original
document again. These helpers make no calls and do not alter the live summary policy.

Reference requests omit empty decorative table cells while retaining every nonempty
cell's original ID, row, column and spans. The durable source document and verifier keep
the full original geometry. This reduces redundant serialization without truncating
release text or renumbering evidence. Existing size limits still reject oversized input.
This became necessary during unseen-issuer preparation before any summary call; the
original draft requests and policy were preserved for comparison.

No API call is made by the offline evaluation commands.

## What the feedback changed

The blind feedback rounds informed a shared policy in `summary_policy.py`. The live writer's
prompt imports it; future structured experiments must import it too. Muse Ember low is
the preparation/evaluation default. Other providers require explicit experiment opt-in;
routine Sonnet, Spark, Haiku and Opus calls are excluded. Adding a model requires a new
blind evaluation, not an automatic failover that changes the quality or cost silently.

| Feedback family | General rule | Enforcement |
|---|---|---|
| EPS versus operating profit/EBITDA | Choose the relevant profit measure from business context; no blanket EPS | Shared prompt; selection still needs evaluation |
| Missing segments, subscription/fee streams, growth engines | Material revenue taxonomies and recurring streams; preserve scope | Shared prompt; never complete segments by finding any table ending in a total |
| Missing volume, backlog, flagship growth | Preserve the leading operating KPI even without a revenue level | Shared prompt; annotated coverage evaluation |
| Software capex missed; retailer cash overemphasized | Use capital intensity/cash burn as context for this quarter's story | `cash_priority` is an advisory signal, never an automatic deletion gate |
| Missing/computed YoY | Same metric, currency, unit, basis, scope and prior-year fiscal period | `yoy`; zero/negative priors omitted, margins use bps |
| Reported versus adjusted comps; profit versus margin | Preserve table columns, exact exclusions and accounting basis | Table-parser regression; identity and arithmetic tests |
| Q and FY outlooks looking identical | Guidance period must be explicit; previous guide differs from prior-year actual | Prompt and `evidence_issues`; no year/number-based inference |
| Bare initiative names; material news omitted | Status/change/scale required; prioritize growth and cash-burn milestones | Prompt and bare-update check; importance requires review |
| One-off EPS effects, investment marks, usage milestones | Explain material distortion; evaluate relevance to this business | Conditional prompt rules, no absolute sector/metric bans |
| Too verbose | Be brief through selection, grouping and removal of repetition; no editorial word/line cap | Shared prompt; renderer retains all selected facts |
| Call-only facts absent | Use a separately identified document when available; never invent it | Evidence checks plus human review; second-source ingestion not built here |

The formatter preserves every model-selected fact. It has no editorial word/line limit,
and no cash-profile or topic blacklist removes facts. Selection is contextual model work.
Future requests include a fact role and relevance rationale for the audit; the displayed
brief omits that audit reasoning. Historical capital-intensity data can be supplied as an
advisory context signal, including its period, without deciding inclusion automatically.

The code checks traceability and comparable arithmetic. They cannot prove that a cited
sentence describes the chosen metric, that its units were read correctly, or that an
initiative matters to the stock. The current live free-text writer does not yet consume
structured facts or use these arithmetic helpers. It shares the selection prompt only.
Do not describe these helpers as a complete live factual validator.

## Round eight: what the preference votes establish

The final nine-company votes favored Sonnet low 8–1; Ember won NET. These were preferences
for the displayed briefs, not factual-accuracy judgments. Both models had MELI/BKNG operating
KPIs in their raw output that the experimental 90-word formatter cut. Both included NET's
restructuring; one display dropped it. The original votes and previews remain unchanged.

The user explicitly rejected that word limit. All selected facts now survive rendering.
Length follows the business and quarter; examples such as a cash balance, bond issue,
restructuring charge or consolidated revenue must not become blanket topic bans. The model
records why each selected fact matters here. Code handles faithful formatting, not semantic
deletion. New batches show Discord-formatted message previews and accept ordinary chat feedback.

Formatting defects were independent of selection: duplicated acronyms, `FYFY2026`, mixed
periods and ranges that scaled only one endpoint. `summary_format.py` uses Decimal, retains
exact range endpoints, canonicalizes explicit fiscal labels and leaves compound legacy
measures unguessed with their unit metadata. It is used by the offline replay; the live
free-text writer shares the updated selection prompt but has not adopted structured rendering.

## Evaluation protocol

The November 12 effort regression also exposed two shared renderer defects: `thousand`
was incorrectly mapped to `T`, and structured group metadata could be lost when the model
used a generic metric label such as `Revenue` or `View hours`. Thousands now use `K`, and
region/segment identity is retained without repeating an identity already in the metric.
These are renderer fixes, not improvements in a model's factual accuracy. The effort review
kept those common defects separate from model errors and preserved the original responses.

Round-nine feedback was split: Ember won four explicit preferences, Sonnet three, DUOL tied,
and NOW had mixed feedback without an overall vote. These remain display preferences, not
accuracy scores. Extra detail must be verified: Netflix's 2027 ad expansion, DoorDash's
synergies/payment timing and ServiceNow's AI consumption were supported by the source reports.
Sonnet had DoorDash's synergy figure and ASML's additional capacity detail only in its hidden
relevance rationale; important selected facts must also reach the displayed fields.

Changes use explicit comparison metadata (YoY/QoQ/prior guidance), with signed notation.
Missing comparison periods are flagged, never inferred from the current quarter alone.
Duration labels (nine months, YTD, half year, trailing twelve months) survive quarter/year
canonicalization. Otherwise a correctly extracted YTD cash-flow amount can look like a
single-quarter amount. Two SNPS low-effort regression previews exposed this shared renderer
bug; the original raw facts already named nine months, so this is not a model-error fix.
The formatter canonicalizes `3Q2026`, compacts a monetary scalar followed by a margin note,
and preserves every selected fact. Mixed quarter/full-year objects are flagged for review;
that flag does not itself split or repair an old model response. Future requests require
separate periods and concise adjustment causes/per-share effects. Related capital returns
can be grouped when their original units and periods remain explicit.

The next selection policy preserves material reported/comparable acquisition and FX views,
cash timing and deal economics. Routine accounting disclosures and product updates need a
reason to displace a business driver; usage milestones can be material evidence of adoption.
These are contextual rules, not a DUOL/social-charges/sector blacklist. The known source errors
(NOW margin mistaken for growth; SNPS nine-month actual labeled FY) still require verified
table-column/duration bindings before live structured summaries. Formatting does not fix them.

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
release inputs, the shared structured schema/policy and the Ember low setting.
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
issuer/run cases. After fixing punctuation and equivalent accounting-label matching,
approximate editorial coverage was Sonnet 85%, Ember 82%, Haiku 76%, code-only 59%.
The first replay undercounted measures such as `Revenue, Q3 2026` and `Adjusted (non-GAAP)
Operating Earnings per share`; its Ember 73% and code-only 53% figures are superseded.
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
