# Publication-trial evidence

Listing/API trials require all three configured patterns to match the same entry. RSS and
Atom use one item/entry; supported JSON lists use one object; HTML uses the text of one
link. A page containing the issuer in one story and the requested period in another is
absent, not a measured sighting. Unsupported listing structures fail closed and need a
scoped decoder. Article, file and SEC-directory controls retain their document scope.

The journal records `candidate_scope` and `matched_entries` with each matching identity
and title. A candidate is not yet a verified full report. Check fiscal period, issuer,
canonical provenance and complete results body before promoting a detection route. Retain
last-negative/first-positive intervals and clock uncertainty, not an invented publication
instant derived from a cache header.

Generic recorder polling now handles both numeric and HTTP-date `Retry-After`. Expired
dates mean no additional server delay, while malformed values retain the 30-second
fallback. Normal cadence still applies. Trials share this parser. Transparent connection
retries are distinct from complete 429/503 responses; integrated observation retries
already consume an additional budget reservation through Fetcher middleware.

These changes prepare trustworthy comparisons; they establish no new issuer latency win.
