
In directory mode, `/track NVDA` adds a catalog issuer and starts looking for its
next earnings notice. `/upcoming` shows the next seven days and issuers awaiting
dates. The bot uses the release time stated by the issuer when available. It
uses a wider window for a confirmed day with an approximate release time.
Manage Server users can provide `when` and `period` to override a window, or
use `/untrack` to remove the subscription.

Each tracked issuer gets one progress message in the status channel, or in the
earnings channel when no status channel is configured. The message follows the
next scheduled window and shows its prepared sources. The source audit reports
measured headlines and report text separately, along with archive timestamp
estimates and the verified @earnings_guy comparison when available. Audits run
one at a time outside active earnings windows. The separate
`.tracking-progress.sqlite3` file retains the Discord message IDs across restarts.
