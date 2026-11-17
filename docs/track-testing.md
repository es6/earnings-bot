# Debugging `/track`

The owner's requirement (Nov 15, 2026): run the whole `/track` process over and over, fixing
what it gets wrong in a feedback loop with Claude, until a fresh run gets every needed fact
right on the first try. Built in v0.21.28 for US tickers ([track_test.py](../src/first_print/track_test.py)).
It's Phase 0 of [international.md](international.md). Each international phase adds its own
facts to the report and its answer keys.

## The command

`/track ticker:NVDA debug:true`, for members with Manage Server in the bot's server, and for
the bot's owner in any server the bot is in, whatever roles they have there (the owner's
decision). The owner is whoever owns the bot's Discord application, or any member of the
team that owns it. The bot reads that from Discord, so no setting is needed and no role can
take it away.

It runs everything `/track` does, from scratch, as for a company the bot has never seen:

1. **Identify:** SEC's ticker list and the company's filings. A company in the catalog
   (`companies.toml`) is looked up on SEC like any other, so the run shows what research
   finds, not what a person wrote down.
2. **Research:** the investigation (IR site, newswire, release routes, Chrome where a site
   blocks plain requests) and date discovery (the next release, its time, the period and the
   evidence), in one hourly check as the bot runs it.
3. **Speed audit:** release history, usual release time and the release times the window
   will cover.

It changes nothing live:
- Research goes into a throwaway calendar in a temporary folder, deleted after the run. The
  server's calendar and scorecards never see it.
- It opens no window, posts no scorecard and makes no paid model call.
- It runs one at a time, behind live windows like other research, and gives way to a live
  window's Chrome. It refuses to start in the minutes a release is most likely.
- It stops after 10 minutes, in time to reply before Discord's 15-minute limit, and reports
  what it found by then.
- The speed audit reads the bot's own journals for past measurements, as `/track` does. A
  company the bot has watched before can show those; one it never watched can't.

## What it reports

- **In Discord, only to the member who ran it:** a checklist of every fact (✅ found, ⚠️ unsure,
  ❌ missing), how long each step took, and, when the company has an answer key, every fact
  that differs from it. A date only an outside calendar gave is marked unsure.
- **A full report on disk:** `schedules/track-tests/<SYMBOL>-<UTC time>.json` and `.md`, the
  last 20 per symbol. It holds every fact, each source the investigation checked with why it
  was watched or turned down, and what research said while it worked. That's what Claude
  reads to find the mistake.

The same run works without Discord, for one ticker or a batch:

```
uv run python -m first_print.track_test NVDA LCID --out schedules
```

It prints each checklist and, for tickers with answer keys, how many were one-shot. Unlike
the bot, it doesn't wait for live windows, so run it away from release times.

## Answer keys

A file per company, `tests/track_cases/<SYMBOL>.toml`, holding the answers a person checked.
Any fact the report lists can be in it:

```toml
symbol = "LCID"
name = "Lucid Group, Inc."
cik = 1811210
sec_form = "8-K"
wire = "Business Wire"
ir_site = "ir.lucidmotors.com"
routes = ["SEC 8-K"]
next_release = { day = 2027-02-24, time = "16:05" }
period = "FY2026Q4"
usual_time = "16:05"
```

The values above are an illustration, not checked facts. Text matches ignoring case. The IR
site and the evidence match when the key's text appears in what was found. A list matches
when every item in the key was found. The next release matches on its day, and on its time
when the key gives one. **One-shot** means a fresh run matches every field of the key.

## The feedback loop

1. The owner runs `/track SYMBOL debug:true` and tells Claude what's wrong: "the IR site is X",
   "the date is Y".
2. Claude writes or corrects the answer key and reads the saved report to find where the run
   went wrong.
3. Claude fixes the code, adds a regression test from a saved copy of the pages involved, and
   runs the case again.
4. Repeat until the case is clean on a fresh run.
5. Then the batch: every ticker with a key, through the command above, for the one-shot rate.

Fixes are measured on the whole batch, never tuned for one ticker (the standing rule). A
market goes live once its keys pass one-shot on a batch of at least ten companies.

## Still open

- Whether a debug run should also poll each route it finds once, to show it answers now.
- The card the scorecard would show, beside the checklist.
