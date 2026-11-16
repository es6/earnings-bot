# November 15 implementation handoff

The reviewed implementation work from the source investigation and Claude session is
merged through v0.21.39. Deployment remains held: the running checkout is still v0.21.26.
No international implementation, live restart, schedule/config change, manual Discord post
or additional paid model call was made during the final continuation.

**Later the same night:** with the owner's yes, main through v0.21.43 (#154) was deployed at
18:16, so the "awaiting deployment" and "live code remains unchanged" notes below are history.
Dependabot #119 was closed. Current state is in the [roadmap](roadmap.md).

| Work | Result |
|---|---|
| Trial correctness and backoff | Per-entry identity prevents cross-item sightings; HTTP-date Retry-After and reserved connection retries are accounted for (#142). Nested JPM manifests now produce the exact validated press-release PDF identity |
| Issuer surfaces | ROAD pagination, Chrome-only history and JPM manifest discovery/acquisition (#143); fresh NVDA and LCID debug runs now recover their wire and three quarterly timestamps (#146, #148) |
| Body recovery | Historical BW mirrors are usable for NBIS, CRWD, KLAR and UNH; canonical wire identity and retrieval provenance persist (#144). Coverage remains partial |
| Diagnostics and experiments | Direct/fallback failure metadata and a bounded optional client-profile A/B tool (#146). Local HubSpot profile access verified; no blanket bypass or new publication-speed win |
| Multiple servers | Fanout, isolated server views, shared research/cooldown and guarded imports reviewed and merged (#136–#138), awaiting deployment |
| Summary work | Reference-mode integration, frozen studies, count/currency and temporal-binding regressions (#141, #149). Default writer unchanged; summaries remain Beta because the quality gate is unmet |
| Claude documentation | README corrected and merged (#145). Audit completed with 33 omitted functions and mutation claims corrected (#147): 737 functions at its dated base |
| Soak | Four-hour replay and passive sampling completed; reusable replay and Windows sampler supplied. See [results and limits](soak-2026-11-15.md) |

## Remaining evidence and owner decisions

- **November 16–20:** review the already configured GlobeNewswire/NVDA SEC observations
  and journal the real-release streak. A replay cannot satisfy a publication event gate.
- **Source comparisons:** [inert trial drafts](trial-plans/README.md) require confirmed
  events, historical recognition, host budgets and separate first-usable-body verification
  before activation. Intel's distinct cache rows and available mirrors are not speed proof.
- **Summary activation:** retention is 790/1,869 selected values (42%), with only a subset
  independently checked. No universal numeric-accuracy claim. Reference mode and driver
  profiles stay off until supported retention and new-release auditing meet the gate.
- **Deployment and operations:** live code remains unchanged; deployment is not authorized
  by merge permission. Real multiple-server Discord limits and prolonged Chrome behavior
  still need live measurements. Windows Update pause is an owner operation.
- **Separate backlog:** Dependabot #119 (Python 3.15) remains an explicit owner decision.
  The audit's future refactor plan needs exact suite-wide mutation reproduction first;
  its selected-group survivors are not a list of 32 established production defects.

The [roadmap](roadmap.md) records these states, and [Astra priorities](astra-tasks.md)
identify bounded tasks for future reasoning/research work. International implementation
has not started.

## Evidence and reproduction

Sanitized raw evidence is preserved outside git in the workspace's
`completion-evidence-nov15`: `work-ledger.md`, `replay-soak.jsonl`, `passive-soak.jsonl`,
per-round replay files, profile ledgers, bounded browser traces, fresh-debug reports,
offline summary scores, regression/mutation logs and PR descriptions. Original Astra
captures and Claude scratch outputs remain unchanged. Neither endpoint reachability,
Last-Modified nor a publisher stamp is substituted for first observed availability.

Run the usual Ruff/format/Pyright/pytest checks and the commands in the soak report.
The draft JSON files validate as `publication_trials.TrialPlan` without scheduling them.
