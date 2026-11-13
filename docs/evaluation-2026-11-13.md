# November 13 unseen-issuer reference-summary comparison

The reference writer is **not ready for default activation**. Its selection is much
better than its final coverage: code drops too many useful values. Profiles, examples
and an internal selection check do not solve that bottleneck. One repair improves
coverage but adds a second paid request and roughly doubles median latency.

## Method

- Coca-Cola (KO), Moody's (MCO), Eaton (ETN), Boston Scientific (BSX): current Q3 2026
  and prior Q2 releases from their public SEC exhibits. These names were absent from
  the earlier saved tuning-document filenames checked before collection.
- Frozen documents, source hashes, agent-written editorial annotations and request
  policy before summary calls. Profiles use only the prior release. Examples come
  from the owner's recorded FICO round 3 and Uber round 6 C selections.
- Muse Ember low only. Four request variants, three runs each per issuer. The repair
  condition reuses each examples run, then permits one call for code-rejected headline
  or guidance values. Every replacement goes through the same binder.
- 64 new paid calls: four profiles, 48 summaries, 12 conditional repairs. No Discord
  posts, production-state changes, activation or deployment.
- $0.40322178 for this study; $0.70991728 including the earlier six reference calls
  and recovered Claude reference/ablation costs. One durable $2 ceiling, reservations
  committed before calls, no SDK retries. Unknown outcomes retain reservations.

Preparation exposed excessive annotated-input overhead before any summary call. Empty
decorative cells were omitted without changing text, IDs, coordinates or the verifier's
full geometry. The original draft policy/requests remain preserved. All summary variants
use the same compact representation. No input or output was silently truncated.

## Frozen comparison results

The 48 annotated important topics give 144 possible topic/run matches per variant.
The editorial matcher is conservative and may miss equivalent labels. Selection is
measured before binding; final coverage after it. Numeric retention counts scalar/range
values only, separately from business updates; older experiment reports included updates
in their retention counts and are not directly comparable.

| Variant | Important topics selected / 144 | Important topics retained / 144 | Numeric values retained / selected | Median latency | Total condition cost |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline references | 114 | 27 | 74 / 356 | 9.11 s | $0.0760 |
| Prior-release profile | 121 | 25 | 71 / 369 | 9.47 s | $0.0878 |
| Profile + examples | 117 | 30 | 81 / 381 | 9.97 s | $0.0800 |
| Profile + examples + internal selection check | 123 | 27 | 92 / 385 | 9.82 s | $0.0850 |
| Profile + examples + conditional repair | 123 | 40 | 110 / 378 | 19.09 s | $0.1454 |

The repair condition includes its reused examples call; do not add that column to the
other costs to calculate billed spend. Its additional calls cost $0.065447484. Profile
preparation cost $0.0090386 and is excluded from individual summary-condition costs.

No wrong accepted value was found among the matched annotated-value checks (26 baseline,
24 profile, 28 examples, 25 internal-check, 38 repair). **This is not an all-claims accuracy
score**: respectively 48, 47, 53, 67 and 72 accepted values remain outside those checks.
The two reproduced wrong-metric/wrong-quarter cases remain adversarial unit tests.

Scoring preserves segment/consolidated and reported/organic distinctions. Initial scoring
drafts incorrectly compared organic orders with organic sales, and year-only period labels
without their explicit quarter durations; those drafts are preserved and superseded.

Agreement compares metric, slot, period, duration, basis, canonical value, currency, unit
and comparison qualifier across three runs. Empty outputs never earn perfect agreement.
Baseline mean pairwise Jaccard was 0.284 KO, 0.316 MCO, 0.000 ETN and 0.067 BSX. Respectively
6, 5, 14 and 5 important topics were missing from every final baseline run: those are
failures, not agreement. The saved report lists omissions and agreement for every variant.

## Follow-up fixes and limits

The output review exposed a presentation gap: a rolling twelve-month rate could render
under a plain quarter label, and year-only guidance could lose its verified three-month
horizon. The renderer now retains differing durations. `months ending DATE` is a window,
not a point-in-time balance. A fiscal-year label cannot disguise a subannual duration.

Separately, replay of the original saved cases reproduced a reporting-title scope lost
when a layout table precedes it. Binding can use the release's own explicit reporting
announcement as missing opening scope. Comparative table cells still require their own
period headers, and explicit guidance sections cannot turn a forecast into an actual.

These later fixes are regression/replay results, not another independent holdout. The
initial experiment remains frozen. The live writer, profile switch and optional request
variants remain unchanged by this comparison. Default reference activation still needs
substantially better final coverage and broader factual auditing.

## Preserved artifacts

The project workspace contains `unseen-profile-evaluation-2026-11-13/`: source URLs and
hashes, eight original HTML documents and extraction snapshots, frozen gold/policy,
request JSON, durable raw responses, initial report, follow-up replay and Discord-style
HTML previews without a voting widget. The shared call ledger remains in
`development-evaluation-2026-11-13/calls.db`. No credentials are stored in these artifacts.
