# IR access and freshness investigations

When investigating a difficult ticker from `/track`, an IR access failure, stale
release detection, or a source/cache regression, first read
[the IR access experiment playbook](docs/ir-access-playbook.md).
Use its diagnostic sequence and case-record template. Review applicable experiments,
record why others do not apply, and distinguish tested results from hypotheses.
Carry successful findings into reusable adapters and regression fixtures; do not stop
at a ticker-specific URL patch. Keep the scorecard's unresolved state explicit.

This instruction applies to agents doing this work. It does not implement an automatic
agent invocation from the running Discord bot. Do not claim that a reachable page,
HTTP 200, cache MISS, or completed research proves fresh release delivery.
