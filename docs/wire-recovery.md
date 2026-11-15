# Business Wire body recovery

An English Business Wire RSS sighting can recover its report from FinancialContent.
This adds report acquisition after discovery; it does not discover unpublished releases
or establish that the mirror is faster. Discord retains the wire's public URL. The saved
source document separately records `canonical_url` and `retrieval_url`, along with its
content hash and table coordinates.

The known wire ID, headline and published Eastern date produce at most two mirror
candidates (unpadded and padded dates). Only a 404/410 tries the second spelling. The
complete outer article must carry the exact headline and a link to that exact English
wire ID, with substantive financial report text. Nested article sections and tables are
preserved. Parsing runs off the event loop. Teasers, missing stories, other issuers or
periods, lookalike domains and unrelated page links fail closed. No wire IDs are guessed.

A shared mirror pause prevents other releases or spelling retries from circumventing a
403 or a 429/503 `Retry-After`, including an HTTP date. Existing independent report
routes remain available. This route uses ordinary HTTP and no personal browser state.

November 15 historical checks recovered full, provenance-bound copies for Nebius Q3,
CrowdStrike Q2 FY27, Klarna Q2 and UnitedHealth Q3 2026. Their known wire IDs were checked
against the mirror links; CrowdStrike and UnitedHealth required retaining nested article
sections. Adobe Q3 was readable through the search service, while the same local HTTP route
returned 404; vantage-dependent availability remains unresolved.
The tested FICO and HubSpot headline/date-derived URLs returned 404; coverage is partial.
This is historical availability evidence only. Mirror ingestion lag, publication-time
availability and future headline/slug conventions require event-keyed measurement.

Fixtures contain the public Nebius and CrowdStrike article HTML, saved separately from
navigation. Tests cover real tables, durable provenance, exact identity, date formatting,
nested sections, missing spelling fallback and shared backoff. No polling schedule or
live configuration is changed by this adapter.
