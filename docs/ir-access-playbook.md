# IR access, cache experiments, and release recovery

Owner's objective: `/track` should prepare the fastest practical free routes to the
issuer's actual earnings release, including IR sites that require stock headful Chrome.
Investigate difficult issuers thoroughly, preserve independent sources, and make failures
visible. This is an agent research playbook, not an assertion that its experiments are
implemented or that a particular bypass works on every site.

Written November 12, 2026 against main `163e65b`, v0.15.0. Re-read current code and recent
incident records before acting: transport, cache, recovery, and publication behavior change.
See [browser configuration](browser-ir.md), [architecture](architecture.md), and
[summary handling](summaries.md). Never overwrite an earlier experiment's raw evidence.

## 1. Required outcome and honest readiness

The target is reliable, low-latency retrieval for every tracked issuer. No client can
guarantee that an unavailable publisher will respond, force a publisher to update an
internal database, or prove that no earlier public copy exists anywhere. Do not convert
that limitation into an excuse to skip investigation. Complete the applicable experiments,
retain independent coverage, and leave a concrete next action for unresolved cases.

Keep these findings separate, with timestamps and transport details:

| Finding | Required evidence | Does not establish |
| --- | --- | --- |
| Reachable | Expected issuer content through a named transport | Full release available |
| Report usable | Correct issuer/period, full report text or valid PDF | Latest publication visible |
| Cache behavior characterized | Repeated control/variant samples and content identity | Origin database updated |
| Route accesses newer content | Variant has a verified new item while control still lacks it | Permanent future advantage |
| Live latency measured | Actual release transition, request intervals, visible alert time | Universal fastest source |
| Degraded | Specific failure, remaining working routes, next retry | Investigation complete and healthy |

An HTTP 200 challenge page is not success. A fresh 404 is not a release. A cache MISS is
not proof of current publisher data. Lack of Age or cache headers means unknown behavior.
An ETag change can reflect banners, nonces, or layout changes rather than a new report.
An unchanged body outside a publication event is inconclusive, even after twenty requests.

## 2. When an agent must consult this playbook

Consult it for a new ticker whose investigation cannot verify a usable IR route; any
403/429/challenge/redirect/soft-404; inconsistent HTTP versus browser content; uncertain
cache freshness; an inaccessible attachment; a dated results row that does not match;
or an alert/summary materially behind another measured source.

For each case, produce the record in section 10. Review all experiment families, but run
only those supported by the site's evidence. Record skipped families and the reason.
Do not blindly launch every URL variant or a large concurrent probe matrix.

Future `/track` integration should persist a research-needed event with issuer identity,
failure evidence, and existing source plan; show its queued/running/degraded state on the
existing scorecard; and supply this document to the reviewing agent. This document and
the repository agent instructions do NOT themselves create that runtime integration.
Any automated investigator needs a bounded request budget, deduplication, cancellation,
and priority below hot release detection. Do not spawn unlimited agents from poll errors.

## 3. Reconstruct the failure before changing the route

1. Confirm issuer identity, exchange, fiscal year/quarter, official IR domains, expected
   release interval, and whether a timestamp belongs to the call or the release.
2. Read the actual persisted target and source list. A profile saying Chrome was researched
   does not prove that the running target uses it. Include code/version and schedule hash.
3. Identify the failing stage: discovery, listing request, parser/matcher, article request,
   attachment download, text extraction, report verification, or Discord delivery.
4. Reconstruct every transport attempt. Record HTTP status/exception before Chrome fallback,
   the exact URL variant, redirect destination, browser warmup, and final response.
5. Preserve successful bodies around the incident where available. Missing bodies limit
   retrospective claims: matched=false alone cannot distinguish stale data from bad parsing.
6. Check stop flags, backoff, deadline, host queue, browser lock, tab lifecycle, and whether
   independent recovery shares the same pacing. A live process is not proof of live polling.
7. Separate expected time, publisher stamp, file Last-Modified, first observed listing,
   first usable body, visible alert, and visible summary. Convert UTC/ET explicitly.

Code entry points at the baseline revision:
- `tracking_commands.py`, `issuers.py`, `investigate.py`: identity and issuer research.
- `date_discovery.py`, `cache_research.py`: date and route/cache preparation.
- `browser_sources.py`, `quarterly_table.py`, `sources.py`: adapters and matchers.
- `fetch.py`, `browser.py`, `_windows_desktop.py`: pacing and browser lifecycle.
- `recorder.py`: polling, failures, timing, and stop/backoff behavior.
- `text.py`, `pipeline.py`: body retrieval, PDF extraction, enrichment, and delivery.
- `track_progress.py`: user-visible investigation/readiness.

## 4. Build a publication map, not just one URL

Start from the issuer's public IR navigation and recent verified releases. Inventory:
newsroom listing; quarterly-results table; investor hub; financial reports API; issuer RSS
or Atom; event page; release HTML; PDF; shareholder letter; supplement; presentation;
and named newswire/syndication copies. Preserve the origin of each discovered endpoint.

Model dependencies: newsroom -> API -> article -> file CDN, for example. Two URL spellings
of the same generated feed are correlated routes. A document host and separately updated
quarterly table may provide more useful independence. Neither is automatically first.

Inspect public network traffic during a normal navigation. Look for Fetch/XHR JSON,
GraphQL queries already used by the page, HTML fragments, embedded application state,
hydration data, pagination requests, and cross-origin document links. Change a visible
filter once and observe which request implements it. Record method, required parameters,
response schema, content identity, and any session requirement. Replay only read operations.

Use only public HTTPS endpoints and normal public browser sessions. Preserve the bot's
URL/redirect/private-address protections. Do not turn endpoint discovery into scanning
private origins, admin interfaces, or using unrelated accounts/credentials. A public CDN
hostname actually linked by the site is useful evidence; a guessed backend IP is not.

## 5. Access and browser experiments

### A. Canonical HTTP baseline and scoped identity

Test the exact public canonical listing and latest known article with the ordinary client.
Record redirects rather than silently treating their destinations as equivalent. Compare
body identity, MIME type, title, issuer, and the known release anchor. A response can be a
generic homepage even when the URL and HTTP status look plausible.

Where identity appears relevant, compare the existing project User-Agent with a scoped,
documented per-host variant and stock Chrome. Change one variable at a time. A browser UA
string alone does not reproduce Chrome's cookies, TLS behavior, fetch metadata, or scripts.
Record the exact working configuration rather than pretending a header fixed the cause.
Prefer genuine browser-generated headers for browser traffic; avoid a contradictory bundle
of invented browser headers. Do not change the identity globally based on one issuer.

### B. Stock headful Chrome: warm navigation, then fetch

Use the bot-owned Chrome profile on its inactive desktop. Navigate to the canonical public
IR page, allow the normal page initialization, and verify actual issuer content. Then fetch
the listing/article from the warm same-origin tab. Compare canonical and selected variant
URLs separately. A path rejected by a firewall may fail in Chrome too while canonical works.

Keep public session state within the owned profile; never copy personal browser cookies.
Test warm-tab reuse versus a fresh owned tab/profile only when the evidence suggests a
session-specific failure. Repeated profile creation can add latency and lose useful state.
Distinguish a normal public challenge completing from an unresolved challenge page.

### C. Navigation versus in-page fetch

Some pages serve usable content to navigation but reject Fetch/XHR, or build the release
list using scripts. Compare raw HTTP HTML, warm-tab fetch response, and rendered DOM.
Observe the underlying data responses before assuming full navigation must be repeated.

For rendered monitoring, prefer a source-specific condition such as a completed relevant
response plus a verified results row. Do not wait for unrelated analytics to become idle.
Keep a bounded timeout and verify complete financial text before treating an article as
usable. Listing recognition alone can produce the immediate link signal when its adapter's
verification contract permits it; it cannot certify a complete summary input.

### D. Browser cache versus service worker

Run paired controls: normal browser behavior; HTTP cache disabled; and, where compatible,
service-worker bypass. Inspect whether the response came from disk cache or a service worker.
Chrome provides distinct `Network.setCacheDisabled` and `Network.setBypassServiceWorker`
commands. Apply settings to the targets that issue the requests; do not assume a top-level
tab setting covers every worker or out-of-process frame. Confirm supported protocol behavior
against the installed Chrome version. [CDP Network](https://chromedevtools.github.io/devtools-protocol/tot/Network/)

Browser `fetch(..., {cache: 'no-store'})` concerns the browser's HTTP cache; it is not evidence
that a CDN or application bypassed its own storage. Compare the actual network response.
Service-worker bypass can break a site that depends on worker behavior, so retain a normal
control. [Fetch cache modes](https://developer.mozilla.org/en-US/docs/Web/API/Request/cache)

### E. Repeated-session and sustained access

A successful manual navigation is only a first test. Exercise the planned polling cadence
for a bounded pilot, then a longer soak when appropriate. Record success rate, 403/429 onset,
timeouts, session age, and resource growth. Use the shared host budget and honor Retry-After.
Reduce redundant requests before concluding a new identity or more workers are needed.
One later 403 does not prove an IP-wide ban; isolate URL, session, transport, and time.

## 6. Cache experiment catalog

Use an interleaved control/variant design. Keep exact URLs and content fingerprints so a
client's URL normalization or redirect cannot silently erase the proposed cache key.
Choose small pilots under the host budget; expand only if they answer a specific question.

### F. Unique query parameter

Compare canonical with a bounded sequence of unique query values such as a monotonic probe
identifier. Determine whether the server accepts, ignores, strips, or redirects it. Observe
Age, Date, cache status, validators, feed build time, and semantic release IDs. A new cache
entry can still contain the same application-generated feed. Do not label that origin-fresh.

### G. Equivalent path spellings

Test a small number of percent-encoded unreserved letters or case variants only where the
site demonstrably resolves them to the same resource. Preserve encoded octets through the
HTTP client; avoid accidentally canonicalizing them before sending. Inspect redirects.

Equivalence must include issuer/category identity, parser shape, and a known release, not
just the page title. PR Newswire case variations have historically returned an unintended
feed; an HTTP 200 is insufficient. Canonical URLs remain the user-facing links. A 403 on a
variant should trigger investigation of canonical access, not mark the whole host dead.

### H. Meaningful parameters: page size, filters, and ordering

Use the public application's supported page-size, category, year, offset, or sort controls.
For a WeRide/Q4-like case, compare several small page sizes while holding newest-first order
and issuer/category fixed. Verify the known newest report is included on every route.
Different pages may share a cached database query, or changing page size may simply omit
the desired item. Record both body overlap and freshness evidence. Do not rotate thousands
of arbitrary values or assume parameters work because the server returns 200.

Keep publication order distinct from fiscal period order. A corrected old release can
appear newly modified. Narrowing to this year can exclude a fiscal-year result published
in the next calendar year. Test these edge cases before promotion.

### I. Alternate representations and public hosts

Compare site-linked RSS, Atom, JSON/widget, HTML, printable pages, PDF endpoints, and public
asset hosts. Record whether each representation includes the complete earnings release,
only a teaser, or additional guidance documents. Mirrors must match issuer/period/content.
Preserve the publisher's canonical article link in Discord with provenance for retrieval.

GlobeNewswire's feed-host article route is a historical example, not a universal hostname
rewrite rule. An RSS and Atom pair may share one publisher rebuild even with different CDN
objects. A Business Wire mirror must be timed at a live transition; existence alone does
not establish a faster publication path.

### J. Header-dependent representations

Inspect `Vary` and test supported representation headers such as Accept or Accept-Language
one at a time. Verify the resulting language, issuer, category, completeness, and release
identity. A Vary field describes cache selection; it does not guarantee an unseen header
value produces newer origin data. Accept-Encoding may change compression rather than
publication timing. Do not manufacture unbounded arbitrary header values.

### K. Revalidation controls and conditional requests

Compare request revalidation directives with the unmodified route and with a verified URL
variant. Test conditional requests only with a validator captured for that same resource
and representation. A 304 saves transfer but supplies no new report body. Validate whether
the route's ETag tracks release-list changes before relying on it for monitoring.

HTTP cache directives and validators are useful evidence, not an instruction the client can
use to force an application rebuild. Keep browser, edge, intermediary, and application
freshness separate. [HTTP caching semantics](https://www.rfc-editor.org/rfc/rfc9111.html)

### L. Direct document candidates and negative cache

Derive a small candidate set from several prior public releases and current issuer links:
quarter/year substitutions, documented filename changes, and linked file hosts. Check both
negative and positive responses. A HEAD can differ from GET, and a cached 404 can outlive
publication. Validate PDF bytes, issuer, and period after a successful response.

Measure the 404-to-200 transition with canonical and verified variant controls. Preserve
redirects and file cache headers. File Last-Modified is not first public availability: it
can reflect build/upload time, a copied timestamp, or a revision. Never use it alone to
claim we could have alerted hours earlier. Bytes obtained from a public candidate still
need issuer/period/result verification before alerting.

### M. Diagnose the publisher's inner cache

If independent CDN keys all show the same feed build and newest release, compare a different
publication surface: individual article, quarterly table, document manifest, or issuer API.
An advancing Date header with an unchanged application build is a clue, not proof of its
internal implementation. Estimate update cadence over multiple actual content transitions.

If every verified public route is delayed at the publisher, retain the least-delayed route
and independent wire/SEC sources. Record 'publisher update delay unresolved'. Random keys,
more browser tabs, and faster polling cannot guarantee an unpublished record appears.

## 7. Latency experiments after access works

- Warm connections and browser sessions before the hot interval. Measure startup separately.
- Promote the relevant observed JSON response to a tested adapter instead of rendering the
  whole page. Capture the response on the initial navigation without fetching it twice.
- Separate urgent article work from listing work with bounded concurrency, preserving one
  host rate budget. A second tab does not authorize a second independent rate limiter.
- Test staggered independent sources or a bounded delayed second request when the first is
  unusually slow. Count extra requests and cancellation; use measured tail latency.
- Minute-boundary bursts are a hypothesis. Keep baseline coverage, infer cadence from real
  observations, and compare total load and missed off-boundary publications. A rounded
  wire timestamp does not establish the exact moment its feed becomes readable.
- Examine larger feeds and pagination for overflow on busy earnings days. A fast feed that
  evicts the release before the next successful poll is an unreliable source.
- Consider a second authorized machine only as a measured resilience experiment, with
  deduplicated delivery and shared applicable budgets. Different locations need not reach
  independent CDN state. Do not substitute proxy rotation for a diagnosed source problem.

Record scheduling wait, semaphore wait, browser lock wait, warmup, host pacing, request
start, first byte, completed body, parse, verification, first sighting, and Discord time.
Use monotonic durations and UTC wall-clock timestamps; retain clock uncertainty. Report
median and tail latency with sample counts. Never omit failures from latency comparisons.

## 8. Recovery is part of the source plan

Persist a bounded state machine: working -> suspect -> alternate transport/route -> backing
off -> recovered or degraded. No single transient 403 should silently retire the issuer for
the entire window. Differentiate variant-specific failure from host-wide evidence.

Suggested progression, adapted to evidence:
1. Retain the failed attempt and canonical control; check whether a variant caused it.
2. Try the already verified alternate HTTP/browser route under the same budget.
3. If in-page fetch fails, test normal navigation/rendered content and observed data routes.
4. Honor server backoff; retain independent sources and schedule recovery.
5. Report sustained degradation on the existing scorecard, with next retry and coverage.

Recovery loops must consume the same backoff state as normal polling. Do not let a secondary
issuer-body fetch retry every five seconds while the primary source is paused for minutes.
Give server Retry-After precedence, including supported date-form values. Enforce deadlines,
attempt budgets, and cancellation when a sufficient body arrives from another source.

A wire sighting should initiate independently managed report-body acquisition: issuer
article, shareholder letter, public PDF, and a verified mirror as appropriate. Reading a
listing does not prove every required report document is available. Parse downloaded PDFs
off the detection path; retain table geometry and extraction uncertainty. Cache the body
for attachment and summary use. Do not repeatedly download or summarize the same release.

Preserve partial progress: source alert first, files/body/summary afterward, with visible
failure states. Corrected documents require explicit versions, not duplicate release alerts.

## 9. Promotion gates and regression tests

A research success becomes a production route only after:
- A known real earnings report is retrieved and verified for issuer and fiscal period.
- Date notices, webcasts, prior quarters, dividends, challenge pages, generic feeds, empty
  JSON stubs, and unrelated issuers do not pass the matcher.
- Canonical and variant resource identity is tested, including redirects and category drift.
- The proposed cadence and concurrency fit shared budgets; failures and recovery are bounded.
- Body, attachment, and summary acquisition can proceed without delaying the first alert.
- Readiness describes precisely which freshness property has evidence and which is unknown.

Use saved real response fixtures plus small synthetic failure cases. Mutation-check changes
that protect issuer/category/period matching, URL preservation, source retirement, backoff,
and resource cleanup. Run the repository lint, format, type, and test checks before a PR.
For latency changes, compare the burst replay and run a browser-inclusive load test when
relevant. An offline fixture proves parsing/recovery logic, not publication freshness.

Before the next release, recheck the selected route and body path; do not rely solely on an
old green preflight. During the release, keep a control where budget allows. Afterward,
compare first usable body as well as first link against independent sources and the X
benchmark. Label unmatched benchmarks unknown. Renew evidence after site/API changes.

## 10. Required case record and agent handoff

Store sanitized artifacts under an explicitly ignored research directory, or attach a
small sanitized evidence report to the PR. Check ignore rules before writing raw captures.
Do not commit cookies, credentials, authorization headers, personal browsing data, or raw
HAR files containing them. Retain public response bodies needed to reproduce the issue.

```yaml
# Proposed research record, NOT a supported production configuration schema.
issuer: {ticker: EXAMPLE, exchange: EXCHANGE, cik: null, fiscal_period: FY2026Q3}
investigation: {utc: TIMESTAMP, code_revision: SHA, trigger: HTTP_403_OR_STALE}
canonical_listing: https://issuer.example/investors/news
previous_verified_report: {url: URL, period: FY2026Q2, body_sha256: SHA}
running_plan: {transport: http, variant: canonical, interval_seconds: 1}
failure_stage: listing_request
attempts:
  - experiment: warm_chrome_canonical
    hypothesis: public_browser_session_changes_access
    control: ordinary_http_canonical
    exact_request_url: URL
    transport: chrome_fetch
    status: 200
    redirect_chain: []
    browser_cache: unknown
    service_worker: unknown
    headers: {date: DATE, age: null, cache_status: MISS, etag: ETAG}
    semantic_identity: {issuer: EXAMPLE, category: earnings, latest_report_id: ID}
    content_sha256: SHA
    timings: {lock_ms: 0, pacing_ms: 0, network_ms: 0, parse_ms: 0}
    conclusion: reachable_only
    artifact: sanitized-response-path
skipped_families: [{experiment: conditional_requests, reason: no_reliable_validator}]
freshness: {cache_layer: unknown, live_transition_measured: false}
recovery: {next_retry_utc: TIMESTAMP, alternate_route: URL, remaining_coverage: [SEC]}
decision: {state: degraded, reason: newer_content_not_yet_observed}
next_experiment: compare_quarterly_table_during_next_content_transition
```

Agent completion report must answer: What failed? What was actually tested? Which response
contained the correct release? What freshness property was established? What remains
unknown? What adapter/general rule changed? Which tests fail without it? What will the
running bot do if the same failure happens again? When will evidence be rechecked?

Use plain scorecard messages, for example:
- 'HTTP blocked; Chrome retrieved the previous report. Monitoring route prepared;
  publication freshness has not yet been measured.'
- 'A URL variant was newer than canonical during this release by 8.2s; measured once.'
- 'IR temporarily blocked; retry at 07:12:30 ET. Wire and SEC remain active.'
- 'Listing readable; report PDF unavailable. Retrying the document separately.'

## 11. Lessons from November 12, 2026

NBIS had roughly 3,500 successful HTTP polls before a failed HTTP attempt and a Chrome
403. The old fatal-stop behavior ended IR monitoring before publication. The logs did not
identify the initial HTTP failure, so do not call it a proven IP-wide ban. Business Wire's
release stamp was 07:11:00 ET, feed build 07:14:51, and detection 07:14:53: the publisher feed
rebuild dominated that delay. Cache busting did not eliminate it.

BN's Chrome quarterly table detected the PDF at 07:06:35.734 ET, then Discord showed the
alert at 07:06:35.821. A timeout created an observation gap before discovery. Cache MISS
and a recent listing timestamp do not establish the exact public publication instant.
The already-downloaded PDF was not read for the summary, which waited four minutes for SEC.

v0.15.0 added 403 backoff, transport logging, PDF extraction after a grace period, and
issuer-body recovery after a wire alert. Verify current implementation and shared backoff
semantics before relying on those paths. These incidents require end-to-end tests, not
just a successful one-off request to the IR homepage.

## 12. Reference semantics

Use current primary documentation when implementing protocol-specific experiments:
- [Chrome DevTools Network](https://chromedevtools.github.io/devtools-protocol/tot/Network/):
  request/response observation, cache controls, service-worker bypass.
- [HTTP caching, RFC 9111](https://www.rfc-editor.org/rfc/rfc9111.html):
  cache keys, freshness, revalidation, Age, and Vary.
- [HTTP semantics, RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html):
  conditional requests, HEAD/GET distinctions, redirects, and Retry-After.
- [Browser fetch cache modes](https://developer.mozilla.org/en-US/docs/Web/API/Request/cache):
  the client-side meaning of no-store, reload, and no-cache.

The experiment hypotheses and operational recommendations above are project proposals.
These references explain protocol mechanisms; they do not certify any issuer's behavior.
