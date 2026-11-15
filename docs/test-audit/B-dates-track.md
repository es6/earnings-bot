# Test audit B: dates, /track, scorecards, debug mode

Base: origin/main 0dc9d97 (v0.21.35). All numbers below are measured unless marked "judgment".
Files: test_date_discovery.py, test_track_progress.py, test_tracking_commands.py, test_calendar.py,
test_track_test.py, test_issuers.py (helpers fakes.py, sec_data.py read, not individually audited).

Measured totals: 121 test functions, 173 collected cases, all pass, 16.8 s wall.
Verdicts: KEEP 98, STRENGTHEN 18, MERGE 4, REWRITE 1, DELETE 0.
Coverage of the six source modules by these six files alone: calendar 86%, date_discovery 73%,
issuers 97%, track_progress 95%, track_test 83%, tracking_commands 93%.
Mutation checks: 22 run, 17 killed, 5 survived (2 of those survivors are equivalent or redundant code).
git status clean after every mutation (verified at the end).

Overall: this is a strong suite. Tests are named for behavior, use real SQLite stores and
fakes at the network edge, and most assert exact user-visible text or state. The weaknesses are
(a) heavy use of private methods in date_discovery tests, (b) real-time sleeps and wall-clock
thresholds in the Discord pacing tests and the two end-to-end alert tests, (c) a few misplaced
tests, and (d) the survivors listed below.

## test_calendar.py (9): KEEP 8, STRENGTHEN 1

| test | verdict | reason |
|---|---|---|
| test_subscription_and_override_survive_restart_and_preserve_file_trials | KEEP | Real restart through SQLite; killed M3 (manual override branch of plan). |
| test_tracking_and_untracking_can_be_repeated | STRENGTHEN | Only asserts add/remove return values six times; no check the table is empty or that a review row is cleared. Fold into the first test or assert `entries() == []` and `review(cik)` cleared. |
| test_override_requires_a_valid_fiscal_period_and_future_time | KEEP | Both refusal paths; first regex `fiscal\|period` is loose (matches any message mentioning either). Parametrize into two ids. |
| test_auto_browser_source_preserves_existing_schedule_choices | KEEP | Checks merge of IR source and trial flags across plan(). |
| test_saved_automatic_browser_headline_is_repaired_after_restart | KEEP | Restart plus repair of a saved headline. |
| test_a_calendar_saved_before_profiles_is_upgraded_in_place | KEEP | Real old-schema migration. |
| test_a_company_the_catalog_dropped_is_still_watched_on_edgar | KEEP | Asserts resulting source list. |
| test_the_calendar_follows_the_bot_to_a_new_channel | KEEP | Killed M18 (follow the largest channel). Tests "once" and the untouched small channel. |
| test_legacy_schedule_keeps_its_key_and_watches_the_researched_table | KEEP | Asserts key stability and a single QuarterlyTable with the right flags. |

## test_track_test.py (9): KEEP 7, STRENGTHEN 2

| test | verdict | reason |
|---|---|---|
| test_a_run_researches_a_catalog_company_as_new_and_leaves_nothing_behind | KEEP | Verifies SEC is asked, catalog entry not used, scratch folder removed, live folder untouched. Strong. |
| test_a_ticker_that_cant_be_identified_reports_why | KEEP | Error text plus all-missing checklist. |
| test_the_checklist_marks_a_date_only_an_outside_calendar_gave_as_unsure | STRENGTHEN | Calls private `track_test._facts` to build facts; reach it through `run_track_test` with a fake research that reports "FMP's earnings calendar" evidence. |
| test_an_answer_key_lists_every_fact_that_differs | KEEP | Case, substring and set-membership rules each exercised. |
| test_saved_reports_keep_the_last_twenty_per_symbol | KEEP | Killed M15 (KEEP 20 to 21). |
| test_the_owner_is_whoever_owns_the_bots_application_or_its_team | KEEP | Tests `tracking_commands.is_owner`, not debug mode: belongs in test_tracking_commands.py. |
| test_research_investigates_a_catalog_company_only_in_a_test_calendar | STRENGTHEN | Calls private `dates._investigate`, imports module inside the test, monkeypatches `investigate`. Public path is `run_once`. |
| test_a_run_past_its_limit_stops_and_reports_what_it_found | KEEP | Real 0.2 s limit (0.21 s measured). Note the pinned message says "0 s limit" for limit=0.2: asserts a display quirk, not a rule. |
| test_a_network_timeout_is_reported_as_itself | KEEP | Error text includes exception class. |

## test_issuers.py (12): KEEP 11, STRENGTHEN 1

| test | verdict | reason |
|---|---|---|
| test_symbols_are_spelled_as_sec_lists_them | KEEP | Parametrized. |
| test_wire_names_drop_the_legal_ending_and_add_the_exchange_tag | KEEP | Parametrized, includes one-word and dual-class rules. |
| test_the_names_find_the_companys_release_and_not_a_lookalike | KEEP | Drives a real RssFeed parse end to end. |
| test_release_time_comes_from_when_results_8ks_were_accepted | KEEP | Parametrized with ids and reasons. |
| test_the_usual_time_is_the_earliest_recent_release_in_their_session | KEEP | Killed M17 (USUAL_FEWEST 2 to 1). |
| test_a_profile_watches_edgar_and_all_three_wires | KEEP | Asserts exact source list. |
| test_an_unusable_fiscal_year_end_is_left_out | KEEP | Parametrized. |
| test_a_catalog_ticker_needs_no_lookup | KEEP | Asserts zero requests. |
| test_other_tickers_come_from_sec_and_the_list_is_kept_for_a_day | KEEP | Four concerns in one test (cache, refusal, refresh patch); fine but a failure will not localize. Optional split. |
| test_a_failed_lookup_asks_again_next_time | KEEP | 2 x 3 parametrized; recovery asserted. |
| test_something_that_isnt_a_symbol_isnt_looked_up | KEEP | Asserts no request. |
| test_single_word_publisher_matches_azentas_own_results | STRENGTHEN | Name is misleading (it tests Calendar.plan restoring `wire_publisher` on old rows); the final `for source in planned.sources(): if source.name in (...)` loop asserts nothing if no such source exists, so it can pass vacuously. Assert the matching sources exist first. Belongs in test_calendar. |

## test_tracking_commands.py (21): KEEP 17, STRENGTHEN 4

| test | verdict | reason |
|---|---|---|
| test_track_adds_a_catalog_ticker_once_and_wakes_the_calendar | KEEP | Reply text, wake events, next-window selection. |
| test_any_ticker_sec_lists_is_tracked_with_what_sec_says_about_it | KEEP | Orders defer before SEC request; restart equality. |
| test_a_ticker_sec_doesnt_list_is_refused | KEEP | Parametrized. |
| test_when_sec_doesnt_answer_nothing_is_saved_and_a_retry_works | KEEP | Failure then recovery. |
| test_a_release_time_override_needs_manage_server_and_both_fields | KEEP | 8 ids; killed M14 (permission check removed). |
| test_an_admin_override_pins_the_release_and_can_move_it | KEEP | State and reply. |
| test_an_admin_can_pin_a_ticker_outside_the_catalog | KEEP | Source list. |
| test_one_member_can_track_six_tickers_in_one_day | STRENGTHEN | Name promises a per-member daily limit but the test lowers MAX_TRACKED to 6 and checks a global cap. Rename (e.g. "the calendar refuses a company past its cap"); 6 slow-ish real `track` calls are not needed to prove it. Killed M2. |
| test_commands_from_another_server_or_with_the_watcher_off_answer_offline | KEEP | All three commands, both states. |
| test_untrack_needs_manage_server_and_tells_the_scorecard | KEEP | Killed M21. |
| test_only_managers_see_untrack_and_every_command_is_server_only | KEEP | Overlaps test_directory_bot_registers_tracking_commands (see Redundancy). |
| test_upcoming_lists_the_weeks_windows_and_issuers_awaiting_dates | KEEP | Whole reply compared as lines. |
| test_a_long_upcoming_list_stops_at_a_whole_line | STRENGTHEN | Last assertion is `shown + more == len(targets) or "didn't fit" in footer`; the disjunction passes if the count is wrong whenever anything was dropped. Assert the exact dropped count equals the footer's number. |
| test_upcoming_shows_each_scorecards_readiness | KEEP | Sets `progress.audit_failed` directly (internal state); acceptable but couples to the attribute. |
| test_a_tracked_issuer_with_no_schedule_file_gets_one_alert | STRENGTHEN | Highest-value end-to-end test, but 3.0 s measured and timing-based: real `time.monotonic` release 0.8 s after a poll, real clock `sync()`; can flake on a loaded CI box. Drive a fake monotonic/clock. |
| test_a_ticker_outside_the_catalog_gets_its_alert_from_the_wire | STRENGTHEN | 6.0 s measured (slowest test); same real-time design, near-copy of the one above (ShiftedClock/`released_at`/restart scaffolding). Share a fixture and use a controllable clock. |
| test_watch_status_shows_each_releases_time_beside_when_its_watch_opens | KEEP | Tests `health_embed` (status), not /track: move to the status/unattended tests. |
| test_a_route_that_went_quiet_marks_the_window_report_degraded | KEEP | Uses private `unattended._window_report`; misplaced here. |
| test_status_names_a_degraded_route_while_its_window_is_open | KEEP | Misplaced here (status). |
| test_the_owner_can_test_in_any_server_and_managers_only_in_the_bots_own | KEEP | Permissions plus saved report plus no calendar change. |
| test_a_test_waits_for_a_release_due_soon_and_runs_one_at_a_time | KEEP | Lock behaviour asserted by a 1 s `wait_for`. |

## test_track_progress.py (31): KEEP 20, STRENGTHEN 7, MERGE 4

| test | verdict | reason |
|---|---|---|
| test_directory_bot_registers_tracking_commands | MERGE | Same registration check as test_only_managers_see_untrack...; one registration test should cover names, permissions, guild_only. |
| test_watch_status_shows_build_separately_from_the_version | STRENGTHEN | Unrelated to scorecards; calls private `bot._status` with a Mock interaction. Move to a status test. |
| test_a_card_resumes_after_discord_accepted_its_send | KEEP | Lost-response recovery, no duplicate send. |
| test_a_card_is_edited_in_place_and_sent_again_if_deleted | KEEP | |
| test_an_unchanged_card_isnt_edited_again | STRENGTHEN | Replaces `FakeMessage.edit` by hand in try/finally instead of `monkeypatch`; leaks if the assignment itself fails. Also passes with publish()'s in-memory check removed (M5), because CardPoster already skips unchanged content: the test cannot tell which layer does it. |
| test_a_restart_leaves_a_card_alone_if_it_still_reads_the_same | KEEP | |
| test_card_edits_are_spaced_to_leave_room_for_release_edits | STRENGTHEN | Real 0.2 s spacing, asserts `>= 0.35` and `< 0.1` wall-clock; 0.42 s measured. Killed M8, but inject a clock/sleep. |
| test_the_audit_waits_for_the_investigation | KEEP | |
| test_the_card_follows_an_investigation_as_it_goes | KEEP | |
| test_a_finished_card_says_what_was_fastest_watched_and_rejected | KEEP | Exact text of every field. |
| test_an_unverified_ir_page_says_why_it_isnt_watched | KEEP | |
| test_without_a_date_the_card_says_what_it_would_watch | KEEP | |
| test_a_scheduled_card_waits_for_ir_verification_to_finish | KEEP | |
| test_the_card_shows_cache_preflight_before_research_completes | KEEP | |
| test_a_complete_card_is_green_only_when_its_routes_are_verified_fresh | KEEP | |
| test_a_check_saved_in_the_old_format_is_redone | MERGE | Same "stale saved review is redone" rule as the next test; parametrize the saved payload. |
| test_a_check_saved_without_a_release_history_is_redone | MERGE | Ditto; also has an arbitrary `asyncio.sleep(0.05)`. |
| test_a_removed_ticker_gets_a_closing_card | STRENGTHEN | Polls with up to 50 x 0.02 s sleeps and does not fail on the loop ending (the later assertion catches it, but with a poor message). Await an event. |
| test_a_page_rejected_at_first_but_verified_later_isnt_listed_as_rejected | KEEP | |
| test_a_lone_timing_isnt_called_the_fastest | KEEP | |
| test_a_minute_resolution_timestamp_cannot_win_by_seconds | KEEP | |
| test_a_moved_calendars_cards_are_edited_not_posted_again | KEEP | |
| test_a_new_version_alone_doesnt_edit_a_card | MERGE | Strict subset of test_a_restart_edits_no_card_whose_content_is_unchanged (which also varies the version via monkeypatch). |
| test_a_restart_edits_no_card_whose_content_is_unchanged | KEEP | Regression for the Nov 12 incident; keep. |
| test_cards_wait_out_an_open_window_so_an_alert_edit_isnt_queued_behind_them | STRENGTHEN | Killed M7 but relies on `sleep(0.2)` and `took < 0.25` wall-clock. |
| test_a_release_edit_cancels_a_card_edit_already_waiting_on_discord | KEEP | Good use of a stalled edit; the only slow-ish piece is a never-completing 30 s sleep that is cancelled. |
| test_old_cards_show_settled_results_one_at_a_time | STRENGTHEN | Real `old_card_spacing=0.3` plus `sleep(0.35)`; 0.37 s. Inject the clock (`time.monotonic` is read directly in track_progress). |
| test_a_fresh_card_shows_research_as_it_goes | KEEP | |
| test_an_old_card_discord_keeps_refusing_is_posted_again | KEEP | Killed M6. |
| test_the_audit_runs_in_the_background_during_a_window_but_not_its_own | KEEP | Negative assertion after `sleep(0.3)` (0.31 s) is inherently a wait; docstring "Item 9" is a stale reference. |
| test_a_deferred_audit_preserves_the_previous_review | STRENGTHEN | Calls private `progress._check` and asserts internals (`next_attempt`, `audit_failed`). Go through `run()` and assert the observable card colour / review. |

## test_date_discovery.py (39): KEEP 35, STRENGTHEN 3, REWRITE 1

Pervasive: 14 tests call the private `DateDiscovery._check` instead of the public `run_once`/`run`;
the autouse `no_browser` fixture stubs `_listing_date` so that function is never exercised.

| test | verdict | reason |
|---|---|---|
| test_notice_prefers_report_time_over_call_and_preserves_fiscal_year | KEEP | |
| test_notice_arms_wider_window_after_close_and_leaves_call_only_pending | KEEP | |
| test_an_rss_notice_sets_the_date_and_off_site_links_arent_followed | KEEP | Asserts requests made. |
| test_a_quarterly_webcast_notice_can_give_an_earlier_release_date | KEEP | Two dates parametrized. |
| test_discovery_waits_out_an_open_window_then_saves_the_issuers_date | KEEP | Public `run()`; real asyncio events, no sleeps. Good model. |
| test_a_date_found_as_a_window_opens_or_after_untrack_isnt_saved | STRENGTHEN | "ticker removed" case survives deleting the `row not in self.calendar.entries()` guard (M13) because the later `calendar.find(...) != row` guard duplicates it, so one guard is redundant code; test uses `_check`. |
| test_discovery_leaves_an_admin_override_alone | KEEP | |
| test_a_verified_browser_source_joins_the_found_date_and_is_kept | KEEP | |
| test_fmps_date_at_the_8k_time_sets_a_wide_window | KEEP | Apikey in header not URL asserted. |
| test_fmp_is_asked_twice_a_day_until_its_window_passes | KEEP | Killed M19. Name says "twice a day" but nothing pins the refresh interval. |
| test_a_failed_fmp_answer_saves_nothing_and_is_asked_again | KEEP | |
| test_a_symbol_fmps_plan_doesnt_cover_waits_for_the_next_refresh | KEEP | |
| test_an_issuers_date_beats_fmp_and_fmp_needs_a_key | KEEP | Killed M11. |
| test_fmp_isnt_asked_without_a_time_or_fiscal_year_to_go_with_its_date | KEEP | |
| test_catalog_companies_have_no_fiscal_year_end_so_fmp_leaves_them_alone | KEEP | Data invariant guard; belongs with the catalog tests. |
| test_a_headline_with_the_date_is_read_even_when_the_notice_is_about_the_call | KEEP | |
| test_notice_headlines_name_their_quarter_in_several_ways | KEEP | |
| test_a_reset_isnt_a_date_notice | KEEP | Tests `is_date_notice`; belongs with test_sources. |
| test_a_notice_whose_date_cant_be_read_is_named_on_the_card | KEEP | Pins a 200-character message verbatim: brittle to copy edits, but it is the user-facing text. |
| test_a_time_beside_another_days_date_isnt_the_release_time | KEEP | |
| test_a_pages_posted_at_line_isnt_read_with_the_headline_above_it | KEEP | |
| test_a_call_date_cannot_fill_in_a_missing_or_past_release_date | KEEP | |
| test_dateline_and_html_line_breaks_preserve_a_confirmed_release_time | KEEP | |
| test_a_morning_call_moves_an_afternoon_estimate_before_it | KEEP | Killed M9. |
| test_before_930_am_is_before_the_open_whatever_the_filing_times | KEEP | |
| test_a_window_opens_by_when_the_ir_site_had_the_last_release_not_sec | KEEP | |
| test_a_window_is_placed_by_recent_quarterly_releases_not_a_preliminary_one | KEEP | |
| test_a_window_covers_each_recent_release_time | KEEP | |
| test_a_tracked_window_is_widened_by_the_saved_release_history | KEEP | |
| test_the_first_plan_uses_the_verified_article_time_before_the_audit | KEEP | |
| test_a_closed_window_needs_evidence_of_a_release | KEEP | Parametrized delivered/not. |
| test_track_arms_the_rendered_results_page_alongside_the_issuer_api | KEEP | Parametrized first-probe failure. |
| test_a_wire_notice_can_supply_the_date_when_ir_cannot | KEEP | Includes the lookalike negative. |
| test_event_cards_read_adjacent_dates_and_zones_without_arming_tentative_events | STRENGTHEN | Branches inside the test (`if time is None`, `if day == 25`); the tentative case only asserts `result is None`, so it cannot tell "tentative skipped" from "no date parsed". Split into two tests; for the tentative one assert the date was seen but not armed. |
| test_a_morning_results_event_keeps_its_issuer_date_when_history_is_afternoon | KEEP | |
| test_new_watch_is_saved_after_issuer_and_cache_preparation | REWRITE | Monkeypatches `_issuer_date`, `_issuer_routes` and `_prepare_cache` on the object under test; the final assertion mostly proves the fake's return value flowed through. Only the ordering asserts inside the fakes carry value. Rewrite with the fetcher fake and assert the saved row. |
| test_a_notice_naming_no_year_gets_the_quarter_as_the_company_names_it | KEEP | |
| test_fmps_date_gets_the_quarter_as_the_company_names_it | KEEP | Same rule via FMP path. |
| test_a_company_is_settled_once_its_check_finishes_outside_a_window | STRENGTHEN | Stubs `_check` and `_prepare_cache`, so it pins the `looked` bookkeeping of `run_once` only; killed M20 but a refactor of the internals breaks it. Use a real check with the fake fetcher. |

## Mutation checks (22 run)

| test(s) | mutation | result |
|---|---|---|
| test_one_member_can_track_six_tickers | calendar.add: drop `len(entries) >= MAX_TRACKED` check | killed |
| test_subscription_and_override_survive_restart... | calendar.plan: ignore `Manual override` branch | killed |
| all six files | calendar.plan: drop the 6-K `sec_form` copy to an existing schedule file | SURVIVED (173 pass) |
| test_an_unchanged_card_isnt_edited_again (and spacing test) | publish(): remove `last_sent == shown` early return | SURVIVED (CardPoster also dedupes; layer redundant, low value) |
| test_an_old_card_discord_keeps_refusing... | publish(): never replace a refused old card | killed |
| test_cards_wait_out_an_open_window... | flush(): do not skip edits while busy | killed |
| test_card_edits_are_spaced... | _pace(): never sleep | killed |
| test_a_morning_call_moves_an_afternoon_estimate_before_it | drop call-deadline adjustment | killed |
| all six files | event_target: drop `(sec_form != "6-K" or release_published)` condition | SURVIVED |
| test_an_issuers_date_beats_fmp... | keep saved issuer date branch disabled | killed |
| all six files | final `elif saved:` (keep a remembered FMP date when FMP gives nothing now) disabled | SURVIVED |
| test_a_date_found_as_a_window_opens_or_after_untrack | remove first `row not in entries()` guard | SURVIVED (equivalent: second guard `calendar.find(...) != row` does the same) |
| test_a_release_time_override_needs_manage_server | untrack: drop Manage Server check | killed |
| test_saved_reports_keep_the_last_twenty | KEEP = 21 | killed |
| test_the_usual_time_is_the_earliest... | USUAL_FEWEST 2 to 1 | killed |
| test_the_calendar_follows_the_bot_to_a_new_channel | follow the smallest channel instead of largest | killed |
| test_fmp_is_asked_twice_a_day... | ignore remembered FMP answer | killed |
| test_a_company_is_settled_once_its_check_finishes | drop in-window check before marking looked | killed |
| test_untrack_needs_manage_server_and_tells_the_scorecard | stop recording `progress.removed` | killed |
| all six files | _issuer_date: skip 6-K exhibit notices | SURVIVED |
| (one attempted mutation, `if self.fmp_key`, did not match the source and was not run) | | not counted |

Survivors that are real gaps: 6-K plan merge (M4), 6-K call-guide condition (M10), keep remembered FMP
date after later FMP failure (M12), 6-K issuer exhibit notices (M22; test_sec_results.py may cover
it from another angle, not checked). M5 and M13 are redundant code paths, not test defects.

## Redundancy groups

1. Registration: test_directory_bot_registers_tracking_commands (track_progress) and
   test_only_managers_see_untrack_and_every_command_is_server_only (tracking_commands): one test.
2. Stale review redone: test_a_check_saved_in_the_old_format_is_redone and
   test_a_check_saved_without_a_release_history_is_redone: parametrize.
3. Version-only footer: test_a_new_version_alone_doesnt_edit_a_card is a subset of
   test_a_restart_edits_no_card_whose_content_is_unchanged.
4. End-to-end alert scaffolding: the PLTR and LCID tests in test_tracking_commands share ShiftedClock,
   DirectoryWatcher build, `released_at`, window skip; extract a fixture.
5. Quarter naming by fiscal_year_offset appears twice (parse_notice and FMP) with identical params;
   fine as two paths, but could share one parametrize table.

## Slow tests (measured)

- test_a_ticker_outside_the_catalog_gets_its_alert_from_the_wire: 6.02 s. Real-time polling (2 s
  PR Newswire cadence, results 1 s after start) in a real clock.
- test_a_tracked_issuer_with_no_schedule_file_gets_one_alert: 3.02 s. Same design, 0.8 s release delay.
- test_card_edits_are_spaced...: 0.42 s; test_old_cards_show_settled...: 0.37 s;
  test_the_audit_runs_in_the_background...: 0.31 s; test_cards_wait_out_an_open_window...: 0.21 s;
  test_a_run_past_its_limit...: 0.21 s. All are deliberate real sleeps against time.monotonic.
Everything else under 0.1 s.

## Untested important behavior

1. `date_discovery._listing_date` (direct fetch of an IR listing, then the Chrome fallback; source lines
   441-476): stubbed to None by the autouse fixture and never exercised in these files.
2. `event_target` 6-K branch: a call-only notice for a 6-K filer whose release time is not yet published
   should place the window from the call (survived M10).
3. A remembered FMP-calendar date is kept when a later FMP check fails or returns nothing (survived M12).
4. `Calendar.plan` propagating `sec_form="6-K"` to an existing schedule file (survived M4).
5. `track_test._main` CLI (lines 352-381: answer-key one-shot count, `--out`, per-symbol printing).
   Also 6-K issuer exhibit notices in `_issuer_date` (M22), and the uncovered chunk at date_discovery
   653-697 (event-page fallback when a listing gives no dated cards).

## Suite-wide practice issues

- Cross-module test imports: test_track_progress, test_tracking_commands and test_track_test import
  fixtures/classes from other test modules (`tests.test_pipeline.uber`, `FakePoster`,
  `tests.test_unattended.FakeDiscord/FakeMessage`, and `tests.test_tracking_commands.NOW/client/OWNER`).
  Importing a test module re-collects nothing but couples files and makes moving tests hard. Move to
  tests/fakes.py or conftest.py.
- Private API in tests: ~14 uses of `DateDiscovery._check`, plus `_investigate`, `_issuer_date`,
  `_issuer_routes`, `_prepare_cache`, `_facts`, `_status`, `_window_report`, `TrackProgress._check`.
- Real time: `asyncio.sleep`/`time.monotonic` thresholds in track_progress tests and the two
  end-to-end alerts (ShiftedClock.sync uses the real clock). Flake risk on a loaded CI runner. Inject a
  clock (track_progress reads `time.monotonic` directly; add a `monotonic` parameter like `now`).
- Manual class patching (`FakeMessage.edit = counted` in try/finally) where `monkeypatch` exists;
  inline imports inside tests (`from earnings_bot import date_discovery`, `import contextvars`).
- Misplaced tests: status/health_embed, `_window_report`, `is_owner`, `is_date_notice`, the catalog
  fiscal-year data invariant, and the wire-publisher plan test live in files named for other features.
- Tests with branches in the body (`if time is None` / `if day == 25`; loops with `if source.name in`).
- Docstring/comment staleness: "Item 9" reference in test_track_progress, "HQ Nest's #market-watch"
  channel IDs in test_calendar are history, fine but opaque.
- Platform: no skip markers in these files; Chrome is stubbed everywhere, so the suite runs on Linux CI
  (one test monkeypatches `chrome_available` to True for this reason).
- DeprecationWarning from discord.utils (count positional) appears in 5 tests; third-party, ignorable.
