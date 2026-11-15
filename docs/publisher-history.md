# Release times behind date-only issuer listings

Some Notified listings show only a calendar date, even when the original wire report
states a precise publication time. The history reader now follows at most three linked
listing pages and, when those provide fewer than three timed results, checks at most
three unique quarterly-results article links on the issuer's own HTTPS host. Date notices,
preliminary results and generic navigation are excluded.

A report needs an exact matching visible/metadata headline, substantive financial text,
the same reported quarter, and one unambiguous zoned publication meta date. A date-only
card, naive timestamp or Last-Modified value never receives a guessed time. The actual
report headline is located after navigation before reading its reporting period.

If the issuer article has no such date, one explicitly linked original PR Newswire
article can supply it under the same identity/content checks. No publisher URL is guessed,
no lookalike host or credential/query-bearing link is followed, and HTTP redirects are
not automatically followed. Metadata parsing runs off the event loop. Host pauses are
shared across these historical reads after a block, throttle or server outage.

This adds at most three issuer-article and three publisher reads to the bounded listing
walk. Existing background research policy, host pacing and window priority still apply.
History schema version 4 causes older reviews to be refreshed normally.

A fresh Lucid debug run on November 15 recovered November 9, August 4 and May 5, 2026 at
16:05 ET and now reports that usual release time. The next Q4 date remains an FMP estimate,
properly marked unsure because the issuer has not confirmed it. The real August 4 PR
Newswire HTML is a fixture, including its explicit `2026-08-04T21:05:00+01:00` date (16:05
EDT). Publication stamps are historical timing evidence, not first-observed availability
or a speed win. No live schedule, report gate or international path is changed here.
