# Public source diagnostics (November 15)

`HttpRelease` journals each direct request's status or exception type before Chrome
fallback, the fallback status/error, elapsed time, logical HTTP call count, client-pool
and source age, a hash of the URL spelling, and fixed challenge markers from at most
16 KiB. The listing keeps its own snapshot before an article request changes the current
diagnostics. No response body, cookie, request header or raw exception message is included.
Client age is pool age, not TCP connection age; logical calls are not a wire-attempt count.
Existing retry, recovery and independent-source behavior is unchanged.

This addresses the Nebius incident's missing first direct-HTTP failure. The historical
record has 3,503 successful HTTP listing responses followed by a Chrome 403; it cannot
identify that missing direct failure, prove an IP block, or diagnose a fingerprint.
The first On/Sea misses were different: no HTTP errors/timeouts or route retirement.
Their evidence, limits and existing replay fixes are in [architecture.md](architecture.md)
under **Nov 10, On and Sea**. Upload/Last-Modified stamps do not establish availability.

## Optional client comparison

Run only off-window after checking the live schedule:

```powershell
uv run --with curl-cffi==0.17.0 python -m first_print.transport_probe plan.json --out recordings/profile-pilot
```

The optional library is not a production dependency. A plan supplies `off_window_confirmed`,
up to four `cases` (`name`, public HTTPS `url`, `listing`, and three identity/period/results
regex `patterns`), and a total `budget` of at most 16. The CLI alternates ordinary and
Chrome-like curl profiles with the same honest User-Agent. Each sample uses a fresh session,
public DNS pinned for that host, normal certificate/SNI validation, no inherited proxies,
no redirect following, no application retry loop and a 4 MiB decoded-body ceiling.

The durable ledger reserves before each transfer, retains failures across restarts and
refuses a changed plan in the same folder. One output lock, at most eight transfers per host,
at least five seconds between samples and a 15-minute deadline bound the pilot. A 403 waits
30/60/120 seconds; 429, 5xx or Retry-After stops the host. These are application-transfer
reservations with fresh sessions, not counts of TCP/TLS packets. Protocol negotiation changes
with the profile, so a difference cannot isolate TLS fingerprinting alone.

Listings use entry-scoped identity matching; article pattern matches are observations for
manual verification, not automatic acceptance of a report. Bodies and selected cache headers
are saved for that audit. No model or Discord client is created, and no live route is changed.

## Measured results

- **HubSpot:** control/profile/control/profile = 403/200/403/200. Same resolved IP and
  Cloudflare PoP, fresh sessions and identical explicit headers; both successful copies
  name the correct issuer and actual Q3 2026 release. This verifies profile-dependent
  listing access locally. A 30-day cache lifetime was advertised, so this establishes no
  freshness or speed advantage. Production keeps its existing Chrome and spelling routes.
- **Applied Materials:** all four samples 403. No profile bypass demonstrated.
- **Nebius:** all four samples 200 with the correct Q3 entry. No profile advantage and no
  attribution of the earlier production incident.
- **Business Wire:** the control failed before an HTTP status was available. Three reserved
  followup samples (profile/control/profile) also failed with curl code 92, HTTP/2 stream
  failure and status zero. The combined cohort remained within 16 reserved transfers.
  This is a transport failure, not proof of a server 403 or a fingerprint bypass.

Two disposable-Chrome Notified navigations also recorded all request types on each main
page's CDP target for 20 seconds: WeRide 48 requests, Lucid 50. No publisher document-manifest
JSON request appeared; their listings were server-rendered. Analytics/consent calls were
not publication backends. No cookies, headers or POST bodies were saved and query values
were redacted. OOPIF targets were not auto-attached, so this is a bounded main-target trace,
not an exhaustive claim about every browser process. No hidden/private endpoint search followed.

## Fresh research correction

GlobeNewswire keyword search returned zero entries for SEC's `NVIDIA CORP` and 100 for
`NVIDIA`. Queries now omit legal suffixes; the exact normalized publisher check still rejects
other companies. A new fresh NVDA debug run identified GlobeNewswire and three quarterly
release timestamps, while confirming November 17 at 16:20 ET, FY2027Q3. A reachable archive
or this timing history still does not prove the next release will appear there first.
