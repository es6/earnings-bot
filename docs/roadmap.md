# Roadmap

What First Print 1.0 means, and what comes after. Progress, plans and evidence are tracked
outside the repository; this page changes only when the goals do.

## 1.0

1.0 is a bot the owner can leave running through an earnings season:

- **Reliable release detection.** Every route stays alive or says loudly that it isn't; a
  route that dies mid-window alarms. Polling is gentle far from a release time and full
  speed around it. A live streak of releases posts every alert from the first route to see
  it, with no failure.
- **Summaries worth trusting.** Each fact shown is checked against the release
  (`VERIFIED_SUMMARIES`). Summaries are labeled Beta until live releases confirm the check.
- **Safe on the owner's PC.** Chrome is bounded in memory and priority, ends with the bot,
  and a heavy release day never slows the PC.
- **Several Discord servers,** each with its own tracked companies, sharing one watch per
  release.
- **Tickers outside the US:** ASX, Hong Kong, Tokyo, Toronto and Europe's main markets
  ([international.md](international.md)), identified with OpenFIGI, each market with a
  dedicated source.
- **Maintainable code,** reviewed against the [Python quality guide](python-quality.md):
  clear ownership of tasks and resources, and no workflow too large to change safely.

## After 1.0

### Earnings call transcript summaries (`/transcript`)

`/transcript TICKER` listens for the company's call. The bot posts a Discord message at once
and keeps it updated with status: call found, live, ended, transcript found, summarizing.
Once the call is over it gets the transcript and summarizes the most useful new information
from the call, not a repeat of the release. The summary then replaces the status.

To work out: where transcripts come from and how soon after the call (the IR site, the
webcast provider, transcript services); how the bot knows the call has started and ended;
Muse Ember at low effort only, with a spend cap per call.
