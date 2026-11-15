# Testing `/track`

A proposal (Nov 15, 2026) for the owner's requirement: run the whole `/track` process over and
over, fixing what it gets wrong in a feedback loop with Claude, until a fresh run gets every
needed fact right on the first try. Not built yet; it's Phase 0 of
[international.md](international.md), and it serves US tickers too.

## The command

`/track ticker:SAF.PA test:true`, for members with Manage Server in a configured server, and
for the bot's owner in any server the bot is in, whatever roles they have there (the
owner's decision). The owner is whoever owns the bot's Discord application, as
discord.py's `is_owner` reads it, so no setting is needed and it can't be lost with a
role.

It runs everything `/track` does, from scratch, as for a company the bot has never seen:

1. Parse the symbol and identify the company: SEC for US tickers; the exchange's list,
   OpenFIGI and the second-source check for others.
2. Investigation: IR site, newswire, release routes, Chrome where a site blocks plain requests.
3. Date discovery: the next release date, its time, the period and the evidence.
4. Speed audit and release history: usual release time, home clock, the release times the
   window will cover.
5. The window it would plan and when polling would speed up.

It changes nothing live:
- Cached research is ignored, so the run shows what a first try would find.
- It writes to a throwaway database under `schedules/track-tests/`, never to the server's
  calendar.
- It opens no window, posts no scorecard and makes no paid model calls.
- It runs one at a time, behind live windows like other research, and never in the minutes a
  release is most likely.

## What it reports

- **In Discord, only to the member who asked:** the card the scorecard would show, plus a
  checklist of every fact (✅ found, ⚠️ unsure, ❌ missing) and how long each step took.
- **A full report on disk:** `schedules/track-tests/<SYMBOL>-<UTC time>.json` and `.md`. It
  holds every step's input and output, each page or feed it read, and why each route was
  taken or turned down. That's what Claude reads to find the mistake.

## Answer keys

A file per company, `tests/track_cases/<SYMBOL>.toml`, holding the answers a person checked:

```toml
symbol = "SAF.PA"
name = "Safran SA"
exchange = "XPAR"
isin = "FR0000073272"
ir_site = "https://www.safran-group.com/finance"
wire = "none"
next_release = { period = "FY2026", day = 2027-02-26, session = "before open" }
usual_time = "07:00"
results_headline = "Full-year 2026 results"
```

The values above are an illustration, not checked facts. A test run compares its findings
with the key and lists every difference. **One-shot** means a fresh run matches every field of
the key.

## The feedback loop

1. The owner runs `/track SYMBOL test:true` and tells Claude what's wrong: "the IR site is X",
   "the date is Y".
2. Claude writes or corrects the answer key and reads the saved report to find where the run
   went wrong.
3. Claude fixes the code, adds a regression test from a saved copy of the pages involved, and
   runs the case again.
4. Repeat until the case is clean on a fresh run.
5. Then the batch: `scripts/track_eval.py --cases tests/track_cases`, which already runs
   `/track`'s research for many tickers without Discord, runs every key and prints the
   one-shot rate.

Fixes are measured on the whole batch, never tuned for one ticker (the standing rule). A
market goes live once its keys pass one-shot on a batch of at least ten companies.

## Questions to settle when it's built

- Whether test runs should also try a live poll of each route they find, to show it answers
  now (one request each).
- How long to keep saved reports. Proposed: the last 20 per symbol.
