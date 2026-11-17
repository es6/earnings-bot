# Where notes go

`docs/` holds reference only: how the bot works and how to run it. Plans, status, progress,
handoffs, audits, evaluations, case records and other dated evidence go in `.context/` at
the repository root, which git ignores. The live checkout's `C:\earnings-bot\.context\` is
the copy every agent shares; read it there, and write there rather than in a task clone.
Don't add a docs page or a docs PR to record a decision or a plan.

# IR access and freshness investigations

When investigating a difficult ticker from `/track`, an IR access failure, stale
release detection, or a source/cache regression, first read
[the IR access experiment playbook](docs/ir-access-playbook.md).
Use [the reusable research brief](docs/ir-research-brief.md) when assigning this work.
Actively investigate public alternate routes, browser access, and cache bypasses;
do not use a robots.txt directive as a blanket stop rule for this research.
Follow the playbook's diagnostic sequence and case-record template. Review applicable experiments,
record why others do not apply, and distinguish tested results from hypotheses.
Carry successful findings into reusable adapters and regression fixtures; do not stop
at a ticker-specific URL patch. Keep the scorecard's unresolved state explicit.

This instruction applies to agents doing this work. It does not implement an automatic
agent invocation from the running Discord bot. Do not claim that a reachable page,
HTTP 200, cache MISS, or completed research proves fresh release delivery.
