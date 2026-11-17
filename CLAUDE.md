# Where notes go

`docs/` holds reference only: how the bot works and how to run it. Plans, status, progress,
handoffs, audits, evaluations and other dated evidence go in `.context/` at the repository
root, which git ignores (the live checkout's `C:\earnings-bot\.context\` is the shared
copy). Code, tests and PR descriptions carry what a change needs; don't add a docs page or a
docs PR to record a decision or a plan. See [AGENTS.md](AGENTS.md).

# Difficult ticker research

For `/track` source investigation, IR blocks, cache behavior, or late release incidents,
read [AGENTS.md](AGENTS.md) and follow [docs/ir-access-playbook.md](docs/ir-access-playbook.md).
Use [docs/ir-research-brief.md](docs/ir-research-brief.md) for research assignments.
Preserve the distinction between verified access, usable report retrieval, cache-layer
evidence, and measured live publication latency. Record unresolved cases and next steps in
`.context/`.
