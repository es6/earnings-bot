# Test audit C: live release path

Scope: tests/test_pipeline.py, test_recorder.py, test_unattended.py, test_release_cards.py, test_release_timeout_replay.py, test_burst_replay.py and the helper tests/burst_replay.py. Base: origin/main at 0dc9d97. All numbers are measured on this Windows machine unless marked otherwise.

Baseline: 107 test functions, 115 collected items, all pass, 27.7 s (measured). Six concurrent runs of the pipeline, recorder, timeout-replay and burst files: 6 of 6 passed, ~20.5 s each (measured), so no flake showed up under moderate load.

Verdict totals (measured by hand over 107 functions): KEEP 85, STRENGTHEN 13, REWRITE 3, MERGE 5, DELETE 1.

## tests/test_pipeline.py (38)

| Test | Verdict | Reason |
|---|---|---|
| test_newswires_are_named_in_the_alert | KEEP | Pure mapping check; could be a parametrize but fine. |
| test_release_posts_links_files_and_summary | KEEP | Golden path: ordered post, links, attach, summary, journal steps. Asserts `live_seconds < 5` from the real clock (see Flakiness). |
| test_both_sec_sources_give_the_filing_one_link | KEEP | Mutation (_same_release_link removed) killed it. |
| test_both_sec_sources_download_the_filings_exhibit_once [2] | KEEP | Asserts exact urgent request list; strong. |
| test_the_other_sec_source_tries_again_when_the_first_read_gets_nothing | KEEP | Exact request sequence after a 404 detail page. |
| test_only_one_summary_when_several_sources_have_the_text | KEEP | One model call, one `body` event. |
| test_a_newly_added_source_posts_like_any_other | DELETE | Pins the absence of "trial gating" that no longer exists in pipeline.py (no `trial` branch). Nothing to remove, so no mutation can fail it. Its only unique content (Broadcom label in the alert) duplicates describe_source and the golden path. |
| test_the_same_release_from_both_copies_of_a_feed_posts_and_downloads_once | KEEP | One send, one GET. |
| test_an_alert_for_a_release_already_out_says_so | KEEP | Checks the visible label. |
| test_a_file_listed_before_its_upload_is_attached_once_it_goes_up | STRENGTHEN | `expected=datetime.now(ET)` ties it to the wall clock; use the journal clock or PinnedClock. |
| test_a_missing_file_isnt_retried_after_the_window_closes | STRENGTHEN | Passes only because the real date is after Nov 4 2026 (uber() window). Pin the journal clock instead; it would have failed before that date. |
| test_browser_pdf_attaches_already_verified_bytes_without_another_get | KEEP | Asserts zero raw requests. |
| test_keep_warm_pings_discord_only_while_a_window_is_open | KEEP | Mutation (always ping) killed it. Real sleeps; see Flakiness. |
| test_the_summary_starts_as_soon_as_the_text_arrives | REWRITE | Guards a lookup that no longer exists in the code (FMP calls). It passes with or without SlowCalendar, so it is a tombstone with a 1 s sleep. Replace with a deterministic check that the writer is invoked before any other awaited work, or delete. |
| test_a_file_too_big_for_discord_stays_a_link | KEEP | Mutation (size check dropped) killed it. |
| test_a_failed_summary_is_tried_again_from_the_next_source | KEEP | Two calls, one posted summary. |
| test_an_alert_with_too_many_links_is_cut_to_discords_limit | KEEP | Killed by removing the cut in `_content` (line ~619). |
| test_an_alert_names_the_quarter_and_masks_each_link | KEEP | Asserts exact lines including markdown escaping. |
| test_a_release_pdf_is_summarized_when_no_page_has_the_text | KEEP | Real PDF bytes through the extractor. |
| test_a_pdf_is_summarized_at_once_and_a_page_replaces_it_once | KEEP | Strong, long (50 lines); candidate to split replace-once from stored-document checks. |
| test_a_page_that_comes_during_the_pdf_summary_replaces_it_after | STRENGTHEN | Orders events with `sleep(0.03)` against a 0.1 s writer. Use an asyncio.Event inside the writer so the page arrives deterministically mid-write. |
| test_a_pdf_never_replaces_a_page_based_summary | KEEP | Not mutation-checked. |
| test_a_deck_or_unreadable_pdf_is_not_summarized | KEEP | No model call. |
| test_a_wire_alert_without_text_reads_the_release_from_the_issuers_newsroom | REWRITE | Redefines its own Newsroom fake, target and constants that the NBIS_*/NebiusNewsroom helpers just below already provide. Rebuild on the shared helpers. |
| test_the_newsroom_read_waits_out_a_refusal | KEEP | Exact `retry_in`. Uses `>= 0.3` against BLOCK_PAUSE 0.3 (see Flakiness). |
| test_the_newsroom_read_waits_while_the_newsrooms_watcher_is_refused | KEEP | Cross-route pause; `>= 0.29`. |
| test_the_newsroom_watcher_waits_while_a_read_of_the_newsroom_is_refused | KEEP | Mirror of the above. |
| test_a_restart_doesnt_summarize_a_delivered_release_again | KEEP | Mutation (skip check removed) killed it. |
| test_a_summary_written_before_a_crash_is_posted_with_no_new_call | KEEP | Zero model calls, summary posted. |
| test_a_release_gets_at_most_three_model_calls_across_restarts | KEEP | Mutation killed it. |
| test_summary_calls_stop_at_the_days_cap | KEEP | Mutation killed it. |
| test_at_most_two_model_calls_run_at_once | KEEP | Mutation (limit 2 to 20) killed it. |
| test_release_writer_reads_prepared_context_without_blocking_detection | KEEP | Weak name; assigns an attribute lambda, but verifies the context reaches the writer. |
| test_a_file_link_reads_without_url_escapes | KEEP | Exact label. |
| test_a_link_and_summary_discord_refuses_still_reach_the_alert | KEEP | Covers redelivery through a store. |
| test_a_refused_first_post_is_sent_again_and_the_summary_follows_it | KEEP | Order of send versus summary. |
| test_redelivery_stops_after_its_last_wait_and_says_so | KEEP | Exact refusal count of 4. |
| test_a_new_item_whose_headline_isnt_earnings_posts_if_its_text_announces_the_results | STRENGTHEN | Uses `await asyncio.sleep(0.05)` to let background headline checks run. Await a completion signal or `finish()` instead. |

## tests/test_recorder.py (33)

Five tests here exercise `Fetcher` itself (real loopback servers) and belong in tests/test_fetch.py, which already covers the pacing: test_fetcher_gives_up_on_a_server_that_never_answers, test_fetcher_reads_a_header_longer_than_aiohttps_default, test_fetcher_doesnt_send_cookies_back, test_an_urgent_request_takes_the_hosts_next_turn_but_keeps_its_gap, test_fetcher_reports_how_long_a_request_waited_for_its_host. All KEEP (move).

| Test | Verdict | Reason |
|---|---|---|
| test_plan_times_default_to_eastern | KEEP | Real TOML to Target, exact windows. |
| test_watch_logs_items_that_appear_after_the_first_poll | KEEP | Checks first-poll baseline. |
| test_watch_keeps_polling_after_a_timeout | KEEP | Timeout counted apart from errors. |
| test_fetcher_gives_up_on_a_server_that_never_answers | KEEP (move) | Real socket; `< 1` s for a 0.2 s deadline. |
| test_fetcher_reads_a_header_longer_than_aiohttps_default | KEEP (move) | Mutation (header limit removed) killed it. |
| test_fetcher_doesnt_send_cookies_back | KEEP (move) | Mutation (DummyCookieJar removed) killed it. Takes 1.01 s here, probably localhost resolving to ::1 first on Windows; use 127.0.0.1 plus a Host header, or accept. |
| test_a_refused_poll_is_retried_after_a_growing_pause | KEEP | Exact backoff list [0.05, 0.1]. |
| test_clock_sync_is_logged | KEEP | Exact event fields. |
| test_failed_clock_sync [2] | KEEP | Parametrized correctly. |
| test_windows_opening_together_share_a_clock_sync | KEEP | One ask. |
| test_watch_stops_polling_a_guess_once_found | KEEP | polls == 2. |
| test_extra_sources_come_from_the_targets_trial_table | KEEP | Source list and cache-buster flags. |
| test_a_late_start_catches_up_on_releases_published_inside_the_window | KEEP | Mutation (_caught_up dropped) killed it. Uses PinnedClock. |
| test_a_release_out_before_the_window_opened_is_already_out | KEEP | Same mutation killed it; distinct input class. |
| test_a_guess_already_there_at_the_first_poll_isnt_caught_up | KEEP | Guess-URL exception. |
| test_an_item_listed_twice_is_new_once | KEEP | Later-poll dedupe. |
| test_new_items_log_how_long_the_poll_waited_for_its_host | KEEP | Name undersells it (also checks the summary rows). |
| test_a_poll_that_downloads_the_release_logs_its_text_arriving | KEEP | Body size logged, not embedded. |
| test_overlapping_windows_share_one_poll_of_a_feed | KEEP | Mutation (share_key None) killed it. Timing-shaped asserts (`1 < nvda.polls < amd.polls`). |
| test_a_window_opening_later_joins_the_feeds_poll | KEEP | Baseline-only first poll. |
| test_an_urgent_request_takes_the_hosts_next_turn_but_keeps_its_gap | KEEP (move) | Mutation (urgent priority removed) killed it. |
| test_fetcher_reports_how_long_a_request_waited_for_its_host | STRENGTHEN (move) | Real socket and `0.15 < queued < 0.4`; the upper bound is a flake source. Use a fake transport and only assert the lower bound. |
| test_a_long_retry_after_is_honored_but_doesnt_hold_the_window_open | KEEP | Single watch; wait_for guard. |
| test_a_long_retry_after_on_a_shared_feed_lets_each_window_close_on_time | KEEP | Different path (shared task). |
| test_late_shared_watcher_closes_during_backoff_and_preserves_it | KEEP | Third distinct path; relies on two `sleep(0)` yields to hit a state (fragile to refactors, deterministic today). |
| test_a_bug_in_posting_doesnt_stop_the_source_polling | KEEP | Mutation (except narrowed) killed it. |
| test_console_output_cant_fail_on_characters_its_encoding_lacks | KEEP | Mutation killed it. |
| test_a_timestamp_that_isnt_text_has_no_time | KEEP | Trivial; parametrize with the datetime case. |
| test_after_the_window_a_source_that_hadnt_shown_the_release_still_links_it | KEEP | Takes 1.01 s: `longest=1s` is a real wait. |
| test_new_wire_items_require_a_results_headline_for_the_scheduled_quarter | KEEP | Good table of headline inputs; should be parametrized for failure clarity. |
| test_resume_enriches_from_a_verified_guessed_page_and_baselines_head_only_urls | KEEP | Name is two behaviors; fine. |
| test_each_watch_reports_its_progress_for_the_watchdog | KEEP | Exact progress sequence. |
| test_repeated_timeouts_as_a_release_goes_live_dont_delay_it | STRENGTHEN | `seen[0] < 0.2` is a wall-clock budget; assert on poll count instead (the claim is "no backoff"). |

## tests/test_unattended.py (24)

| Test | Verdict | Reason |
|---|---|---|
| test_an_alert_retries_then_resumes_after_restart | STRENGTHEN | Real 1.0 s retry delay (delivery.RETRY_DELAYS); patch to 0 like quick_retries does in test_pipeline. |
| test_restart_recovers_message_accepted_before_the_id_was_saved | KEEP | Nonce recovery. Survivor of the duplicate group with release_cards test 3. |
| test_an_alert_someone_deleted_is_posted_again [2] | KEEP | Fresh and reused poster. |
| test_a_resent_alert_keeps_links_edited_into_the_deleted_one | KEEP | Survivor of the deleted-card group. |
| test_a_later_sighting_recreates_the_active_alert_and_keeps_links | MERGE | Same outcome as the previous test, through Release; fold into one parametrized test. |
| test_an_update_recreates_a_deleted_alert_and_is_applied [2] | KEEP | Attachment and summary. |
| test_a_window_that_keeps_failing_is_reported_once | KEEP | Mutation (dedupe removed) killed it. |
| test_both_globenewswire_copies_post_one_alert_from_one_article_read | MERGE | Duplicates test_pipeline's same-release test and replaces record_target with a fake that emits canned sightings, then calls private `runner._watch`. Delete, or keep only a check that Trial(globenewswire=...) yields two sources. |
| test_a_resumed_alert_doesnt_attach_a_file_it_already_has | MERGE | Same behavior as the Nebius test at the end of the file; keep the end-to-end one. |
| test_a_resumed_alert_adds_only_the_links_it_lacks | KEEP | Survivor; parametrize with the old-layout case. |
| test_the_store_commits_without_syncing_the_disk_each_time | REWRITE | Name says synchronous=NORMAL, assertion checks only WAL. Mutation (synchronous pragma removed) survived. Assert `PRAGMA synchronous` equals 1. |
| test_schedule_reload_preserves_running_windows_and_shares_polls | KEEP | Cancels and restarts correctly. |
| test_restart_reports_windows_that_closed_while_offline | KEEP | Mutation killed it. |
| test_calendar_updates_edit_one_card_and_keep_their_log | STRENGTHEN | Slowest test: 5.02 s of real `card_spacing` sleeping. Pass `card_spacing=0` to TrackProgress. Also reaches into `runner._problem` and `runner.dates.status`. |
| test_a_twenty_release_burst_keeps_one_message_per_release_after_restart | STRENGTHEN | 1.0 s from the real retry delay after `lose_response`. Patch delay. Otherwise strong. |
| test_two_schedules_defining_one_release_use_the_first | KEEP | Mutation killed it. |
| test_a_finished_window_isnt_watched_again_after_a_restart | KEEP | started == ["AAA"]. |
| test_an_alert_from_before_the_new_layout_still_takes_only_new_links | MERGE | Fold into the resumed-links test as a second parameter. |
| test_a_window_report_names_the_winner_then_how_far_behind_the_rest_were | STRENGTHEN | Mutation (Stopped early line removed) killed it, but it calls private `unattended._window_report` and packs four scenarios into one body; split into parametrized cases. |
| test_a_restart_resumes_later_links_without_a_second_alert | KEEP | One message after restart. |
| test_changed_sources_rearm_an_active_followup | STRENGTHEN | Monkeypatches private `_resume_links` and inspects the watches it receives; asserts the implementation hook, not an alert or poll. |
| test_a_resumed_alert_gives_no_time_for_a_link_it_already_had | KEEP | Mutation killed it. |
| test_a_start_notice_replaces_the_ones_before_it | KEEP | Mutation killed it. |
| test_a_resumed_alert_doesnt_attach_its_file_again | KEEP | End-to-end with `%` stripping. Survivor. |

## tests/test_release_cards.py (9)

| Test | Verdict | Reason |
|---|---|---|
| test_the_first_alert_is_one_green_card_with_a_ticker_notification | KEEP | Title, author, colour, timestamp. |
| test_late_links_and_files_keep_the_summary_and_first_seen_after_restart | KEEP | Strong. |
| test_a_card_accepted_before_a_crash_is_recovered_without_another_send | MERGE | Duplicate of test_restart_recovers_message_accepted_before_the_id_was_saved. |
| test_a_legacy_alert_keeps_its_summary_when_the_next_link_upgrades_it | KEEP | Legacy embed. |
| test_replacing_a_deleted_card_keeps_its_edited_links_and_summary | KEEP | Adds summary to the deleted-card group. |
| test_long_links_and_summaries_fit_discord_without_losing_recovery_data | KEEP | Exact size budgets. |
| test_a_summary_longer_than_one_embed_shows_every_line | KEEP | Round-trip of full text. |
| test_a_later_link_keeps_a_continued_summary_whole | KEEP | Mutation killed it. |
| test_catchup_and_replay_labels_stay_visible_and_legacy_content_is_untouched | STRENGTHEN | Two unrelated behaviors in one test; split. |

Imports FakeDiscord and FakeMessage from tests/test_unattended.py (move to tests/fakes.py).

## tests/test_release_timeout_replay.py (2 functions, 6 cases)

| Test | Verdict | Reason |
|---|---|---|
| test_release_survives_repeated_timeouts_and_a_temporary_200_page [4] | KEEP | Excellent end-to-end: real fetch queues and watch loop, exact timeout and request counts. Risk: 0.02 s real request deadlines (see Flakiness). |
| test_an_independent_issuer_posts_while_another_request_is_still_stalled [2] | KEEP | Proves non-serialization by ordering, not speed. Takes 1.0 s per case because the window runs to its real end. |

## tests/test_burst_replay.py (1) and tests/burst_replay.py

| Test | Verdict | Reason |
|---|---|---|
| test_the_burst_replay_alerts_once_for_every_company_and_keeps_to_secs_limit | STRENGTHEN | Valuable: the only end-to-end run of record() with IR, RSS, wire and both SEC polls. Mutations (queue_key per host; SEC gap removed) both killed it. Weakness: `replay()` forces `fetch._SHARED_QUEUES` itself, so changing the default to `()` survived. 4.19 s of real time (1.5 s warmup + 2 s tail), and wall-clock-sensitive asserts. |

Harness verdict: worth keeping, but not as one 996-line file in tests/. Measured composition: ~560 lines are the scenario itself (Company, World, ReplayFetcher, FakePoster, replay). The test reads 6 output keys (missed_alerts, duplicate_alerts, no_body, caught_up_at_first_poll, errors, sec.most_in_any_second, alerts_from). About 400 lines are benchmark-only: the CPU-sampling thread `Usage`, ctypes `memory_mb`, `loop_lag`, `spread`/`nearest_rank`, most of `measure()` (percentile tables, per-kind columns), and `show`/`compare`/`main`. CONTRIBUTING.md line 23 requires `python -m tests.burst_replay` before and after source-timing changes, so the tool has a real owner. Recommendation: keep the scenario module in tests/ with a lean `measure()` (the invariants the test needs), and move the reporting CLI (Usage, memory, compare, show, main) to `scripts/` or next to src/first_print/benchmark.py. That also removes a background thread and a ctypes call from every CI run. Also make `replay()` stop overwriting `_SHARED_QUEUES` unless `sec="per-host"` is requested, so the default config is what is tested.

## Mutation checks (26 runs, measured)

| Test | Mutation | Result |
|---|---|---|
| test_a_file_too_big_for_discord_stays_a_link | drop `len(body) > MAX_FILE_BYTES` | killed |
| test_a_restart_doesnt_summarize_a_delivered_release_again | disable the DELIVERED skip | killed |
| test_a_release_gets_at_most_three_model_calls_across_restarts | disable attempts cap | killed |
| test_summary_calls_stop_at_the_days_cap | disable daily cap | killed |
| test_at_most_two_model_calls_run_at_once | MAX_MODEL_CALLS 2 to 20 | killed |
| test_keep_warm_pings_discord_only_while_a_window_is_open | always ping | killed |
| test_fetcher_doesnt_send_cookies_back | remove DummyCookieJar | killed |
| test_a_late_start_catches_up... and test_a_release_out_before_the_window... | drop `_caught_up` in record_items | killed |
| test_an_urgent_request_takes_the_hosts_next_turn_but_keeps_its_gap | urgent priority 0 to 1 | killed |
| test_a_start_notice_replaces_the_ones_before_it | skip deleting stale notices | killed |
| test_an_alert_with_too_many_links_is_cut_to_discords_limit | remove cut in `_content` (line ~619) | killed |
| same test | remove cut in link-merge path (line ~284) | SURVIVED: that truncation of merged resume content has no test |
| test_the_burst_replay... | `queue_key` returns host (no shared SEC queue) | killed |
| test_the_burst_replay... | SEC host gap set to 0 | killed |
| test_the_burst_replay... | default `_SHARED_QUEUES = ()` | SURVIVED: harness overrides the setting |
| test_a_bug_in_posting_doesnt_stop_the_source_polling | narrow `except Exception` | killed |
| test_console_output_cant_fail... | skip `reconfigure(errors=...)` | killed |
| test_the_store_commits_without_syncing_the_disk_each_time | remove WAL pragma | killed |
| same test | remove `synchronous=NORMAL` pragma | SURVIVED: name promises it, assertion does not check it |
| test_a_window_report_names_the_winner... | drop "Stopped early" line | killed |
| test_overlapping_windows_share_one_poll_of_a_feed | share_key forced None | killed |
| test_a_later_link_keeps_a_continued_summary_whole | skip `_summary_text(prior)` | killed |
| test_a_resumed_alert_gives_no_time_for_a_link_it_already_had | drop `stamp < first_seen` guard | killed |
| test_a_window_that_keeps_failing_is_reported_once | always report | killed |
| test_restart_reports_windows_that_closed_while_offline | never report | killed |
| test_two_schedules_defining_one_release_use_the_first | no duplicate detection | killed |
| test_fetcher_reads_a_header_longer_than_aiohttps_default | remove max_field_size | killed |
| test_both_sec_sources_give_the_filing_one_link | `_same_release_link` removed | killed |

Not mutatable: test_a_newly_added_source_posts_like_any_other and test_the_summary_starts_as_soon_as_the_text_arrives guard behavior that no longer exists in the source.
One attempted mutation (a label text) did not match the source and was skipped. All source edits reverted; `git status` clean.

## Redundancy groups

1. Deleted-card recreation: unattended `test_a_resent_alert_keeps_links...`, `test_a_later_sighting_recreates...`, release_cards `test_replacing_a_deleted_card...`. One parametrized test (poster-level or Release-level) plus the summary variant.
2. Crash-recovery by nonce: unattended `test_restart_recovers_message_accepted...` and release_cards `test_a_card_accepted_before_a_crash...` are the same scenario.
3. Resumed alert adds only new links: unattended `..._adds_only_the_links_it_lacks` and `..._from_before_the_new_layout...` differ only in link markup; parametrize.
4. Resumed alert and attachment: unattended `..._doesnt_attach_a_file_it_already_has` is subsumed by the Nebius end-to-end test.
5. Same release from both GlobeNewswire copies: test_pipeline and unattended both assert one alert and one GET.
6. Nebius newsroom fakes: `Newsroom` inside the wire-alert test duplicates `NebiusNewsroom`/`nebius()`.
7. Five copies of the 10-line `Fetcher.request` override signature (FakeFetcher, CountingFetcher, RateLimitedFetcher, ListedLate, UploadedLater): use `*args, **kwargs` or one base fake.

## Slow tests (>0.5 s, measured)

| Test | Time | Why |
|---|---|---|
| test_calendar_updates_edit_one_card_and_keep_their_log | 5.02 s | Real card-edit spacing sleeps; set card_spacing=0 |
| test_the_burst_replay_alerts_once... | 4.19 s | 1.5 s warmup + 2 s tail on the real clock |
| test_a_twenty_release_burst_keeps_one_message... | 1.02 s | Real delivery retry delay (1.0 s) |
| test_an_alert_retries_then_resumes_after_restart | 1.01 s | Same retry delay |
| test_after_the_window_a_source_that_hadnt_shown... | 1.01 s | follow_up(longest=1 s) really waits |
| test_fetcher_doesnt_send_cookies_back | 1.01 s | Probably Windows localhost IPv6 fallback (not confirmed) |
| test_an_independent_issuer_posts... [http, browser] | 1.00 s each | Window runs to its 1 s end |
| test_a_refused_poll_is_retried_after_a_growing_pause | 0.60 s | 0.6 s window |
| test_repeated_timeouts..., test_each_watch_reports... | 0.51 s | Window length |

Roughly 13 s of the 27.7 s run is real waiting that could be removed by patching delays or driving a fake clock.

## Flakiness risks

Timing-dependent assertions, highest risk first. None failed in 6 concurrent runs, but those runs did not saturate the CPU.

1. test_release_timeout_replay (all 6 cases): real request deadlines of 0.02 s and exact asserts `(failures, temporary_pages, watched.timeouts, watched.errors) == (3, 1, 3, 0)` and `slow.timeouts == 3`. Any event-loop stall over 20 ms (GC, a loaded CI runner, Windows timer granularity of ~15.6 ms) turns a healthy answer into an extra timeout and breaks the count. Raise the deadline to 0.1-0.2 s for the healthy path, or give the fake session a per-URL deadline so only the stalled requests time out.
2. test_the_burst_replay...: 1.5 s warmup must finish every first poll before R, otherwise `caught_up_at_first_poll`, `missed_alerts` or `errors` fail; `most_in_any_second <= 9` depends on real timer wakeups (already commented as allowing one extra). A slow Linux runner could break warmup. Also starts a CPU-sampling thread.
3. test_the_newsroom_read_waits_out_a_refusal: `answered[0] - refused[-1] >= 0.3` with BLOCK_PAUSE 0.3. asyncio can wake early by up to a timer tick; the sibling tests already loosen to 0.29. Use 0.25.
4. test_repeated_timeouts_as_a_release_goes_live_dont_delay_it: `seen[0] < 0.2` wall-clock budget for ~7 polls at 0.01 s; with 15.6 ms sleeps on Windows that is ~110 ms, only 2x margin.
5. test_fetcher_reports_how_long_a_request_waited_for_its_host: `0.15 < queued < 0.4` on a real socket; upper bound flaky.
6. test_keep_warm_pings...: `pings > 3` after 0.1 s of 0.01 s sleeps, then "quiet" check after 0.02 s; a ping mid-flight at `windows.clear()` can add one.
7. test_overlapping_windows_share_one_poll_of_a_feed: `1 < nvda.polls < amd.polls` with 0.1 s and 0.25 s windows; fine unless the loop stalls.
8. test_a_page_that_comes_during_the_pdf_summary_replaces_it_after and the headline test (`sleep(0.05)`): ordering by sleeps rather than events.
9. test_release_posts_links_files_and_summary: `0 <= live_seconds < 5` uses the real clock; low risk.
10. Wall-clock coupling: uber() is pinned to Nov 4 2026 while Journal uses the system clock, so test_a_missing_file_isnt_retried_after_the_window_closes passes only because today is past that date. Several unattended tests use `datetime.now(UTC) + timedelta(...)`, which is safe but should use PinnedClock like the others.
11. No `pytest-timeout` and many `await asyncio.Event().wait()` fakes: a regression that stops a window from closing hangs the whole run instead of failing one test. Only some tests use `asyncio.wait_for`.
12. Platform: no skips found in these files, so Linux CI covers them; `memory_mb` has a win32 and a Linux branch, only the current one runs.

## Untested important behavior in this area (coverage of these files only, measured)

Coverage with only these tests: pipeline.py 87%, recorder.py 88%, release_cards.py 96%, delivery.py 97%, unattended.py 53%, fetch.py 63%. Gaps worth tests:
1. unattended `_watch` window-close path (benchmark, window report posting, follow-up scheduling; lines ~702-752) and `watch_directory`/`run` startup wiring (886-1057, the restart entry point).
2. recorder `route_trouble` (degraded route after 90 s quiet, lines 723-735) has no test here.
3. pipeline browser-feed reading and attachment (`_read_browser_feed`, `_browser_attachment`, lines 756-842).
4. pipeline merged-content truncation on a resumed alert (line ~284; mutation survived).
5. recorder `main`/`_record_to` CLI (1199-1231) and the standalone pipeline `run` with `keep_warm` wired in (746-747).

## Suite-wide practice issues

- No conftest.py. Shared fakes are imported across test modules (`from tests.test_pipeline import FakePoster, uber` in test_unattended; `from tests.test_unattended import FakeDiscord` in test_release_cards). Move FakePoster, FakeSummarizer, FakeDiscord, FakeMessage, `uber()` and an `events(log)` reader into tests/fakes.py or fixtures.
- `json.loads(line) for line in log.getvalue().splitlines()` is repeated about 30 times in test_pipeline.py and unattended; test_recorder defines `events()` but others do not use it.
- `Journal(io.StringIO())` plus the `log` handling is hand-built in nearly every test; a `journal` fixture (with PinnedClock) would cut setup and remove the wall-clock coupling.
- 1,012 DeprecationWarnings from discord.py (`count` passed positionally) in the run; no `filterwarnings` entry, so a real new warning would be invisible. Filter that one message in pyproject.
- Tests reaching private names: `runner._watch`, `runner._problem`, `runner._resume_links` (monkeypatched), `unattended._window_report`, `release._refused`-style helpers. Prefer public paths or at least name the private contract.
- Large tests (20, 14, 15 in their files) mix several scenarios; docstrings recount incidents (good) but assertions should be split.
- Fetcher-level tests (sockets, cookies, headers, pacing) live in test_recorder.py instead of test_fetch.py.
- Real `asyncio.sleep` for ordering in several tests; prefer asyncio.Event or a controllable clock.

## Fixes applied (branch tests-fix-c)

Survivors: (1) merged-resume content cut: gap, fixed by `test_links_merged_into_a_resumed_alert_are_cut_to_discords_limit` (fails with the cut removed, passes without). (2) `_SHARED_QUEUES = ()`: already caught by 8 tests in test_fetch.py (measured, full suite); the harness no longer overrides the default unless `sec="per-host"`. (3) `synchronous=NORMAL`: gap, the store test now asserts `PRAGMA synchronous == 1` (fails with the pragma removed).

Slow tests (measured, serial): calendar-update card test 5.02 s to 0.09 s (`card_spacing=0`); twenty-release burst 1.03 s to <0.1 s and alert-retry-restart 1.02 s to <0.1 s (RETRY_DELAYS patched); follow-up test 1.00 s to ~0.05 s; cookie test 1.00 s to 0.27 s (gap patched); timeout replay 1.0 s x2 to 0.17 s, other four 0.5 s to 0.22-0.28 s (stalled issuer's window is now cancelled once the healthy one posted; healthy deadline 0.02 s raised to 0.05 s); newsroom refusal tests 0.3 s to 0.1 s, refusal now by count instead of time; keep-warm and PDF-during-summary use events. Burst replay stays 4.2-4.3 s: shortening warmup or tail made the SEC-gap mutation (gap 0) survive, so the real-clock budget is needed. Not changed: three 0.5-0.6 s recorder tests whose window runs to its end.

Verdicts: deleted 4 tests (trial-gating tombstone, GlobeNewswire duplicate via private `_watch`, resumed-attach duplicate, release_cards crash-recovery duplicate) and one dead helper; merged 2 into one parametrized resumed-links test; rewrote 2 (summary tombstone now asserts no request is made; Nebius wire test rebuilt on shared helpers with `unlisted_looks`); strengthened/split 6 (window report split in 4, catch-up/legacy split, journal clock pinned in 2 tests, headline test awaits `releases.checks`, `seen == [8]` poll count instead of wall clock, burst harness). Mutation spot-checks (all killed): drop `_same_release_link` (2 cases), keep-warm always ping, route pause `left()` = 0 (3 tests), attach-retry window check removed (2 tests), merge cut, synchronous pragma.

Skipped: moving five Fetcher tests to test_fetch.py (area D owns it); folding `test_a_later_sighting_recreates...` into the resent-alert test (different layers); splitting the burst harness (large, out of scope); coverage gaps listed above.
