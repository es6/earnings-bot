# Test audit F: ops/support modules (part 1) and suite-wide practice (part 2)

Repo es6/first-print at origin/main 0dc9d97 (v0.21.35). Every number below is MEASURED in this
worktree on Windows 10, Python 3.13, 32 logical CPUs, unless marked "read" (from reading code).
Git status is clean at the end; every mutation was reverted by the harness (try/finally rewrite of
the original text).

## PART 1: per-file tables

Part 1 scope: 13 files, 74 test functions (measured by grep; 111 pytest items with parameters).
The 13 files run in 20.97 s (measured, `-W default`), of which 5 tests take 13.5 s.

Verdict totals (74 functions, counted from the tables): KEEP 58, STRENGTHEN 12, REWRITE 3, MERGE 1,
DELETE 0.

### tests/test_publication_trials.py (5 tests, 11.9 s measured of the 20.97 s)

| test | verdict | reason |
|---|---|---|
| a_trial_records_a_transition_and_stops_at_its_budget | STRENGTHEN | 3.2 s real time. Survives mutation P2: `transition_observed=absent and matched` changed to `matched` still passes, because the test only checks that SOME row has transition+usable, never that the first poll of a candidate is not flagged as a transition. Budget half is killed (P1). |
| a_trial_budget_survives_a_restart_and_its_id_cant_be_reused | KEEP | Both durable-budget and ID-reuse halves killed (P1, TR4). 2.2 s. Uses `with sqlite3.connect(...) as db`, which commits but does not close the connection. |
| trial_turns_wait_behind_monitoring_polls | STRENGTHEN | Priority mutation TR3 is killed, but it proves it with 3.0 s of real sleeping and an `>= 2.9` wall-clock assert (flaky under load, slow). Has an unused `monkeypatch` parameter. Inject a clock or shrink the host gap. |
| the_bot_runs_trial_plans_from_its_schedule_folder | STRENGTHEN | 3.2 s, reaches into `runner.trials` and `runner.trial_db` internals and awaits a task with a 10 s real timeout. Valuable end-to-end (never posts, broken plan reported once). Cut interval/duration to speed up; assert through the journal only. |
| a_trial_s_options_reach_its_requests_and_nothing_else | KEEP | Byte-cap mutation TR2 killed. Real loopback aiohttp server (no external network). Monkeypatches private `fetcher._browser`; acceptable, no public hook. Name has a stray "s" ("a_trial_s_options"). |

### tests/test_release_band.py (10 tests; 100% line coverage of release_band.py measured)

| test | verdict | reason |
|---|---|---|
| a_route_polls_slowly_far_from_the_release_time_and_fast_close_to_it (5 params) | KEEP | Ramp-branch mutation killed. Good parametrization. Add `ids=`. |
| a_route_never_polls_faster_than_its_own_speed_or_paces_without_times | KEEP | Pins own-speed floor and no-times fallback. |
| between_two_release_times_a_route_polls_slowly_again | KEEP | Real WeRide scenario, exact values. |
| every_route_but_secs_follows_the_release_times | KEEP | `paced()` mutation killed. |
| recent_release_times_are_placed_on_the_release_day_in_its_part_of_the_day | KEEP | RECENT 4 to 5 and session-filter mutations both killed. |
| a_watch_polls_slowly_then_speeds_up_on_time | STRENGTHEN | Replaces `recorder.pace` with a scaled fake, so the real `pace` is not exercised through the watch loop; relies on wall-clock bounds (`fast[0] < 0.40`) that can flake on a loaded CI box; 0.6 s. Use a fake monotonic clock. |
| a_shared_feed_polls_as_fast_as_its_fastest_window_needs | STRENGTHEN | 0.43 s real time; `both.sent > 5` is a loose bound (stronger: compare against `slow_only` or exact expected count with fake time). Also its run emits the 3 unclosed-sqlite ResourceWarnings in this file (recorder.py:424, measured), a Journal/store not closed. |
| a_window_paces_every_route_but_secs_by_the_release_times | KEEP | Checks polling_band event and watches' times. |
| a_window_paces_its_routes_from_the_moment_it_opens | KEEP | End-to-end through `record_target`; patches `recorder.sync_clock`. |
| a_company_releasing_by_a_clock_without_daylight_saving_is_placed_by_it_too | KEEP | Both the HK case and the US negative control. |

### tests/test_text.py (15 tests)

| test | verdict | reason |
|---|---|---|
| broadcom_text_comes_from_its_metadata_api | KEEP | Checks the urgent request URL shape and parsed text. |
| a_broadcom_release_that_isnt_up_yet_has_no_text | KEEP | Three stub shapes. |
| a_page_the_poll_downloaded_isnt_fetched_again | KEEP | Asserts zero requests. |
| a_missing_page_or_file_has_no_text | STRENGTHEN | Survives TX2: removing the `status != 200` guard in `_get_text` still passes, because the 404 stub has an empty body and the "too short" rule rejects it. Use a 404/403 whose body is long enough to look like a release. |
| globenewswire_article_is_read_from_the_feed_host | KEEP | Pins host rewrite and the single request. |
| html_to_text_keeps_paragraphs_and_table_columns | KEEP | Exact lines. |
| blank_header_cells_keep_labels_over_their_columns | KEEP | Real Costco regression plus a rule check over columns. |
| edgar_spacer_and_dollar_columns_read_as_before | KEEP | Regression pin for Workiva layout. Pure snapshot but documented. |
| a_heading_that_spans_two_rows_keeps_the_row_below_in_place | KEEP | rowspan case. |
| pick_exhibit_prefers_the_press_release | KEEP | `named or pages` mutation killed. |
| pick_exhibit_falls_back_to_the_biggest_document | KEEP | Fallback path. |
| q4_text_comes_from_the_newest_release_alone | KEEP | One-item request and its bodyType. |
| q4_text_falls_back_when_the_one_item_response_has_no_release (5 params, ids present) | KEEP | Good ids. |
| a_page_too_short_to_be_a_release_has_no_text | KEEP | Teaser case. |
| a_release_pdf_is_read_as_text_and_a_broken_one_as_nothing | KEEP | Hyphen-join mutation killed. Combines two behaviors; splittable, not required. |

### tests/test_watchdog.py (6 tests)

| test | verdict | reason |
|---|---|---|
| a_route_that_stops_answering_raises_one_alarm_naming_it_then_clears | KEEP | Dedupe (W1) and clear message (W2) mutations killed. |
| a_silent_journal_while_a_window_is_open_says_the_bot_may_be_down | KEEP | Both silent and missing journal. |
| a_route_with_no_poll_since_its_window_opened_is_named | KEEP | |
| no_alarm_early_in_a_window_after_it_or_for_a_finished_route | KEEP | Four negative cases in one test; consider parametrizing for failure clarity. |
| the_journal_is_read_as_it_grows_even_mid_line | STRENGTHEN | Mutation W4 (never skipping the first partial line when starting mid-file, `START_BYTES` tail read) survives: that branch is untested. |
| a_failed_check_is_logged_and_the_next_one_still_alarms | KEEP | |

Missing: the 5x interval limit (W3: `limit = QUIET` survives), and a webhook post that fails
(OSError) must be retried next check (W5 survives: marking raised even when `_send` failed is not
caught). Webhook posting itself (`post_webhook`, lines 67-88) and `main` are uncovered.

### tests/test_benchmark.py (4 tests)

| test | verdict | reason |
|---|---|---|
| tickers_share_one_timeline_lookup_with_a_fallback | KEEP | 20 concurrent callers, one request pair. |
| matches_the_window_and_verifies_x_author_and_earnings_text | STRENGTHEN | Name promises author AND earnings text verification. Author is killed (B1) but removing the earnings-text check (B2) survives, and removing the window filter (B3) survives, because the decoy titles are excluded some other way. Needs a post whose embed text is not earnings, and an in-feed post outside the window with a valid title. |
| feed_unavailable_differs_from_no_matching_post (2 params) | KEEP | |
| window_report_uses_discord_message_creation_time | REWRITE | 60 lines of setup: patches a class method and `unattended.record_target`, drives private `runner._watch`, builds a store inline. Good intent (2.00 s after alert, message-snowflake time) but the setup dwarfs the assertion. Move the harness to a fixture shared with test_unattended. |

### tests/test_restart.py (7 tests)

| test | verdict | reason |
|---|---|---|
| a_restart_waits_while_a_window_is_open_or_about_to_open | KEEP | Boundary mutation R1 killed. |
| a_restart_goes_ahead_once_the_alert_is_posted | KEEP | R2 killed. |
| a_restart_goes_ahead_once_the_window_is_closed | KEEP | Also isolates by channel. |
| every_window_in_the_way_is_listed_in_order | KEEP | |
| schedules_are_read_as_the_bot_reads_them | KEEP | BOM, duplicate, broken file. |
| the_check_counts_schedule_files_and_tracked_tickers | STRENGTHEN | Name says "counts" but asserts exit codes and window text; uses real `datetime.now(ET)` (+3 min), fine but wall-clock; rename. |
| the_check_needs_the_alert_channel | KEEP | |

### tests/test_chrome_cleanup.py (5 tests)

| test | verdict | reason |
|---|---|---|
| chrome_a_gone_bot_left_is_found_and_a_live_ones_isnt | KEEP | Parent-alive mutation (CH2) killed. |
| the_sweep_ends_leftovers_and_removes_profiles_no_live_chrome_uses | KEEP | Profile removal mutation (CH1) killed. |
| with_nothing_left_the_sweep_ends_nothing | KEEP | Small; could be a param of the previous test but is clear. |
| chrome_ends_when_the_bot_that_started_it_is_killed | STRENGTHEN | Windows-only (skipif; also `pytest.skip` at runtime). Spawns a subprocess and polls `tasklist` for up to 10 s. Leaks both Popen pipes (2 ResourceWarnings, measured). Use `with Popen(...)` and close pipes. |
| the_bot_sweeps_leftover_chrome_before_it_starts_watching | KEEP | Mutation BOT1 killed. Local imports inside the function; move to module top. |

### tests/test_resources.py (3 tests)

| test | verdict | reason |
|---|---|---|
| a_snapshot_adds_up_every_chrome_and_says_what_the_pc_has_left | KEEP | Sum and `processes` mutations (RS1, RS2) killed. |
| a_window_records_the_bots_use_of_the_pc | KEEP | Journal-write mutation (RS4) killed. |
| chrome_runs_below_normal_priority_under_a_memory_ceiling | REWRITE | Windows-only, 2.0 s, reads `child._kernel`, `child._job`, `_EXTENDED_LIMITS`: pure implementation details via ctypes; redundant platform guard inside; skipped on Linux (covered only by the Windows CI job). Assert through a public `DesktopProcess` property instead. |

### tests/test_formatting.py (7 tests)

| test | verdict | reason |
|---|---|---|
| a_fiscal_period_reads_like_a_quarter | KEEP | Could be parametrized. |
| times_are_eastern_on_a_12_hour_clock (4 params) | KEEP | |
| alert_times_keep_their_milliseconds | MERGE | Same function (`clock`) as the previous test; fold the cases into one parametrized table. |
| durations_read_at_a_glance (8 params) | KEEP | |
| each_source_has_a_plain_name (16 params) | KEEP | Table is a lookup, but it is the behavior. |
| copies_of_a_source_share_a_line | KEEP | |
| a_long_list_says_how_much_didnt_fit | KEEP | Overflow-note mutation (F1) killed. |

### tests/test_clock.py (5 tests)

| test | verdict | reason |
|---|---|---|
| parse_reply | KEEP | Name is not behavior-descriptive. |
| parse_reply_rejects_bad_answers (4 params) | KEEP | Stratum/leap/mode mutation (C1) killed. |
| clock_uses_the_fastest_answer | KEEP | C2 killed. |
| clock_keeps_its_last_sync_when_no_server_answers | KEEP | |
| clock_ignores_the_system_clock | STRENGTHEN | `approx(abs=0.05)` against the real clock: sensitive to a slow runner. Untested: `ask()` (clock.py:48-59, 11 uncovered lines, measured); a local UDP socket would test it without the network. |

### tests/test_config.py (5 tests)

| test | verdict | reason |
|---|---|---|
| reads_settings_from_environment | KEEP | |
| empty_guild_id_means_unset | KEEP | `env_ignore_empty` mutation (CF3) killed. |
| token_is_hidden_in_repr | KEEP | |
| missing_token_is_an_error | KEEP | |
| sec_user_agent_needs_a_contact_email | KEEP | Pattern mutation (CF2) killed. |

Survivors: CF4 (`discord_server_channel_ids` comma parsing returns `[]`) and CF1 (a non-None
default for `discord_channel_id`) both pass. `alert_channel_ids` dedupe/ordering, the int/list
branches, `watchdog_webhook_url` and `RecorderSettings` have no test (config.py lines 50, 52
uncovered, measured).

### tests/test_main.py (1 test)

| test | verdict | reason |
|---|---|---|
| alerts_and_status_can_share_one_channel | REWRITE | The name claims channel sharing, but `main()` never looks at the status channel (that logic is in bot.py:143). It only proves `bot.run` got the token. Mutations MAIN1 (drop `log_handler=None`) and MAIN2 (remove the "set DISCORD_CHANNEL_ID and SEC_USER_AGENT" exit) both survive. Replace with: missing env exits with the message, bad TOML exits "Can't read", valid run passes token and `log_handler=None`. Coverage: __main__.py lines 45, 48-50, 62 uncovered (measured). |

### tests/test_commands.py (1 test)

| test | verdict | reason |
|---|---|---|
| ping_replies_privately_with_latency | STRENGTHEN | Ephemeral/text mutation killed (CMD1), but unregistering the command (`ALL_COMMANDS = []`) survives (CMD2): it calls `ping.callback` directly and uses MagicMock. Add a check that `ALL_COMMANDS` contains `ping` and the bot's tree registers it. |

## Mutation checks (part 1: 39 run, 27 killed, 12 survived; measured)

Harness: python script rewrote one snippet in src, ran only the file's tests with `-x`, restored
the file in a `finally`. EXIT 1 = killed (a test failed), EXIT 0 = SURVIVED.

| id | test file | mutation | result |
|---|---|---|---|
| 1 | release_band | ramp branch `every = min(every, NEAR)` replaced by `pass` | killed |
| 2 | release_band | `paced()` returns True for SEC | killed |
| 3 | release_band | RECENT 4 to 5 | killed |
| 4 | release_band | session filter `if True` | killed |
| 5 | text | `named or pages` to `pages` | killed |
| 6 | text | hyphen-join regex removed from pdf_text | killed |
| 7 | text | drop `status != 200` guard in `_get_text` (TX2) | SURVIVED |
| 8 | watchdog | alarm dedupe removed (W1) | killed |
| 9 | watchdog | "polling again" message never sent (W2) | killed |
| 10 | watchdog | `limit = QUIET` (drop 5x interval) (W3) | SURVIVED |
| 11 | watchdog | never skip partial first line (W4) | SURVIVED |
| 12 | watchdog | mark alarm raised even if webhook post failed (W5) | SURVIVED |
| 13 | restart | window end `<` to `<=` (R1) | killed |
| 14 | restart | posted-alert check disabled (R2) | killed |
| 15 | formatting | overflow note removed (F1) | killed |
| 16 | clock | drop leap/stratum checks (C1) | killed |
| 17 | clock | pick slowest not fastest (C2) | killed |
| 18 | config | `discord_channel_id` default 7 (CF1) | SURVIVED |
| 19 | config | drop SEC UA regex (CF2) | killed |
| 20 | config | `env_ignore_empty=False` (CF3) | killed |
| 21 | config | server channel ids parse to `[]` (CF4) | SURVIVED |
| 22 | main | `bot.run(..., log_handler=None)` to no handler arg (MAIN1) | SURVIVED |
| 23 | main | remove missing-env `sys.exit` (MAIN2) | SURVIVED |
| 24 | commands | `ephemeral=False` (CMD1) | killed |
| 25 | commands | `ALL_COMMANDS = []` (CMD2) | SURVIVED |
| 26 | benchmark | skip X author check (B1) | killed |
| 27 | benchmark | skip earnings-text check (B2) | SURVIVED |
| 28 | benchmark | skip window filter (B3) | SURVIVED |
| 29 | chrome_cleanup | profile removal disabled (CH1) | killed |
| 30 | chrome_cleanup | live check by `if main` (CH2) | killed |
| 31 | chrome_cleanup | bot skips `sweep_leftover_chrome` (BOT1) | killed |
| 32 | resources | chrome_cpu_s +1 (RS1) | killed |
| 33 | resources | processes += 1 (RS2) | killed |
| 34 | resources | recorder stops writing "resources" (RS4) | killed |
| 35 | publication_trials | budget `turns < ? + 1000` (P1) | killed |
| 36 | publication_trials | `transition_observed=matched` (drop `absent and`) (P2) | SURVIVED |
| 37 | publication_trials | allow ID reuse (TR4) | killed |
| 38 | publication_trials | no response byte cap in fetch (TR2) | killed |
| 39 | publication_trials | trial turns not lowest priority (TR3) | killed |

Totals (measured): 39 mutations, 27 killed, 12 survived (ids 7, 10, 11, 12, 18, 21, 22, 23, 25,
27, 28, 36).

## Redundancy groups

- test_formatting: `times_are_eastern...` + `alert_times_keep_their_milliseconds` are one `clock()`
  table; `a_fiscal_period_reads_like_a_quarter` is a 3-row parameter set.
- test_release_band: 3 tests use the same `T`/window helpers with near-duplicate setup; fine, but
  `a_window_paces_every_route_but_secs_by_the_release_times` and `..._from_the_moment_it_opens`
  assert the same `polling_band` event at two layers (unit and end-to-end); keep both.
- test_chrome_cleanup: `with_nothing_left...` is the empty case of the sweep test.

## Slow tests (>0.5 s, measured, my files)

test_publication_trials: 3.18 s, 3.17 s, 3.03 s, 2.16 s, 1.00 s (real sleeps/intervals and real
host pacing of 1 request/s); test_resources chrome priority 2.01 s (spawns a hidden-desktop
subprocess); test_release_band watch 0.61 s (real time); test_chrome_cleanup kill test 0.61 s.

## Untested important behavior (coverage, measured)

1. watchdog.py 78% (38 lines): `post_webhook`, `main`, `planned_windows`, failed-post retry.
2. __main__.py 86%: environment-missing exit, bad schedule exit, summaries-off log.
3. clock.py 78%: `ask()` network function never exercised (a local UDP echo would do).
4. publication_trials.py 80% (44 lines): CLI `main` (lines 397-431), error paths 100-119.
5. config.py: `discord_server_channel_ids` parsing and `alert_channel_ids`.

## Suite-wide practice issues found in these files

- 5 tests use `with sqlite3.connect(...)`, which does not close; ResourceWarnings leak.
- Wall-clock tolerances (`approx(abs=0.05)`, `>= 2.9`, `< 0.40`) in 4 tests.
- Tests import private helpers (`runner._watch`, `fetcher._browser`, `child._job`).
- Imports from other test modules: test_publication_trials (FakePoster via test_unattended),
  test_release_band (test_recorder), test_resources (test_recorder), test_benchmark (test_pipeline).

---

## PART 2: suite-wide

### Full run (measured)

`uv run --no-sync pytest -q -p no:cacheprovider --durations=25 -W default`: **1216 passed, 74
warnings in 93.80 s** (wall 1 m 35 s). 737 test functions collect to 1216 items.

25 slowest (measured, call phase):

1. 6.12 s test_summary_evaluation::test_future_requests_share_policy_schema_and_default_pair
2. 6.02 s test_tracking_commands::test_a_ticker_outside_the_catalog_gets_its_alert_from_the_wire
3. 5.02 s test_unattended::test_calendar_updates_edit_one_card_and_keep_their_log
4. 4.18 s test_burst_replay::test_the_burst_replay_alerts_once_for_every_company_and_keeps_to_secs_limit
5. 3.18 s test_publication_trials::test_the_bot_runs_trial_plans_from_its_schedule_folder
6. 3.15 s test_publication_trials::test_a_trial_records_a_transition_and_stops_at_its_budget
7. 3.09 s test_summary_evaluation::test_future_requests_never_silently_truncate_a_release
8. 3.03 s test_servers::test_a_company_other_servers_track_is_watched_once_and_alerted_in_each
9. 3.01 s test_tracking_commands::test_a_tracked_issuer_with_no_schedule_file_gets_one_alert
10. 3.01 s test_publication_trials::test_trial_turns_wait_behind_monitoring_polls
11. 2.16 s test_publication_trials::test_a_trial_budget_survives_a_restart_and_its_id_cant_be_reused
12. 2.03 s test_bounded_research::test_date_discovery_researches_other_companies_behind_the_polls
13. 2.02 s test_resources::test_chrome_runs_below_normal_priority_under_a_memory_ceiling
14. 1.44 s test_fetch::test_polls_of_both_sec_hosts_and_exhibit_downloads_share_8_requests_a_second
15. 1.24 s test_browser::test_render_only_skips_settling_for_a_verified_target_quarter_pdf[Q3 2026 Earnings Release-other.example.net-4]
16. 1.23 s test_browser::test_a_render_turns_off_chromes_own_cache_before_it_navigates
17. 1.21 s test_browser::test_render_only_skips_settling...[Q2 2026 Earnings Release-ir.example.com-4]
18. 1.21 s test_browser::test_render_only_skips_settling...[Q3 2026 Earnings Call-ir.example.com-4]
19. 1.14 s test_source_observations::test_trial_never_stitches_a_candidate_from_different_entries[markup0]
20. 1.14 s same test [markup2]
21. 1.14 s same test [<a href="/old">...]
22. 1.14 s test_source_observations::test_trial_positive_records_the_matching_link_not_unrelated_page_links
23. 1.02 s test_release_timeout_replay::test_an_independent_issuer_posts_while_another_request_is_still_stalled[browser]
24. 1.02 s test_unattended::test_a_twenty_release_burst_keeps_one_message_per_release_after_restart
25. 1.02 s test_unattended::test_an_alert_retries_then_resumes_after_restart

The top 25 sum to about 60 s of the 94 s; nearly all are real `asyncio.sleep`/host-pacing time,
not CPU. 51 `sleep(` calls exist under tests/ (measured by grep).

### Warnings (measured)

The ~1000 figure is what a plain `pytest` run reports: **1039 warnings** (identical in the plain
serial run, a randomized run, and xdist). With `-W default` the "once per location" filter
collapses them to **74**. Breakdown of the 1039 by category and source:

- 1039 (100%) DeprecationWarning, all one site:
  `.venv/Lib/site-packages/discord/utils.py:1010: 'count' is passed as positional argument`
  (discord.py 2.7 calling `re.sub(regex, replacement, text, 0, re.MULTILINE)` on Python 3.13).
  Third-party. Triggered by our `discord.utils.escape_markdown` call at
  src/first_print/pipeline.py:615. By module: test_pipeline 929, test_unattended 62,
  test_burst_replay 15, test_browser_sources 15, test_release_timeout_replay 6,
  test_track_progress 3, test_nebius_ir 3, test_tracking_commands 2, test_referenced_writer 2,
  test_servers 1, test_source_document_flow 1.
- Hidden under that noise and only visible with `-W default` (the other 11 of the 74):
  ResourceWarning x9 (8 "unclosed database" sqlite3 connections, attributed by gc to
  test_recorder::test_fetcher_gives_up_on_a_server_that_never_answers,
  test_sources::test_the_fresh_pr_newswire_copy_asks..., and test_release_band shared-feed test,
  all really created at recorder.py:424 or by tests that use `with sqlite3.connect()`; 1
  unclosed asyncio StreamWriter) and ResourceWarning x2 (unclosed Popen pipes in
  test_chrome_cleanup kill test).

Verdict: no DeprecationWarning from our own package. The 1039 are third-party noise that buries
the 11 real ResourceWarnings, which ARE our leaks (tests and possibly recorder/Journal not
closing a sqlite connection). Filter the discord one by message, then make everything else an
error.

### Helpers, fakes, and a conftest

Files: fakes.py 294 lines, results_data.py 232, sec_data.py 42, browser_lane_replay.py 80,
burst_replay.py 996 (a replay harness, not a fake library; also run by test_burst_replay and
importable as `tests.burst_replay`).

Fake classes (measured by grep):

| class | defined in | note |
|---|---|---|
| RoutedFetcher | fakes.py | shared, 15+ importers; subclass of the real Fetcher |
| RoutedChrome, FakeSession | fakes.py | shared |
| FakeFetcher | test_recorder.py | a second fake Fetcher with a different request signature and canned-order semantics; imported by test_release_band and test_resources |
| QueuedFetcher | test_publication_trials.py | subclass of RoutedFetcher; imported by test_source_observations |
| FakePoster | test_pipeline.py AND burst_replay.py (different constructors) | same Poster interface, two implementations; imported from test_pipeline by 8 modules and re-imported from test_unattended by 3 more |
| FakeSummarizer | test_pipeline.py | |
| FakeChrome | test_investigate.py | parallel to RoutedChrome |
| FakeTabSocket | test_browser.py | imported by browser_lane_replay |
| FakeDiscord, FakeMessage | test_unattended.py | imported by 6 modules |
| FakeStream | test_summary.py | |

Fidelity: fakes subclass the real `Fetcher`/`Poster`, so signature drift is partly caught by
pyright (`include = ["src","tests"]`). But RoutedFetcher.request and FakeFetcher.request omit
the real method's `max_bytes`/observation behavior (publication_trials needed its own
QueuedFetcher to model host turns), so each fake covers a different slice and a new real kwarg
will not fail any fake. Recommend one contract test per fake (call the fake and the real
Fetcher with the same kwargs).

Test modules that import from other test modules (measured; 24 modules + 1 helper):

| importing module | imports | suggested home |
|---|---|---|
| test_benchmark | FakePoster (test_pipeline) | tests/fakes.py |
| test_bounded_research | company (test_ir_release_coverage), FakePoster (test_unattended) | fakes.py / builders.py |
| test_browser_sources | FakePoster (test_pipeline) | fakes.py |
| test_driver_profiles | SOURCE, TODAY, content, parsed (test_driver_profile) | tests/driver_data.py |
| test_globe_frontier | FakePoster (test_unattended) | fakes.py |
| test_investigate | AMAT, AMAT_NOTICE_BODY (test_date_discovery), filing_routes (test_pipeline), plus FakeChrome local | fakes.py / sec_data.py |
| test_ir_pages | AMAT (test_date_discovery) | sec_data.py or amat_data.py |
| test_nebius_ir | BrowserFake (test_browser_sources), FakePoster | fakes.py |
| test_publication_trials | FakePoster (test_unattended) | fakes.py |
| test_referenced_writer | FakePoster, fetcher, ir_sighting, uber (test_pipeline); summarizer_answering (test_summary); fact, value (test_summary_evidence) | fakes.py + a `builders.py` |
| test_release_band | CompanyFeed, FakeFetcher, ListSource (test_recorder) | fakes.py |
| test_release_cards | FakeDiscord, FakeMessage (test_unattended) | fakes.py |
| test_resources | FakeFetcher (test_recorder) | fakes.py |
| test_sec_results | FakeDiscord (test_unattended) | fakes.py |
| test_servers | FakePoster; GUILD, ReleaseDay, ShiftedClock, call (test_tracking_commands); INVESTIGATED, NOW, SPEED, WatchedDiscord (test_track_progress); OWNER, client (test_tracking_commands) | tests/discord_fakes.py |
| test_source_audit | listing, row (test_globe_history) | fakes.py/builders |
| test_source_document_flow | several (test_pipeline) | builders.py |
| test_source_observations | QueuedFetcher, events, plan (test_publication_trials) | fakes.py |
| test_summary_binding / test_summary_bound_format | fact, value (test_summary_evidence); TABLE (test_summary_binding) | tests/summary_data.py |
| test_tracking_commands | LocalClock (burst_replay), FakePoster, FakeDiscord | fakes.py |
| test_track_progress | uber (test_pipeline), FakeDiscord, FakeMessage | fakes.py |
| test_track_test | NOW, NVDA_CIK, OWNER, client (test_tracking_commands) | discord_fakes.py |
| test_unattended | FakePoster, uber (test_pipeline) | fakes.py |
| tests/browser_lane_replay.py | FakeTabSocket (test_browser) | fakes.py |

Should there be a tests/conftest.py? Yes. Reasons: (1) move shared fixtures/factories out of
test modules (above) so tests stop depending on one another's collection; (2) an autouse
fixture to isolate env: several tests call `Settings()`/`restart.main`/`entry.main` and defend
by `monkeypatch.chdir(tmp_path)` and `_env_file=None` each time; one autouse fixture could
`monkeypatch.delenv` `DISCORD_*`, `SEC_USER_AGENT`, `META_MODEL_API_KEY`, `FMP_API_KEY`,
`LIVE_SUMMARIES` and chdir to tmp_path, so a developer's real `.env` or shell variables cannot
change a test; (3) a `no_network` guard (socket patch that allows 127.0.0.1) to prove the "tests
use fakes" claim; (4) a `fake_clock` fixture to replace 51 real sleeps; (5) a `closing` sqlite
helper. Fixture count today: only 8 `@pytest.fixture` in 737 tests (measured), so almost all
setup is helper-function copy-paste.

### Config vs modern 2026 practice (measured/read)

Current `[tool.pytest.ini_options]`: `testpaths`, `asyncio_mode = "auto"`,
`asyncio_default_fixture_loop_scope = "function"`. Missing:

- `--strict-markers` and `--strict-config` (via `addopts`; pytest 9 also has `strict = true`).
  Typos in markers currently pass silently. The suite uses only `skipif`/`skip`, so adopting is
  free.
- `xfail_strict = true`. Zero xfails exist now (measured), so it is free insurance.
- `filterwarnings = ["error", "ignore:'count' is passed as positional:DeprecationWarning:discord.utils"]`
  (plus `error::ResourceWarning` after fixing the 11 leaks). The current setup lets 1039 warnings
  scroll past unseen. At minimum `error::DeprecationWarning:first_print`.
- `--import-mode=importlib`: tests rely on `tests/__init__.py` and `from tests.x import` (read);
  importlib mode works only with `pythonpath = ["."]` and forbids cross-test-module imports, which
  is the right pressure to move helpers into non-test modules. Do this after the helper move.
- `pytest-asyncio` settings are fine (`auto`, function loop scope). Add
  `asyncio_default_test_loop_scope = "function"` explicitly (pytest-asyncio 1.x).
- `timeout`: add pytest-timeout (e.g. 60 s, `timeout_method = "thread"`): a hanging async test
  currently hangs CI until the job limit.
- `addopts = "-ra"` to show skips/xfails in summaries.
- `--durations=10` in CI to track the slow tail.

Experiments (measured):

- **pytest-randomly** (two runs: one default seed, one `--randomly-seed=424242`): both **1216
  passed**, 90.7 s and 89.7 s. No order-dependent failures found.
- **pytest-xdist** `-n auto` (32 CPUs): **1216 passed in 29.06 s** (31.0 s wall) versus 93.8 s
  serial: 3.2x faster. No failures, so tests are already parallel-safe (tmp_path state,
  ephemeral loopback ports). CI runner has fewer cores; expect about 2x on 4 cores.
- Coverage in CI: **none today** (ci.yml has no `--cov`). Per-file coverage measured for my 13
  files ranges 78% to 100%; run ephemerally, not enforced.
- Mutation testing: mutmut/cosmic-ray are feasible only per-module (the full suite takes 94 s
  serial, 29 s with xdist, so a whole-package run is too slow). Targeted runs on pure modules
  (release_band, text, formatting, clock, restart, config, watchdog) with `-n auto` are
  realistic. My 39 manual mutations found 12 survivors in 13 files, which suggests a periodic
  mutmut run on `src/first_print/{release_band,text,watchdog,config,formatting}.py` would pay
  off. mutmut 3 on Windows needs WSL or a Linux runner (it uses fork); run in a scheduled
  Linux CI job.
- Hypothesis/property tests: **none** (zero imports, measured). Parsing code is the natural fit:
  `html_to_text` (never raises, idempotent, no `|`-only lines), `pick_exhibit`, `parse_reply`
  (never raises on random bytes, returns Sample or None), `pace` (every >= own, change > 0 or
  None, monotone around release times), `period_label`, `duration`, `bullets` (len <= limit).
  Add hypothesis as a dev dependency and put property tests beside the example tests.
- Parametrize ids: 123 `parametrize` marks, only 26 `ids=` (measured). Auto ids such as
  `[Q3 2026 Earnings Release-other.example.net-4]` and a raw-bytes `$\x00...` id appear in
  failures; add ids or `pytest.param(..., id=...)`.
- monkeypatch vs global patching: good. 145 `monkeypatch.setattr`, 1 `unittest.mock.patch`
  (measured). Exceptions: burst_replay.py assigns `fetch._SHARED_QUEUES` and restores in
  `finally` (acceptable in a helper, but monkeypatch is safer), test_bounded_research assigns
  `dates.in_window = lambda ...` on an instance and test_browser sets `chrome._start`
  (instance, no leakage).
- tmp state and clocks: tests use `tmp_path`; some depend on `datetime.now()`
  (test_restart `now + 3 minutes`, test_clock real time). Fine but fragile.
- Skips/xfails inventory (`grep -rn "skip\|xfail" tests`, measured): 0 xfail. 5 skips:
  test_bounded_research.py:175 `pytest.skip` (Windows-only Chrome research);
  test_chrome_cleanup.py:87 skipif win32 + :96 runtime skip when no hidden desktop;
  test_resources.py:63 skipif win32 + :66-67 redundant in-function skip + :73 runtime skip. The
  rest are `fmt: skip` comments and fixture HTML. Windows-only tests are real coverage only on
  the Windows CI job; the Linux job reports them skipped (use `-ra` to see).

### CI (read .github/workflows/ci.yml and docker.yml)

ci.yml: jobs `linux` (ubuntu-latest: `uv sync --locked`, release-version check on PRs, `ruff
check`, `ruff format --check`, `pyright`, `uv run pytest`) and `windows` (windows-latest: sync,
`uv run pytest`), gated by a `checks` aggregator. Concurrency cancels superseded runs; actions
are hash-pinned. docker.yml builds (and on main/tags publishes) the image; it runs no tests.

What CI misses:
1. No coverage measurement or threshold; no coverage artifact or diff.
2. No warnings policy (`-W error`), so third-party deprecations and the 11 ResourceWarnings never
   fail anything.
3. No randomized order and no xdist: order dependence could creep in unseen; CI is 94 s serial.
4. No timeout, so a hung async test burns minutes to hours.
5. No `--durations` reporting or time budget for the slow tail.
6. No scheduled (nightly) job for mutation testing, property-test deep runs, or a latest-deps
   run (`uv lock --upgrade` canary: discord.py/aiohttp/pydantic break things first).
7. Windows job does not run lint/pyright (platform-specific type branches such as ctypes code
   are only type-checked on Linux).
8. The Docker image is never smoke-tested (e.g. `first-print --version`).
9. Single Python version (3.13), which matches `requires-python >=3.13`; fine, but add the next
   version as an allowed-failure canary.

## Prioritized suite-wide changes

1. Add `filterwarnings = error` (with an ignore for the single discord.py `re.sub` DeprecationWarning)
   and fix the 11 real ResourceWarnings (unclosed sqlite connections, Popen pipes, StreamWriter).
   Cheap, and immediately stops 1039 lines of noise from hiding real leaks.
2. Move shared fakes and builders out of test modules into `tests/fakes.py` (+ `tests/conftest.py`
   fixtures). 24 modules import from sibling test modules; FakePoster has two implementations; add
   an autouse env/cwd isolation fixture. Then switch to `--import-mode=importlib`.
3. Run CI with `pytest -n auto -p randomly --timeout=60` (xdist 29 s vs 94 s measured; two random
   orders already pass), plus `--strict-markers --strict-config`, `xfail_strict`, `-ra`.
4. Replace real sleeps in the slowest tests (publication_trials x4, release_band watch,
   tracking_commands, unattended, summary_evaluation) with an injected fake clock; the top 10
   tests cost about 40 s. Add a `fake_clock` fixture.
5. Close the 12 surviving mutation gaps with targeted tests: benchmark earnings-text and
   window checks; `transition_observed` first-poll case; watchdog failed-post retry, 5x interval,
   mid-file start; config server-channel parsing and `alert_channel_ids`; `__main__` env/schedule
   error exits; `ALL_COMMANDS` registration; `_get_text` non-200 with a long body. Rewrite
   test_main (its name promises behavior that `main()` does not have).
6. Add hypothesis property tests for the pure parsers (`html_to_text`, `parse_reply`, `pace`,
   `pick_exhibit`, `bullets`, `duration`) and a nightly Linux mutmut job on the pure modules;
   enable coverage reporting (no threshold at first) in CI.

---

## FIX PASS (branch tests-fix-f; numbers measured)

Survivors (whole suite run with each mutation in place, then retested):

| id | outcome |
|---|---|
| P2 | gap fixed: `test_a_release_already_up_at_the_first_poll_is_not_a_measured_transition` |
| TX2 | gap fixed: `test_a_missing_page_or_file_has_no_text` now 404/403/500 with a release-sized body |
| W3 | gap fixed: `test_a_slow_route_gets_five_of_its_own_poll_intervals_before_it_counts_as_down` |
| W5 | gap fixed: `test_an_alarm_whose_webhook_post_failed_is_tried_again_at_the_next_check` |
| W4 | equivalent: a cut first line is rejected by `json.loads` anyway, so skipping it changes nothing observable (the whole suite only failed on unrelated load flakes). Added `test_a_long_journal_is_read_from_its_tail_only` for the tail read |
| CF1 | already caught elsewhere: test_restart `the_check_needs_the_alert_channel` and test_chrome_cleanup `the_bot_sweeps_leftover_chrome...`; also added config tests |
| CF4 | already caught by tests/test_servers.py (G) |
| MAIN1, MAIN2 | gap fixed: test_main rewritten (4 tests) |
| CMD2 | gap fixed: `test_the_bot_registers_ping_in_its_command_tree` |
| B2, B3 | gap fixed: benchmark window test has a non-earnings embed and an out-of-window valid post |

Slow tests: publication_trials 3.2/3.2/3.0/2.2 s -> 0.35/0.30/0.07/0.29 s (host gap patched via
`fetch._DEFAULT_GAP`, wall-clock assert removed); source_observations 1.2 s x4 -> 0.15 s;
release_band watch 0.69 s -> 0.01 s (virtual time, real `pace`). Skipped: test_resources
priority test and chrome kill test (real hidden-desktop subprocesses; REWRITE needs a public
accessor in source), window_report REWRITE (harness shared with test_unattended, not mine).
