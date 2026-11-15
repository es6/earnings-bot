# Test audit D: sources, browser, fetch, SEC results, GlobeNewswire, source audit

Base: origin/main 0dc9d97 (v0.21.35). All numbers measured in this worktree on Windows.
Run: `uv run --no-sync pytest <8 files> -q --durations=20` = 196 passed (116 test functions, 196 items after parametrization) in 17.3 s. 15 DeprecationWarnings, all from discord/utils.py (`re.sub` positional `count`), not from our code. No Chrome was started by these files (Chrome process count after the run: 0). Worktree is clean (`git status` empty).

## Verdict totals (per test function, 116 functions; measured by hand-count)

| file | functions | KEEP | STRENGTHEN | MERGE | REWRITE | DELETE |
|---|---|---|---|---|---|---|
| test_sources.py | 32 | 24 | 6 | 1 | 1 | 0 |
| test_browser_sources.py | 20 | 13 | 5 | 2 | 0 | 0 |
| test_browser.py | 8 | 5 | 3 | 0 | 0 | 0 |
| test_fetch.py | 11 | 8 | 2 | 1 | 0 | 0 |
| test_sec_results.py | 23 | 16 | 5 | 1 | 1 | 0 |
| test_globe_frontier.py | 11 | 7 | 4 | 0 | 0 | 0 |
| test_globe_history.py | 3 | 2 | 1 | 0 | 0 | 0 |
| test_source_audit.py | 8 | 6 | 1 | 0 | 1 | 0 |
| total | 116 | 81 | 27 | 5 | 3 | 0 |

The suite is in good shape: no test is obsolete or tautological as a whole, and 45 mutations were run (see below), with most behavior killed. The weaknesses are: a handful of unpinned branches (survivors), timing-based assertions, heavy coupling to private helpers in test_browser/test_fetch/test_sec_results, misfiled tests, and cross-test-module imports.

## test_sources.py

| test | verdict | reason |
|---|---|---|
| test_looks_like_earnings | STRENGTHEN | No case isolates the `is_date_notice` clause (M1: removing it still passes this test; it is only caught by the results_headline test). Add "Acme to Report Third Quarter 2026 Results on November 5" -> False |
| test_a_results_headline_reports_on_a_period | KEEP | Good positive/negative table; caught M1b |
| test_a_headline_agrees_with_the_planned_quarter | KEEP | Right test, wrong file: exercises periods.agrees_with_period (move to a test_periods file) |
| test_host_gaps | MERGE | Pins constants already asserted via host_gap(queue_key(...)) in test_fetch; fold into one parametrized host-gap test |
| test_q4_press_releases | KEEP | Clear parse test |
| test_q4_requests_carry_a_buster_for_palantirs_cdn | KEEP | Small, asserts the contract |
| test_q4_polls_take_turns_with_longer_lists_once_their_copies_expire | KEEP | Injected clock, no sleep; caught M5 |
| test_q4_bodies_url_asks_for_full_html_at_the_given_length | KEEP | Caught M6 |
| test_q4_reads_only_the_normal_lists_length | KEEP | Caught M3 |
| test_q4_financial_reports | KEEP | |
| test_q4_financial_reports_webcast_link_isnt_the_release (4 params) | KEEP | Caught M2; casing/whitespace variants each exercise strip+casefold |
| test_edgar_latest_reads_one_companys_8ks_from_the_live_feed | STRENGTHEN | Bundles url, name, parse and an invalid-XML error in one test (caught M7). Split the `not valid XML` assert out; it duplicates test_rss_rejects_html |
| test_edgar_flags_item_2_02_8ks | KEEP | Covers 2.02, 5.02 and non-8-K |
| test_rss_keeps_items_naming_the_company | KEEP | |
| test_pr_newswire_items_match_on_headline_or_contributor | KEEP | Realistic multi-input fixture with rules explained |
| test_rss_without_companies_keeps_everything | KEEP | Trivial but distinct branch; could join the company-match test as a param |
| test_rss_cache_buster_keeps_the_feed_id | KEEP | |
| test_rss_rejects_html | KEEP | |
| test_guessed_file | KEEP | Name is vague ("test_guessed_file"); behavior asserted is fine |
| test_guessed_file_answered_with_a_web_page_isnt_there_yet | KEEP | Caught M4 |
| test_guessed_page_is_named_after_its_last_folder | KEEP | |
| test_atom_feed | KEEP | |
| test_guessed_page_isnt_there_until_it_answers_with_a_page | KEEP | |
| test_guessed_pdf_page_must_be_a_pdf | KEEP | |
| test_broadcom_newsroom | STRENGTHEN | One test asserts earnings flag, fields, cache buster and error. Split into parse / buster / not-a-list |
| test_published_time (10 params) | KEEP | Caught M39 |
| test_one_feed_watched_for_several_companies_is_polled_once | STRENGTHEN | Name promises "polled once" but only compares `share_key` attributes. Assert through `Polls` that two windows cause one request |
| test_backup_and_cached_feeds_dont_take_turns_from_the_fast_ones | REWRITE | Pins class constants (`interval`), a change-detector. The real invariant (host request budget not exceeded) is only checked in the next test for PR Newswire; assert it for the SEC pair and Q4 pair via the real budget |
| test_the_fresh_pr_newswire_copy_asks_for_a_new_spelling_every_poll | STRENGTHEN | Five behaviors in one (spelling, share_key x2, budget). Uses unseeded `random.sample` (assertion is robust to it, but unseeded). Split |
| test_the_fresh_copy_reads_the_feed_like_the_cached_one | KEEP | |
| test_fresh_pr_newswire_rejects_general_news (2 params) | KEEP | Caught M41 |
| test_fetch_preserves_the_encoded_spelling_on_the_wire | STRENGTHEN | Survivor M40: FakeSession doesn't record `allow_redirects`, so removing `allow_redirects=False` from FreshPrNewswire.fetch passes. Record it and assert |

## test_browser_sources.py

| test | verdict | reason |
|---|---|---|
| test_only_scheduled_issuer_article_is_fetched_and_reused (2 params) | KEEP | Strong; asserts exact chrome calls and no HTTP |
| test_listing_only_or_stub_does_not_signal_a_complete_release | KEEP | |
| test_issuer_http_watch_uses_one_transport_and_keeps_chrome_as_fallback (2 params) | STRENGTHEN | Survivor M32: the HTTP leg doesn't assert `fetcher.no_redirect`, so redirects can be re-enabled. Add the assertion |
| test_a_refused_chrome_fallback_says_why_http_gave_way_and_http_is_tried_again | STRENGTHEN | Good, but survivors M26 (HTTP 404 on an urgent read accepted as http answer) and M27 (`_chrome_until` reset on a Chrome error) have no test. Add two small tests |
| test_pdf_ignores_missing_files_and_html_and_reuses_valid_bytes | KEEP | Caught M30 |
| test_existing_browser_pdf_is_caught_up_without_claiming_a_live_detection | KEEP | |
| test_tesla_scope_preserves_a_named_identity_and_rejects_other_urls | STRENGTHEN | Caught M31, but two unrelated behaviors (request shape, constructor rejection); split |
| test_browser_start_failure_stops_only_the_browser_source | STRENGTHEN | Name claims "only the browser source" but it asserts only `fatal`; assert other sources keep running or rename |
| test_schedule_migrates_legacy_ir_independently_of_other_trials | STRENGTHEN | Five behaviors (migration, TOML round trip, trial.ir, two validation errors); split |
| test_lucids_listing_picks_only_the_quarters_results | MERGE | Same flow as test 1 with a different headline pattern; fold into a parametrized listing test |
| test_browser_rss_alert_is_visible_before_the_article_is_ready (2 params) | STRENGTHEN | Valuable but 100 lines with a positional script of 7 replies and exact retry counts (`browser_calls[-5:]`); couples to retry policy. Name the scenarios, assert behavior (alert before attach, PDF attached) |
| test_planner_prepares_browser_feed_after_reading_a_verified_previous_report | KEEP | Asserts exact `chrome_calls`, acceptable |
| test_browser_feed_and_listing_split_the_same_hosts_poll_budget | KEEP | Invariant, not a constant |
| test_amat_results_are_read_when_the_date_notice_is_listed_first | MERGE | Same shape as the Lucid/FICO listing tests; one parametrized test with (listing, pattern, expected) |
| test_live_chrome_posts_or_edits_one_alert_with_the_issuer_pdf (2 params) | KEEP | End-to-end through Releases with a fake poster |
| test_chrome_may_open_any_public_https_page (4 params) | KEEP | Misfiled: tests browser.browser_host, belongs in test_browser.py |
| test_chrome_never_opens_this_pc_or_its_network (14 params) | KEEP | Security-relevant, caught M10/M11/M12. Same misfile |
| test_chrome_feed_keeps_watching_when_an_item_is_outside_the_window (2 params) | KEEP | |
| test_a_quarterly_table_follows_the_issuer_pdf_to_its_file_host | KEEP | |
| test_a_prior_fiscal_year_table_cannot_arm_the_next_year | KEEP | Misfiled (tests ir_research.verified_results_table) |

## test_browser.py

| test | verdict | reason |
|---|---|---|
| test_a_render_turns_off_chromes_own_cache_before_it_navigates | STRENGTHEN | Caught M9. 1.22 s because `render()` really sleeps 0.3 s x 4; patch `asyncio.sleep` in earnings_bot.browser. Monkeypatches private `_start/_new_tab/_drop` and pokes `_tabs` |
| test_a_slow_answer_times_out_without_losing_the_warm_tab | KEEP | Caught M37 |
| test_render_only_skips_settling_for_a_verified_target_quarter_pdf (4 params) | STRENGTHEN | Caught M34; 1.2 s each for 3 of 4 params (real settle sleeps); counts `_SETTLED` calls (private constant); local imports inside the function |
| test_research_yields_after_pacing_without_dropping_a_live_warm_tab | KEEP | |
| test_an_urgent_read_does_not_wait_for_a_slow_listing_poll_on_its_host | STRENGTHEN | Caught M36 but uses real sleeps (1.0 s) and wall-clock `< 0.5` and `tab_lock_ms < 100`; flaky under load. Use asyncio.Event gating instead of sleep |
| test_the_urgent_tab_is_warmed_once_the_polling_tab_is | KEEP | |
| test_closing_chrome_cancels_and_joins_speculative_warmup | KEEP | |
| test_an_urgent_read_cancels_lower_priority_speculative_pacing | KEEP | Caught M44 |

Smells: four copies of the same `started`/`wait_turn`/`drop` stub closures (extract a `chrome` fixture); `from tests.test_browser import FakeTabSocket` is used by non-test module tests/browser_lane_replay.py (move FakeTabSocket to tests/fakes.py); many imports of private names.

## test_fetch.py

| test | verdict | reason |
|---|---|---|
| test_every_sec_host_waits_in_one_queue_at_8_requests_a_second | KEEP | Caught M8; absorb test_host_gaps here |
| test_polls_of_both_sec_hosts_and_exhibit_downloads_share_8_requests_a_second | STRENGTHEN | 1.44 s real time. `most_in_one_second(times) == 8` is exact on a real clock: on a loaded machine gaps > 0.125 give 7 and it fails. Use `<= 8` (the actual invariant) |
| test_an_urgent_exhibit_download_goes_ahead_of_polls_of_the_other_sec_host | KEEP | Caught M20; 0.53 s |
| test_other_hosts_dont_wait_behind_a_full_sec_queue | STRENGTHEN | `elapsed < 0.05` wall clock; creates 20 tasks and cancels without awaiting them (pending-task noise) |
| test_sec_redirects_and_connection_retries_wait_for_the_shared_queue | KEEP | Real loopback aiohttp server (127.0.0.1 only), no public network. Monkeypatches `aiohttp.ClientSession` and reads private `_queues`; heavy but unique coverage |
| test_background_research_waits_behind_every_poll_and_paces_sec | KEEP | Caught M19 (0.92 s real) |
| test_background_research_gives_way_to_a_host_a_window_is_polling | KEEP | 1.0 s because the last step really waits a host gap; patch `host_gap`. See M18/M18b below: survivors are explained by a redundant check in `_dispatch`, not a test gap |
| test_background_research_without_chrome_cannot_open_it | KEEP | |
| test_background_research_cannot_use_the_shared_live_browser | KEEP | Sets private `_session/_browser` |
| test_queued_research_yields_without_a_turn_when_a_window_opens | KEEP | Reads private `_queues[...].next_turn` |
| test_a_queued_background_turn_is_not_spent_when_the_hot_interval_begins | MERGE | Same as the previous test with `paused` instead of `active`; parametrize |

## test_sec_results.py

| test | verdict | reason |
|---|---|---|
| test_financial_boilerplate_cannot_turn_a_6k_into_results (3 params) | STRENGTHEN | Negatives only (no positive control in the same test) and the 3 `description` params behave identically; one description plus a paired positive is stronger |
| test_both_sec_watchers_verify_once_and_alert_only_on_results | KEEP | Real-time 0.35 s window with real polls; small flake risk |
| test_saved_bn_profile_is_enriched_and_scheduled_from_issuer_notice | STRENGTHEN | ~7 behaviors in one test of DateDiscovery (misfiled here), calls private `dates._check` |
| test_source_audit_anchors_on_verified_6k | KEEP | Misfiled (source_audit) |
| test_klarna_style_results_exhibit_leads_to_issuer_q4_notice | STRENGTHEN | 70 lines of inline JSON, many asserts; move data to a fixture/file; misfiled (date discovery) |
| test_an_issuer_notice_filed_on_6k_can_schedule_but_never_alert | KEEP | Private `_check` call |
| test_scorecard_explains_busy_and_missing_date_states | STRENGTHEN | TrackProgress rendering with substring snapshots including emoji; misfiled; private `_check` |
| test_the_ir_sites_own_time_for_the_release_joins_the_comparison | KEEP | Misfiled |
| test_a_press_release_after_the_exchanges_disclaimers_is_recognized_as_results | KEEP | `assert SCHEDULE.index(...) > 2500` tests the fixture, not the code; fine as a guard but note |
| test_pleased_to_announce_results_is_results_and_not_a_date_notice | KEEP | Caught M21 |
| test_the_speed_review_times_sec_by_the_days_first_results_6k | KEEP | Misfiled |
| test_reports_third_quarter_results_is_an_announcement | MERGE | Identical pos/neg `is_results_release` pattern as the previous two; one parametrized test |
| test_a_q4_time_counts_as_eastern_only_when_sec_confirms_it | KEEP | Misfiled (source_audit.site_local_time) |
| test_a_results_6k_can_begin_after_a_full_cover_page | KEEP | |
| test_followup_verifies_a_results_6k_accepted_after_the_window | KEEP | |
| test_after_midnight_6k_is_verified_and_catches_up_inside_the_window | STRENGTHEN | Calls private `recorder._verify_sixks` and `record_items`; drive through a public path |
| test_a_results_release_names_the_period_it_reports (6 params) | KEEP | |
| test_a_headline_line_names_the_period_too (3 params) | KEEP | |
| test_a_comparison_with_last_year_isnt_the_period_reported | KEEP | |
| test_an_8k_under_other_events_alerts_only_when_its_exhibit_is_results | KEEP | Real-time 0.35 s window |
| test_an_8k_under_results_of_operations_needs_no_exhibit_check | REWRITE | Imports underscore `_filing_type`; assert via `EdgarFilings.parse` output instead |
| test_a_release_under_a_headline_that_isnt_earnings_is_known_by_its_text | KEEP | Caught M22 |
| test_a_text_check_wants_this_quarters_results_with_revenue_figures | KEEP | Five negatives, no positive in the same test (positive is in the previous test) |

## test_globe_frontier.py

| test | verdict | reason |
|---|---|---|
| test_guessing_takes_six_minutes_split_among_the_release_times | KEEP | |
| test_only_a_release_on_globenewswire_alone_is_guessed | KEEP | |
| test_the_release_is_the_companys_ticker_with_this_quarters_results | KEEP | |
| test_guessing_finds_the_page_as_it_opens_with_a_new_url_each_time | STRENGTHEN | Survivor M15: "no URL asked twice" checks distinct URLs, but every URL is a fresh spelling so it never detects re-guessing an already opened number. Add a scenario where 102 opens before 101 and assert 102 is requested once |
| test_a_refusal_ends_the_guessing | KEEP | Caught M17; asserts internal `frontier.sent == 3` |
| test_the_budget_caps_the_guesses | STRENGTHEN | `assert BUDGET == 540` pins a constant: drop it |
| test_a_window_posts_the_guessed_page_as_a_globenewswire_sighting | STRENGTHEN | The expected list contains `posted[0][1]` (compares the URL with itself); assert the real URL pattern. Depends on `expected=now+0.2` real clock |
| test_a_window_starts_the_guessing_only_for_a_globenewswire_release | KEEP | Overlaps test 2 at a different level (recorder wiring) |
| test_a_batch_records_every_page_and_posts_nothing | STRENGTHEN | "posts nothing" is not asserted: no poster or notify hook is wired. Pass a notify that raises |
| test_the_bot_runs_globenewswire_batches_from_its_schedule_folder | KEEP | Good integration test |
| test_when_every_guess_is_out_the_next_numbers_open_at_once | KEEP | Caught M16 |

## test_globe_history.py

| test | verdict | reason |
|---|---|---|
| test_a_listing_gives_each_release_its_minute_organization_and_link | KEEP | |
| test_a_companys_releases_come_from_its_own_organization_page | STRENGTHEN | Survivor M14: no org page ever contains another organization's rows, so the `listed_by == organization` filter is untested. Also a second unrelated assert (`RoutedFetcher({})` -> []) should be its own test |
| test_another_company_with_a_similar_name_isnt_taken_for_it | KEEP | Caught M13 |

## test_source_audit.py

| test | verdict | reason |
|---|---|---|
| test_last_release_ranks_body_separately_from_headline | STRENGTHEN | Imports private `_measurements`; name doesn't match the content (measurements parsing + X benchmark) |
| test_the_archive_reads_one_copy_of_each_feed | REWRITE | Private `_archived_sources` and depends on the live `known_companies()["LLY"]` data: edits to companies.toml break it. Use an inline company |
| test_live_sightings_beat_publisher_timestamps_and_timestamps_beat_nothing | KEEP | Caught M38 |
| test_a_saved_speed_check_reads_back_and_an_old_one_doesnt | KEEP | |
| test_the_release_history_leaves_out_preliminary_results | KEEP | Caught M23 and M24 |
| test_a_q4_time_counts_as_eastern_only_near_the_results_8k | KEEP | Uses `known_companies()["GOOGL"]` data but overrides fields |
| test_a_globenewswire_issuers_history_reads_releases_without_results_headlines | KEEP | Starts from `known_companies()["NVDA"]` and overwrites name/wire; build the Company directly |
| test_a_check_saved_by_an_older_release_history_reads_back_without_it | KEEP | Overlaps the earlier-format assert in the saved-speed test (different key, history_version) |

## Mutation checks (45 run; each reverted, git status clean at the end)

| # | test(s) | mutation | result |
|---|---|---|---|
| M1 | test_looks_like_earnings | drop `is_date_notice` from looks_like_earnings | SURVIVED (caught by the results_headline test, M1b, so a gap in this test only) |
| M1b | test_sources.py | same | caught |
| M2 | q4 webcast test | remove Online filter | caught |
| M3 | q4 normal-length test | drop slice to page_size | caught |
| M4 | guessed file web-page test | disable text/html soft-404 check | caught |
| M5 | q4 turn-taking | skip refresh expiry | caught |
| M6 | q4 bodies url | bodyType stays 0 | caught |
| M7 | edgar_latest test | disable Item 2.02 detection | caught |
| M8 | SEC queue test | `queue_key` returns host | caught |
| M9 | browser cache test | remove `Network.setCacheDisabled` | caught |
| M10 | chrome_never | drop port check | caught |
| M11 | chrome_never | drop fragment check | caught |
| M12 | chrome_never | drop userinfo check | caught |
| M13 | globe_history similar name | drop `_significant` equality | caught |
| M14 | globe_history | drop `listed_by == organization` filter | SURVIVED |
| M15 | globe_frontier | re-guess already opened numbers | SURVIVED |
| M16 | frontier burst | `count = AHEAD` always | caught |
| M17 | frontier refusal | `continue` instead of return "refused" | caught |
| M18 | test_fetch | remove first HostBusy raise (live host) | SURVIVED, equivalent: `_dispatch` also blocks the turn; the test checks observable behavior |
| M18b | test_fetch | remove both inline HostBusy checks | SURVIVED, same reason (dispatcher enforces it) |
| M19 | background SEC pacing | drop `_background_next` update | caught |
| M20 | urgent priority | urgent -> priority 1 | caught |
| M21 | sec_results | only description checked for notices | caught |
| M22 | sec_results | skip `verified_report` | caught |
| M23 | source_audit | stop dropping preliminary headlines (item.title) | caught |
| M24 | source_audit | stop dropping preliminary exhibit | caught |
| M26 | browser_sources | accept urgent 404 from HTTP | SURVIVED |
| M27 | browser_sources | don't reset `_chrome_until` on Chrome error | SURVIVED |
| M29 | browser_sources | don't keep Chrome for 30 s | caught |
| M30 | browser_sources | skip `%PDF-` magic check | caught |
| M31 | browser_sources | Tesla `allow_redirects=True` | caught |
| M32 | browser_sources | HttpRelease `allow_redirects=True` | SURVIVED |
| M34 | browser render | never call `ready()` early return | caught |
| M36 | browser request | urgent read uses the listing tab key | caught |
| M37 | browser request | drop tab on slow answer | caught |
| M38 | source_audit.fastest | ignore `watched_from` | caught |
| M39 | published_time | accept naive stamps | caught |
| M40 | FreshPrNewswire.fetch | allow redirects | SURVIVED |
| M41 | FreshPrNewswire | `verify_channel = False` | caught |
| M44 | browser request | urgent no longer cancels prewarm | caught |

(M25, M28, M33, M35, M42, M43 were scouting greps, not mutations. Count of executed mutation runs: 40 distinct, 45 including the M1b/M4/M18b reruns and M31/M32 correct-target reruns.)

Survivors needing action: M14, M15, M26, M27, M32, M40 (6 real gaps); M1 (gap in one test only); M18/M18b (equivalent).

## Redundancy groups

- Host-gap constants: test_sources::test_host_gaps + test_fetch::test_every_sec_host... (one parametrized test).
- Listing-to-article flow with different headline patterns: browser_sources tests 1, Lucid, AMAT (one parametrized test).
- Background-research "yield without taking a turn": test_fetch active vs paused variants (parametrize).
- `is_results_release` positive/negative pairs: three separate tests in test_sec_results (parametrize).
- Q4 Eastern-time rule: test_sec_results::..._only_when_sec_confirms_it and test_source_audit::..._only_near_the_results_8k (unit vs integration; keep both but co-locate).
- Older-format Speed read-back in two tests in test_source_audit.

## Slow tests (measured, >0.5 s)

| test | s | why |
|---|---|---|
| test_fetch polls of both SEC hosts ... 8 requests a second | 1.44 | real 8/s pacing on the real clock |
| test_browser render_only_skips_settling x3 params | 1.22 each | `render()` really sleeps 0.3 s per sample, 4 samples |
| test_browser render turns off cache | 1.22 | same |
| test_fetch background research gives way | 1.01 | last step waits a real 1 s host gap |
| test_browser urgent read does not wait | 1.00 | deliberate real 1.0 s slow listing |
| test_fetch background waits behind every poll | 0.92 | 0.4 s paced real turns |
| test_fetch urgent exhibit goes ahead | 0.53 | real SEC gaps |

Fix: monkeypatch `asyncio.sleep` / `host_gap` or inject a clock. Total for the 8 files 17.3 s; about 10 s of that is real sleeping.

## Untested important behavior (coverage, measured)

Coverage of my area: sources.py 94%, fetch.py 81%, browser.py 52%, browser_sources.py 80%, sec_results.py 93%, globe_frontier.py 90%, globe_history.py 98%, source_audit.py 84%.
1. browser_sources.HttpRelease.request_page: HTTP 404 on an urgent read and the `_chrome_until` reset after a Chrome failure (M26/M27; lines 422-424).
2. Frontier never re-guesses an already opened number when pages open out of order (M15; globe_frontier 107-124, 163 partly).
3. fetch.Fetcher.observe (record-only trial limits, fetch.py 257-265) and the `research_browser` / `browser_request` / `browser_render` construction paths (302-356): no test in these files (may be covered by trial tests elsewhere, which I did not audit).
4. Spellings `_draw` fallback when most spellings are used and `spelling_positions` (sources.py 185-213): covered only in tests/test_spellings.py, not here.
5. browser.py launch/profile cleanup paths (43-105, 344-445, 671-735): real-Chrome code is untested in these files; no Linux-safe fake covers it.

## Suite-wide practice issues

- Cross-test-module imports: `tests.test_pipeline.FakePoster`, `tests.test_unattended.FakePoster` (two different classes with the same name), `tests.test_unattended.FakeDiscord`, `tests.test_globe_history.listing/row`, and tests/browser_lane_replay.py imports `FakeTabSocket` from tests.test_browser. Importing test modules runs their collection side effects and couples files. Move shared fakes to tests/fakes.py or conftest fixtures.
- Misfiled tests: test_sec_results holds DateDiscovery, TrackProgress, source_audit and site_local_time tests; test_browser_sources holds browser_host and verified_results_table tests; test_sources holds periods.agrees_with_period.
- Private-name coupling: `_SETTLED`, `_Tab`, `_start/_new_tab/_drop/_warm/_tabs/_prewarming/_locks` (test_browser), `_queues`, `_session`, `_browser` (test_fetch), `_check`, `_verify_sixks`, `_filing_type`, `_measurements`, `_archived_sources`. Refactoring internals will break these without a behavior change.
- Real time: wall-clock thresholds (`< 0.05`, `< 0.5`, `== 8`, `0.35 s` live polling windows, `ROUND=0.02` frontier) are flaky under load; prefer injected clocks, `asyncio.Event` gating, or `<=` invariants.
- Repeated stub closures (`started`, `wait_turn`, `drop`) in test_browser: make a `chrome` fixture. Local imports inside test bodies (`from earnings_bot.browser import ...`) should be top-level.
- Production data in tests: `known_companies()["LLY"/"GOOGL"/"NVDA"]` couples tests to companies.toml.
- `HttpRelease.chrome_fallback` defaults to `os.name == "nt"`; tests that care pass it explicitly (good), but any new test omitting it would behave differently on Linux CI.
- DeprecationWarning noise from discord/utils.py: filter in pyproject `filterwarnings` so real warnings stay visible.
- Several long tests assert many unrelated behaviors (schedule migration, Broadcom, fresh PR Newswire, Klarna); prefer one rule per test or parametrization.
- No `@pytest.mark` split for slow/loopback-network tests (the real-socket test in test_fetch).
