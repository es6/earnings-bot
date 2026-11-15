# Historical listings and public document manifests

Release history now follows up to three linked, same-host archive pages and keeps explicitly
zoned dated article rows. This restores older quarterly times when a feed retains only one
quarter. Preliminary results and notices remain excluded by the existing history verifier;
8-K filers still require a corresponding results filing. Browser-only issuer feeds can
also supply history through the existing owned-browser admission controls. History version
3 rechecks older saved histories rather than preserving an incomplete result forever.

An unknown wire no longer prevents reading an exact publisher's GlobeNewswire history.
Research can identify that wire when its public listing names the exact fiscal quarter
within two days of the latest verified SEC result. Name attribution is still checked by the
Globe organization decoder. A stale release or a different quarter does not identify it.

The public manifest adapter recognizes explicitly named `pressRelease` documents under
year/quarter rows, including ordinal quarters. It rejects external PDF hosts, conflicting
rows and presentation/supplement labels. The existing issuer API source downloads the exact
file and verifies its financial text, issuer-domain anchor and reported fiscal period off
the event loop before accepting it. The source carries those bytes to the normal delivery
and evidence pipeline; random filenames are never guessed. A changed file follows existing
document-version handling.

JPMorgan's catalog now supplies its verified quarterly manifest and page. General research
can discover that endpoint from the page's declared feed type plus its linked same-origin
JavaScript, then validate the returned document schema. Discovery is bounded to two scripts
and does not scan guessed backends.

These changes improve discovery and historical coverage. Earliest link, usable PDF, SEC and
wire ordering still require a real publication comparison. No speed or early-publication
claim follows from Last-Modified or a reachable historical PDF.
