# Test audit A: /track research (IR site, newswire, release routes)

Baseline (measured): 234 tests passed (113 test functions, 10 files) in 7.25 s on origin/main. Only one test over 0.5 s.
Verdict totals by function (measured): KEEP 88, STRENGTHEN 16, MERGE 8, REWRITE 1, DELETE 0.
Mutation checks run: 24 distinct mutations; 21 caught, 3 survived (M12, M24, M26) plus M27 survived within my files (4 survivors total, see below).

Overall: this suite is good. Nearly every test drives a public path (investigate(), rendered_date, poll, probe_cache, to_target) with fakes and asserts concrete outputs. No test was a candidate for DELETE. The problems are (a) a few survivors, (b) private-method coupling, (c) cross-module test imports, (d) one platform skip, (e) one real 2 s sleep.

## tests/test_bounded_research.py

| test | verdict | reason |
|---|---|---|
| test_research_pauses_only_in_the_minutes_around_a_release | STRENGTHEN | Killed M2, M3. But one test runs 6 sequential scenarios with shared state; split into parametrized cases so a failure names the scenario. |
| test_research_permissions_follow_a_window_that_opens_after_the_task_started | KEEP | Asserts HostBusy and chrome_allowed transitions; public Fetcher API. |
| test_date_discovery_researches_other_companies_behind_the_polls | STRENGTHEN | Killed M5. Takes 2.02 s (only slow test): real BACKGROUND_SEC_GAP=2.0 sleep (fetch.py:30,495) inferred, not verified by profiling. Monkeypatches `dates.run_once` and `dates.changed.wait` (ScanFinished trick), asserts via a hand-built list; three phases in one test. |
| test_research_that_needs_chrome_waits_without_retrying_every_scan | STRENGTHEN | Killed M10. `pytest.skip("Desktop Chrome research only runs on Windows")` means it is skipped on Linux CI; sibling test monkeypatches `chrome_available` -> True, this one should too. Calls private `_chrome_waits`, `_investigate`; uses real time.monotonic with 500<wait<=600 window. |
| test_an_interrupted_investigation_does_not_save_a_partial_profile[chrome/http] | STRENGTHEN | `chrome` param killed M11. `http` param does NOT isolate its guard: M12 (remove the research_deferred check before save) survives, because `_chrome_waits` (Chrome patched available) catches the same case. Add a param with `chrome_available` False. Private `_investigate`. |
| test_a_window_whose_routes_use_chrome_is_recognized | KEEP | Trivial but valid. Remove the `assert asyncio  # keeps the import` hack and the unused import. |

## tests/test_cache_research.py

| test | verdict | reason |
|---|---|---|
| test_cache_comparison (10 params) | KEEP | Table-driven, asserts outcome flags and summary text and that bust URLs are unique. Killed M14 (3 params fail) and M13. |
| test_a_feed_that_rejects_query_strings_keeps_its_readable_url | MERGE | Same setup/act (`_prepare_cache`, private) as test_spellings planner test; make one parametrized test (outcome: canonical vs spelled). |
| test_dynamic_html_doesnt_hide_the_actual_cache_finding | KEEP | Good regression, both branches. |
| test_a_different_page_title_cannot_verify_a_query_bypass | KEEP | Killed M13. |

## tests/test_ir_research.py

| test | verdict | reason |
|---|---|---|
| test_inspector_requires_same_record_release_evidence | KEEP | Killed M1. |
| test_issuer_listing_keeps_same_origin_links | KEEP | Single case; could add a relative-vs-absolute case. |
| test_browser_headline_requires_the_scheduled_fiscal_period | KEEP | Positive and negative regex cases. |
| test_a_results_headline_that_announces_rather_than_reports_counts | KEEP | |
| test_a_live_chrome_source_is_verified_or_says_why_not (4 params) | STRENGTHEN | Contains conditional assertions (`if source:`, `(source is not None) == why.endswith(...)`); parametrize the expected source instead. |
| test_no_chrome_or_no_page_says_so | KEEP | |

## tests/test_ir_release_coverage.py

| test | verdict | reason |
|---|---|---|
| test_track_keeps_the_results_api_when_the_newsroom_omits_the_release (2 params) | STRENGTHEN | ~25 assertions, `if not legacy_results_alias:` branches inside the test, private `_issuer_routes`, `_cache_routes`. Split into: route plan, poll behavior, cache routes. |
| test_results_api_preserves_the_public_https_boundary | KEEP | |
| test_the_saved_on_release_is_caught_up_on_the_first_q4_poll | STRENGTHEN | Survived M24: the 12-hour early-release clause of `_caught_up` is never exercised, because the Q4 fixture's `published` is not ISO and falls through to the site-local-day branch. Add an ISO-timestamp case before window start. |
| test_a_new_publication_audit_replans_before_the_hourly_check | KEEP | Behavioral (reads==2, replanned window). Private `_issuer_date`/`_check` monkeypatch and ScanFinished trick. |
| test_failed_research_waits_an_hour_even_with_a_saved_audit | KEEP | Killed M6. |

## tests/test_nebius_ir.py

| test | verdict | reason |
|---|---|---|
| test_nebius_tracks_from_the_issuer_notice_and_verifies_the_previous_pdf | REWRITE | ~60 lines of setup, three unrelated phases (first run, re-run with http_first False, audit_sources). The second phase asserts http_first becomes True, but M26 (drop `existing_ir.http_first` from the keep-clause) survives, so the preservation path is unproven. Split into 3 tests; build the SEC submissions/exhibit via fakes helpers. |
| test_issuer_pdf_wrapper_alerts_and_attaches_without_using_boilerplate_as_results | KEEP | Asserts post sequence send/edit/attach, urgent fetch and journal events. Emits a discord DeprecationWarning (library, not test). |
| test_a_pdf_link_alone_does_not_publish_an_earnings_alert (2 params) | KEEP | |

## tests/test_issuer_news.py

| test | verdict | reason |
|---|---|---|
| test_track_reads_the_rendered_newsrooms_api_and_prepares_live_pdf | KEEP | End-to-end through run_once; monkeypatches private `investigate._last_releases`. |
| test_the_news_poll_requires_the_right_date_title_origin_and_pdf | KEEP | Killed M18, M19, M20 (day filter, no-redirect, PDF magic). |
| test_a_results_page_api_names_each_quarters_press_release | KEEP | |

## tests/test_spellings.py

| test | verdict | reason |
|---|---|---|
| test_spellings_dont_repeat_and_name_the_same_url | KEEP | Killed M9. Uses unseeded `random` (3000 draws); asserts only properties so deterministic in outcome. |
| test_a_short_path_can_be_spelled_anywhere | STRENGTHEN | Asserts the private-ish `.positions` attribute; assert that `next()` gives distinct decoding-equivalent URLs for a short path instead. |
| test_a_spelled_feed_goes_out_as_written_and_parses_like_the_plain_one | KEEP | Checks the bytes actually sent. |
| test_ir_sources_can_be_spelled | KEEP | |
| test_a_schedule_names_its_spelled_feeds_and_they_poll_spelled | KEEP | TOML round trip. |
| test_a_new_spelling_can_get_past_a_cache_that_ignores_queries (4 params) | KEEP | Killed M13 param. |
| test_the_planner_polls_a_feed_by_new_spellings_once_the_check_finds_they_work | KEEP | (see MERGE note in cache file; uses private `_prepare_cache` and a hand-rolled SpellingFetcher with `# type: ignore`) |

## tests/test_known.py

| test | verdict | reason |
|---|---|---|
| test_every_company_loads_and_uses_only_known_placeholders | KEEP | Data validation; `>= 40` magic number is a change detector. |
| test_templates_rebuild_the_hand_written_schedules | MERGE | Subsumed by test_schedule_entries_match_the_database (both compare to schedules/*.toml). |
| test_templates_match_urls_seen_on_company_sites (16 params) | KEEP | Pins real observed URLs; valuable regression guard. |
| test_a_release_date_gives_the_fiscal_period_it_reports | KEEP | |
| test_soft_404_sites_get_no_guesses | KEEP | Killed M15. |
| test_blocked_sites_and_other_wires_are_left_out_of_the_target | KEEP | |
| test_schedule_entries_match_the_database | STRENGTHEN | Coupled to historical hand-written schedules/*.toml and the live catalog; any catalog edit can break it for no behavior reason. Pin synthetic fixtures instead. |
| test_printed_entry_is_a_valid_schedule | KEEP | Round trip. |
| test_trial_page_templates_match_the_release_pages | KEEP | |
| test_broadcom_is_matched_on_pr_newswire_and_tried_on_its_newsroom | STRENGTHEN | Pins the whole ordered source-name list (overlaps Lilly assertion in test_blocked_sites...); assert membership/order of what matters. |
| test_how_a_company_names_its_fiscal_years_comes_from_its_results | KEEP | |
| test_a_planned_period_is_named_as_the_company_names_it | KEEP | |

## tests/test_ir_pages.py

| test | verdict | reason |
|---|---|---|
| test_dates_and_times_in_any_ir_format (5 params) | KEEP | |
| test_a_date_without_a_time_is_kept_as_a_day | MERGE | Fold into the moments parametrization above. |
| test_navigation_is_recognized_by_what_it_says (12 params) | KEEP | |
| test_the_period_an_event_names (8 params) | KEEP | |
| test_the_date_is_read_from_a_notice_found_through_the_sites_own_navigation | KEEP | Asserts which pages were opened, too. |
| test_without_a_notice_the_events_list_gives_the_call_and_so_the_release | KEEP | |
| test_an_earnings_call_places_the_release_by_the_companys_history (3 params) | KEEP | |
| test_the_reader_stays_within_its_page_budget | KEEP | Killed M17. |
| test_the_reader_keeps_off_translated_copies_of_the_site | KEEP | Killed M16. |
| test_a_page_chrome_cant_open_is_named_in_the_reading | KEEP | |
| test_ir_hosts_found_in_a_results_release (3 params) | MERGE | Same function (`ir_hosts`) as test_investigate's test_ir_hosts_are_the_investor_sites_a_release_names; one parametrized test in one file. Killed M28 though. |
| test_a_results_headline_can_name_its_quarter_like_2q2026 | STRENGTHEN | Tests `sources.looks_like_earnings`; wrong file. Move. |
| test_an_events_card_gives_the_date_wherever_it_puts_it | KEEP | |
| test_a_tentative_event_is_a_placeholder_not_a_date | KEEP | Killed M21. |
| test_a_call_only_notice_places_the_release_before_the_call | KEEP | |
| test_an_events_period_from_its_title_or_the_fiscal_year (2 params) | KEEP | Keysight row duplicates the title_period param; harmless. |
| test_glued_dates_still_show_which_notices_are_old | KEEP | |
| test_a_fiscal_year_end_from_the_latest_annual_report_when_sec_gives_none | STRENGTHEN | Tests `issuers.fiscal_year_end`; wrong file. Move to the issuers tests. |
| test_a_year_run_into_the_next_word_is_still_a_date | MERGE | Fold into moments params (unit form of the glued-dates case). |
| test_a_notice_dated_ahead_is_read_before_undated_ones | KEEP | |
| test_a_6k_filers_time_from_its_ir_site_is_trusted_before_its_call | KEEP | |
| test_an_event_naming_no_year_is_planned_as_the_company_names_its_quarters | KEEP | |
| test_a_headline_names_its_own_day_with_or_without_a_year (7 params) | KEEP | |
| test_a_notice_posted_today_isnt_an_event_today (2 params) | KEEP | |
| test_a_current_event_beats_a_neighboring_past_release | KEEP | |
| test_undated_news_notice_does_not_borrow_its_posting_day | KEEP | |
| test_news_link_text_does_not_make_results_an_event | KEEP | |

## tests/test_investigate.py

| test | verdict | reason |
|---|---|---|
| test_ir_hosts_are_the_investor_sites_a_release_names | MERGE | See ir_hosts note above. |
| test_the_wire_comes_from_the_dateline (5 params) | KEEP | Killed M4 (late-mention param). |
| test_feed_links_put_the_advertised_feed_first_and_stay_on_the_site | KEEP | |
| test_a_q4_site_is_polled_directly | STRENGTHEN | Good behavior, but pins 8 literal check-description strings; brittle to wording. Assert watch flags/sources, keep one or two strings. |
| test_blocked_q4_homepage_does_not_hide_the_issuer_notice | KEEP | |
| test_a_site_with_a_feed_is_polled_through_it | KEEP | |
| test_feeds_on_the_actual_ir_listing_are_checked (4 params) | KEEP | |
| test_a_site_that_blocks_the_bot_is_read_through_chrome | KEEP | Pins the exact Chrome call sequence (implementation-leaning but intended). |
| test_without_chrome_a_blocked_site_is_reported_and_both_wires_kept | KEEP | |
| test_a_release_naming_no_ir_site_still_gives_its_wire | KEEP | |
| test_an_issuer_named_rss_index_reaches_a_separate_newsroom | KEEP | |
| test_a_globenewswire_company_is_matched_there | KEEP | |
| test_results_filed_under_other_events_are_found_by_their_exhibits | KEEP | Calls private `_last_releases` (also monkeypatched in ~15 tests); acceptable but make it public. |
| test_the_last_release_is_read_from_the_results_8k | KEEP | |
| test_only_companies_outside_the_catalog_are_investigated_and_then_monthly | KEEP | Killed M8. |
| test_discovery_investigates_once_then_finds_the_date_on_the_new_feed | STRENGTHEN | Private `_check`; counts requests ending ".com/" as proxy for "not looked again". |
| test_findings_from_a_check_cut_short_by_a_window_arent_saved | STRENGTHEN | Killed M7, but private `_check` and rebinding `chrome.request`; use run_once. |
| test_every_source_checked_is_reported_with_why_as_it_goes | STRENGTHEN | Pins ~10 literal strings and step texts; keep the progress-count assertions, loosen strings. |
| test_chrome_tries_the_usual_list_when_the_home_page_doesnt_link_it | KEEP | |
| test_chrome_tries_another_listing_after_a_timeout | KEEP | |
| test_a_list_without_results_is_kept_for_notices_but_reported | KEEP | |
| test_a_site_without_a_q4_feed_or_rss_says_what_was_tried | KEEP | |
| test_a_release_naming_no_ir_site_says_so | MERGE | Same inputs class as the "still gives its wire" test; parametrize release text -> (wire, check). |
| test_discovery_shows_the_investigation_while_it_runs | KEEP | |
| test_without_chrome_an_open_site_keeps_the_list_a_plain_request_finds | KEEP | |
| test_a_menu_link_that_looks_like_results_isnt_taken_for_them | KEEP | |
| test_chrome_looks_past_a_date_notice_to_the_last_results_and_their_time | KEEP | |
| test_preliminary_results_dont_give_the_release_time | KEEP | Killed M25. |
| test_a_date_notice_alone_keeps_both_live_wires | KEEP | |
| test_only_verified_chrome_results_set_the_wire | KEEP | |
| test_amat_notice_in_chrome_saves_the_window_and_prepares_live_ir | KEEP | Private `_check`. |
| test_corporate_site_with_a_hosted_release_list_schedules_transdigm | KEEP | Killed M22. Large setup; candidate for a fixture file. |
| test_a_release_page_says_when_it_was_posted (6 params) | KEEP | |
| test_an_unreadable_notice_on_the_press_release_list_is_reported | KEEP | |
| test_a_6k_filers_wire_and_ir_site_come_from_any_copy_of_its_latest_results | KEEP | |
| test_a_live_release_teaches_the_bot_its_wire | KEEP | |
| test_an_ir_site_named_by_an_earlier_release_is_still_found | KEEP | |
| test_earlier_research_stands_when_no_release_names_an_ir_site | KEEP | |
| test_the_investigation_learns_how_the_company_names_its_fiscal_years (6 params) | KEEP | |
| test_what_earlier_research_learned_stands_when_the_releases_dont_say | KEEP | |

## Mutation checks

| test(s) | mutation | result |
|---|---|---|
| test_ir_research::inspector | matches_release: date-match `return True` -> False | caught |
| test_bounded::research_pauses | research_paused body -> False | caught |
| test_bounded::research_pauses | in_window -> False | caught |
| test_investigate::wire dateline | search whole text, not first 2000 chars | caught (late-mention) |
| test_bounded::date_discovery_researches | run() stops skipping companies in own window | caught |
| test_ir_release_coverage::failed_research_waits | drop `self.reviewed[...] = review` | caught |
| test_investigate::findings_cut_short | `_defer_if_busy` never defers | caught (1 test) |
| test_investigate::only_companies_outside_catalog | `due` ignores INVESTIGATION version | caught |
| test_spellings::dont_repeat | clear `_used` so spellings repeat | caught |
| test_bounded::chrome_waits | skip first `_chrome_waits` check in `_investigate` | caught |
| test_bounded::interrupted[chrome] | remove chrome check before save_profile | caught |
| test_bounded::interrupted[http] | remove `_defer_if_busy` check before save_profile (M12) | SURVIVED (redundant guard with chrome_available patched True) |
| test_cache_research / spellings | `_same_resource` title match always True | caught (3 tests) |
| test_cache_research | ignore busted cache hits | caught (3 params) |
| test_known::soft_404 | soft_404 no longer drops guesses | caught |
| test_ir_pages::translated | stop skipping /zh-hans links | caught |
| test_ir_pages::page_budget | reader ignores page limit | caught |
| test_issuer_news::poll | drop day filter | caught |
| test_issuer_news::poll | allow redirects on PDF fetch | caught |
| test_issuer_news::poll | drop `%PDF-` magic check | caught |
| test_ir_pages::tentative | ignore tentative marker | caught |
| test_investigate::transdigm | keep auth_token in hosted URL | caught |
| test_ir_release_coverage::caught_up | drop 12-hour early-release clause in `_caught_up` (M24) | SURVIVED |
| test_investigate::preliminary | preliminary headline still sets ir_posted | caught |
| test_nebius_ir | drop `existing_ir.http_first` from keep-clause (M26) | SURVIVED |
| bounded / ir_research / investigate | `research_chrome` ignores `fetcher.chrome_allowed()` (M27) | SURVIVED (all three files, 73 tests) |
| test_ir_pages::ir_hosts | ir_hosts doesn't strip www. | caught |

Note: an early attempt of M1-M4 using text-mode writes rewrote line endings; reverted with `git checkout -- src`, byte-mode used after. Final `git status` clean.

## Redundancy groups

1. ir_hosts: test_investigate::test_ir_hosts_are_the_investor_sites_a_release_names + test_ir_pages::test_ir_hosts_found_in_a_results_release.
2. moments parsing: test_dates_and_times_in_any_ir_format, test_a_date_without_a_time_is_kept_as_a_day, test_a_year_run_into_the_next_word_is_still_a_date.
3. _prepare_cache setup: test_cache_research::test_a_feed_that_rejects_query_strings... and test_spellings::test_the_planner_polls_a_feed_by_new_spellings...
4. "release names no IR site": two test_investigate tests.
5. test_templates_rebuild_the_hand_written_schedules vs test_schedule_entries_match_the_database.
6. ScanFinished + `monkeypatch(dates.changed.wait)` helper duplicated 3x (bounded, coverage x2): make a fixture.

## Slow tests

Only one over 0.5 s: test_bounded_research::test_date_discovery_researches_other_companies_behind_the_polls 2.02 s (measured). Cause (inferred from fetch.py:30,495, not profiled): real 2 s background SEC gap. Fix: patch `fetch.BACKGROUND_SEC_GAP` to 0 or inject a clock. Next slowest 0.03 s.

## Untested important behavior (coverage run, measured: investigate.py 90%, ir_pages.py 91%, cache_research.py 90%, known.py 87%, issuer_news.py 79%, ir_research.py 73%)

1. ir_research.verified_results_table (lines 197-221): QuarterlyTable verification over HTTP and rendered Chrome, including the wrong-year guard.
2. ir_research.verified_browser_source RSS-in-Chrome verification and the PDF-wrapper branch (lines ~275-329).
3. issuer_news: the `docs.pressRelease` manifest records (lines 79-91) and the manifest quarterly-earnings API discovery (168-183), plus the `manifest_verified` PDF-text check in IssuerNews.fetch (period/host/webcast verification).
4. date_discovery.calendar_date (FMP earnings calendar, lines ~404-435) and the PR Newswire notice route (~495-511) have no coverage from these files (may be in test_date_discovery).
5. recorder._caught_up early-release clause (survived M24); research_chrome vs chrome_allowed (survived M27).

## Suite-wide practice issues

- Cross-test-module imports: bounded imports `company` from test_ir_release_coverage and FakePoster from test_unattended; nebius imports BrowserFake from test_browser_sources and FakePoster from test_pipeline; investigate imports AMAT from test_date_discovery, filing_routes from test_pipeline; ir_pages imports AMAT from test_date_discovery. Importing a test module re-collects nothing but couples files and breaks single-file moves; move to tests/fakes.py or conftest fixtures.
- Heavy reliance on private methods (`_check`, `_investigate`, `_prepare_cache`, `_issuer_routes`, `_issuer_date`, `_chrome_waits`, `_cache_routes`, `investigate._last_releases`) in about 15 tests; prefer `run_once`/`run` or public wrappers.
- Platform skip hiding coverage: test_bounded::test_research_that_needs_chrome_waits... skips off Windows (monkeypatch chrome_available instead).
- Real clock: `time.monotonic()` in assertions (bounded x3, coverage); fakes.py uses time.monotonic for Clock-like anchor; unseeded `random` in Spellings tests. Inject a clock/seed.
- Large multi-phase tests with several scenarios in one function (research_pauses, nebius, date_discovery_researches, track_keeps_results_api).
- Conditional logic inside tests (`if source:`, `if not legacy_results_alias:`).
- `assert asyncio  # keeps the import` hack; `# type: ignore` fakes (SpellingFetcher) instead of a typed fake.
- Brittle pinned UI strings in investigate (check descriptions); wording changes break tests with no behavior change.
- 1360-line test_investigate.py: split by theme (wire/IR hosts, Chrome reading, discovery integration, fiscal naming); move large inline HTML fixtures to files like fixtures/ir_misses.json.
