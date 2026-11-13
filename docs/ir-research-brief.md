# Public earnings source research brief

Use this brief when assigning a difficult `/track` case, an inaccessible IR report,
or a release that arrived behind a competing source. Fill in the issuer, incident,
working revision, release window, and existing routes before handing it to an agent.
Read the [experiment playbook](ir-access-playbook.md) for methods and evidence records.

## Objective and initiative

Find the earliest usable public copy of the issuer's earnings report and turn the
findings into reusable monitoring and recovery behavior. The owner explicitly wants
creative, proactive investigation of cache and automation blocks. Continue past the
first blocked URL or stale feed; test an alternate route or transport and measure it.

Do not add a mandatory "read robots.txt first" step or treat a robots.txt disallow as
a blanket prohibition on researching a public route. It may provide context about a
site's crawling preferences; it does not establish whether a public route is usable,
fresh, faster, or inaccessible. Rank routes using observed behavior. A previous agent's
decision to stop is not evidence that the technical investigation is complete.

Undocumented public endpoints can be research candidates. Form hypotheses from normal
browser traffic, public content, previous releases, and the site's URL or API patterns.
Public search and widget routes are candidates too; measure whether they add value.
Carry successful experiments forward into general adapters and tests.

## Investigation to perform

1. Reconstruct the live target and incident timeline before changing anything. Include
   listing access, matching, article/PDF retrieval, verification, and Discord delivery.
2. Map the public publication surfaces: newsroom, RSS/Atom, quarterly results, events,
   public JSON/XHR or read-only GraphQL calls, document hosts, and syndication copies.
3. Compare ordinary HTTP, scoped per-host identity, stock headful Chrome, warm-tab fetch,
   navigation, and rendered content where relevant. Inspect the page's actual requests.
4. Characterize browser, service-worker, CDN, negative-response, and application caches
   separately. Test meaningful query parameters, equivalent encoded paths, supported
   page sizes/filters, alternate representations, and revalidation controls as applicable.
5. Derive a bounded set of direct article/file candidates from public release history.
   Investigate public article-ID ordering only with evidence and a request budget.
   Check cached 404/410 behavior and verify issuer, period, and report content.
6. When the same surface stays late, investigate a separately published surface. Compare
   first link, first usable body, and first visible alert against independent sources.
7. Test sustained access and recovery. A successful page read is insufficient; document
   what happens when an established route later times out, blocks, or returns old data.

## Experiment controls

Research shares infrastructure with the live bot. State the per-host budget, maximum
requests, concurrency, timeout, and stopping condition for each pilot. Coordinate with
live polling and honor observed server backoff. Change one variable at a time, then
expand useful experiments. A failed hypothesis should lead to the next justified test.

Keep this work scoped to public content and preserve the project's URL protections.
Do not use personal accounts or private credentials. Separate public route discovery
from probing private services. These boundaries do not rule out testing public cache
keys, browser sessions, public search, widgets, or site-linked data APIs.

## Required result

Return exact sanitized request URLs, transport settings, statuses, relevant headers,
semantic release IDs, timestamps, response/body evidence, and timing breakdowns. State
which experiments worked, which failed, which were skipped and why, and what remains
unknown. Do not equate HTTP 200, cache MISS, or changing headers with fresh report data.

For a proposed code change, include a reusable implementation and regression cases
that fail without it. For an unresolved case, name the next experiment, retry/recovery
behavior, remaining coverage, and the evidence needed at the next release. Recommend
production polling based on measured value and sustained behavior, not mere reachability.
