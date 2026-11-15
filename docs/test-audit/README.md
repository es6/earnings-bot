# Test suite audit, November 15, 2026

Six reviewers each read one area of `tests/` and the source it exercises, at `main` 0dc9d97
(v0.21.35). For every test function they judged whether it checks observable behavior with
meaningful assertions, whether it would fail if its feature were removed, and whether it is
redundant, slow, nondeterministic or obsolete. They checked the "would fail" question by
mutation: neutering a feature in the source, running selected tests, and reverting. Verdicts
are reviewer judgments; counts and command results are measurements. The original per-test
tables are in A–F. [The verification supplement](G-omitted-files.md) adds 33 omitted functions,
completing the original base's 737-function inventory. Later PR tests are outside this dated
snapshot and are reviewed with their changes.

| Report | Area | Functions | Keep | Strengthen | Merge | Rewrite | Delete | Mutations survived |
|---|---|---|---|---|---|---|---|---|
| [A](A-research.md) | `/track` research: IR sites, wires, routes | 113 | 88 | 16 | 8 | 1 | 0 | 4 of 25 |
| [B](B-dates-track.md) | Dates, `/track` commands, scorecards | 121 | 98 | 18 | 4 | 1 | 0 | 3 of 22 (+2 redundant code) |
| [C](C-live-path.md) | Live windows, alerts, restarts | 107 | 85 | 13 | 5 | 3 | 1 | 3 of 26 |
| [D](D-sources.md) | Feeds, fetcher, Chrome, SEC, GlobeNewswire | 116 | 81 | 27 | 5 | 3 | 0 | 6 of 40 (+1 equivalent) |
| [E](E-summaries.md) | Summaries and the verifier | 173 | 153 | 5 | 3 | 3 | 9 | 4 of 35 (+2 equivalent) |
| [F](F-ops-suite.md) | Ops tests, plus suite-wide practice | 74 | 58 | 12 | 1 | 3 | 0 | 12 of 39 |
| | **Total** | **704** | **563 (80%)** | **91** | **26** | **14** | **10** | **32 of 187** |

The original totals above describe 704 functions. G adds 25 KEEP and 8 STRENGTHEN judgments;
the completed inventory is 737 functions: 588 KEEP, 99 STRENGTHEN, 26 MERGE, 14 REWRITE and
10 DELETE. These percentages describe review decisions, not measured defect-detection rates.

## What it found

**The suite is mostly sound.** About 80% of the completed inventory received KEEP judgments.
A–F report 187 mutation checks and 32 actionable surviving variants in their selected scopes;
equivalent variants and repeat checks mean `187 - 32` is not a valid killed-mutant count.
The measured full suite passed in two randomized orders (1,216 cases) and in parallel; that
does not prove order independence under every seed or runner.

**32 candidate gaps were reported in selected test scopes.** They are not 32 confirmed
whole-suite gaps. G's new mutation check proves the comma-parser change is caught outside
F's assigned files. Confirm remaining survivors against all tests before adding duplicate
coverage. Each original report retains its observations. Important candidates include:
- The fetcher: the urgent-404 transport branch (the current code intentionally returns an
  urgent HTTP 404 rather than falling back to Chrome; D's mutation description needs its
  exact patch reproduced), redirects re-enabled on two routes, and
  Chrome's back-off not reset after an error (D).
- 6-K handling: copying the form into a plan, and the 6-K condition in `event_target` (B).
- GlobeNewswire: re-guessing article numbers already opened, and history rows from another
  organization's page (D).
- Release text: a 404 page body returned as release text (`text._get_text`, F).
- Summaries: the 120-day stale-release guard before a paid profile call, and the end-turn
  check on profile answers (E).
- The watchdog's interval limit and webhook retry, config parsing of the server channels, and
  `main()`'s exit on missing settings (F).

**Real waiting makes the suite slow and a little fragile.** The full run takes 93.8 s
serially; the slowest 25 tests take about 60 s of that, mostly real `sleep`s and Discord
pacing (5.0 s for one card-update test, 6.0 s for an end-to-end wire alert, 13.5 s across the
publication-trial tests). Several tests assert on wall-clock thresholds (`< 0.05 s`,
`most_in_one_second == 8`, 0.02 s deadlines), the most likely flakes on a loaded CI runner.

**Warnings hide our own leaks.** The ordinary run's 1,039 warnings are one third-party deprecation in
discord.py, triggered from `pipeline.py:615`. Behind them are 11 `ResourceWarning`s that are
ours in the recorded warning-enabled run: 8 unclosed SQLite connections, a stream writer and
2 subprocess pipes. This is a measured run's count, not an exhaustive leak inventory.

**Shared test code isn't shared.** 24 test modules import fakes from other test modules, there
are two different `FakePoster`s and two fake fetchers, and there is no `conftest.py`. Dozens of
tests reach into private methods (about 15 in A and 14 in B alone) (`_check`, `_investigate`, `_prepare_cache`), so refactors
break tests that should only care about behavior.

**Some tests are out of date or misplaced.**
- 9 summary tests cover code nothing calls anymore (`yoy`, `cash_priority`,
  `SummaryFact.identity`, the unwired `summary_review.py`).
- Two live-path tests pin behavior the source no longer has.
- Tests for one module sit in another's file, for example `DateDiscovery` tests in
  `test_sec_results.py`.
- One Chrome test is skipped on Linux CI when a monkeypatch would let it run everywhere.

**Tooling is behind current practice.**
- `pyproject.toml` has no strict markers or config, no `xfail_strict`, no warning policy, no
  timeout and no importlib import mode.
- CI measures no coverage and runs serially.
- Only 26 of 123 `parametrize` marks have readable ids.

## Plan

In order. Each step is measured by the suite still passing, and by the mutations it targets
now being caught.

1. **Shared fakes:** move `FakePoster`, `FakeDiscord`, the fake fetchers and the builders into
   `tests/fakes.py` and a `conftest.py`, and end the test-to-test imports. Everything after
   builds on this.
2. **Reproduce surviving mutations suite-wide:** preserve the exact patch and test selection,
   exclude equivalent changes and coverage already provided elsewhere, then strengthen each
   remaining real gap with a regression that fails when the feature is removed.
3. **Remove real waiting:** inject a clock or patch the delays in the slowest tests, and replace
   wall-clock assertions and sleep-ordering with events. Expected: most of the 60 s goes, along
   with the flake risk.
4. **Warning policy:** turn warnings into errors for our own package, ignore the one discord.py
   deprecation, and fix the 11 leaks.
5. **Prune and tidy:** delete or merge the 36 redundant and obsolete tests, move misplaced ones,
   and replace private-method calls with public paths where one exists.
6. **Tooling:**
   - strict markers and config, `xfail_strict`, a per-test timeout, importlib import mode;
   - CI with `pytest-xdist` (3.2x faster: 29 s against 94 s) and random order;
   - coverage reported in CI;
   - property tests for the pure parsers, and a scheduled mutation-testing run.

## Results of the fixes (Nov 15, v0.21.43)

All six steps were applied. Each area's report has a fix section with the test names. Numbers
are measured unless marked.

| Area | Survivors closed by a new or stronger test | Already caught elsewhere | Equivalent | Tests deleted | Groups merged |
|---|---|---|---|---|---|
| A | 3 (M12, M26, M27) | 1 (M24) | 0 | 0 | 4 |
| B | 3 (M4, M10's morning-call branch, M12) | 1 (M22) | 0 | 3 | 1 |
| C | 2 (resumed-alert cut, `synchronous=NORMAL`) | 1 (`_SHARED_QUEUES`) | 0 | 4 | 1 |
| D | 6 (urgent 404, M27, M32, M40, M14, M15) | 0 | 0 | 0 | 2 |
| E | 4 (M8, M14, M30/M35, M38) | 0 | 0 (M11, M34 were already marked) | 9 | 2 |
| F | 9 (P2, TX2, W3, W5, MAIN1, MAIN2, CMD2, B2, B3) | 2 (CF1, CF4) | 1 (W4) | 0 | about 5 (hand count) |

Every new or strengthened test was confirmed to fail with its mutation in place and pass
without it. B's two "redundant code" findings are live guards, not dead code, and stay.

- **Speed:** the whole suite runs in 34.0 s serially (1,331 tests), down from 93.8 s (1,216
  tests), and 23.5 s in parallel. The real waits were replaced with virtual clocks, patched
  host gaps, poll counts and events: the card-update test went from 5.0 s to 0.09 s, the wire
  alert from 6.0 s to 0.06 s, and the publication trials from about 3 s each to 0.3 s. Two
  remain slow on purpose: the burst replay (4.2 s, as shortening it let a mutation survive) and
  a Chrome priority test (2.0 s, a real subprocess).
- **Flakes:** wall-clock assertions became counts or exact virtual times. Three parallel runs
  in random order and two suites run at once (double load) all passed.
- **Warnings:** pytest now treats warnings as errors, except discord.py's own `re.sub`
  deprecation. The run with ResourceWarnings as errors found 4 leaks, all fixed: SQLite
  connections opened with `with sqlite3.connect(...)` (which commits but doesn't close) in 3
  test files and the trials command line, a test server's writer, and a test's pipes.
- **Shared fakes:** no test module imports another. Fakes are in `tests/fakes.py`, data in
  `tests/builders.py`, `tests/brookfield_data.py` and `tests/clocks.py`. No `conftest.py`: no
  identical fixture was repeated in 3 or more files.
- **Tooling:** strict markers and config, `xfail_strict`, a 120 s per-test timeout, importlib
  import mode, pytest-xdist and pytest-randomly in CI, coverage reported on Linux, property
  tests (hypothesis) for Retry-After and amount parsing, and a weekly mutmut run on the pure
  parsers (`.github/workflows/mutation.yml`).

Left for later, with reasons in each report: tests still calling private methods where no
public path exists yet (`TrackProgress._check`, `_facts`), a few tests in another module's
file, the "untested behavior" lists (new coverage rather than fixes), and the
`test_summary_binding.py` merges, deferred while that module is being changed on main.
