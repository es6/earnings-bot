# Test audit G: completion of the omitted files

An inventory check at the original audit base `0dc9d97` finds 737 test functions. A–F list 704; the following four files (33 functions) were omitted from those assignments. This supplement completes that inventory. Verdicts are review judgments; executable measurements are identified separately.

A fresh selected run on `41223f6` passed all 54 parameterized cases in these four files. Four deliberate source regressions were then tried separately and restored byte-for-byte: primary-only subscribers, whole-page candidate matching, removal of exact wire-ID checking, and an empty comma parser. All four were killed by the selected regression test. These are focused mutation checks, not 33 or 737 mutation proofs.

Importantly, the comma-parser mutation is the same behavior that F reports as surviving CF4 in its narrower scope. `test_servers.py` catches it. This disproves the aggregate claim that every reported survivor can evade the whole suite. Other A–F survivors still need suite-wide confirmation before they are called global gaps.

Counts: KEEP 25, STRENGTHEN 8.

## tests/test_issuer_surfaces.py

| Test function | Verdict | Evidence / next action |
|---|---|---|
| `test_public_pagination_recovers_road_history_without_a_fixed_clock_exception` | KEEP | Real linked pages assert six regular releases, both clock slots and exclusion of preliminary results. |
| `test_history_follows_only_bounded_linked_pages_and_same_host` | STRENGTHEN | Covers wrong zone, external next link and duplicate identity; add a four-page chain to isolate the three-page cap. Done: `test_history_reads_at_most_three_linked_pages` (fails with the cap at 4). |
| `test_release_history_reads_a_feed_that_requires_the_owned_browser` | STRENGTHEN | Positive owned-browser feed is valuable; add refused/malformed responses and disabled-browser controls. Done: parametrized readable/refused/malformed/browser-held (browser-held fails without the `chrome_allowed()` guard). |
| `test_exact_publisher_listing_identifies_a_wire_missing_from_the_exhibit` | KEEP | Matches issuer/quarter and nearby verified filing; rejects wrong period and distant dates. |
| `test_jpm_manifest_discovers_and_verifies_the_real_random_pdf` | KEEP | Real manifest/PDF plus role, host and period controls exercise acquisition, not a fixed URL. |
| `test_manifest_discovery_requires_the_pages_declaration_and_linked_script` | STRENGTHEN | Keeps discovery provenance explicit; add a third-script case to isolate the script-read ceiling. Done: `test_manifest_discovery_reads_at_most_two_scripts` (fails with the limit at 3). |

## tests/test_servers.py

| Test function | Verdict | Evidence / next action |
|---|---|---|
| `test_other_servers_alert_channels_come_from_one_comma_separated_setting` | KEEP | Checks parsing, deduplication, primary precedence and absence. Newly kills the config-parser mutant reported as CF4. |
| `test_every_server_gets_each_step_and_one_refusal_doesnt_stop_the_others` | KEEP | Checks both send/summary steps, first-seen propagation, partial refusal and the single-server path. |
| `test_a_company_other_servers_track_is_watched_once_and_alerted_in_each` | STRENGTHEN | Four-server delivery isolation and one shared watch are useful; replace elapsed waits with completion events. Newly kills subscriber removal. FIXED: release is poll-count driven (PolledReleaseDay) and the clock jumps after both alerts; 3.05 s to under 0.1 s (measured). |
| `test_a_command_works_with_the_list_of_the_server_its_used_in` | KEEP | Exercises command output and tracked-list isolation across guilds. |
| `test_a_schedule_files_release_goes_to_the_deployments_own_server` | KEEP | Preserves file-only releases and the primary-server fallback. |
| `test_the_watcher_gives_each_configured_server_its_own_list_and_scorecards` | KEEP | Checks state separation and own scorecard stores through watcher construction. |
| `test_the_bot_registers_its_commands_in_every_configured_server` | KEEP | Checks actual command synchronization calls for every configured guild. |
| `test_the_shared_calendar_reads_each_company_once_and_writes_to_every_server` | KEEP | Checks deduplication, finding propagation only to trackers and prohibition of shared add/remove. |
| `test_a_company_two_servers_track_is_researched_once_for_both` | KEEP | Runs date research through the shared calendar and checks both persisted targets. |
| `test_each_companys_speed_audit_runs_once_for_every_server` | STRENGTHEN | Success/failure controls check shared claims, cooldown and both cards; replace short real sleeps with barriers. FIXED: waits on each channel's sent event instead of sleeps; 0.49 s and 0.42 s to 0.11 s (measured). |
| `test_a_server_takes_research_another_did_after_a_quick_check` | KEEP | Verifies copied profile/date/review and exactly one evidence check rather than fresh SEC investigation. |
| `test_what_fails_the_check_is_researched_again` | KEEP | Parametrized block, gone, throttle and outage responses cannot validate a borrowed date. |
| `test_manual_dates_are_not_borrowed_and_future_audits_are_not_current` | KEEP | Prevents cross-server manual overrides and future-dated review reuse. |
| `test_primary_server_commands_are_registered_without_a_test_guild_setting` | KEEP | Protects the primary-channel guild discovery edge case. |
| `test_debug_managers_are_scoped_to_configured_servers_and_owner_stays_global` | KEEP | Checks configured managers and owner-anywhere access at the command boundary. |

## tests/test_source_observations.py

| Test function | Verdict | Evidence / next action |
|---|---|---|
| `test_recorder_honors_http_date_retry_after` | STRENGTHEN | Exercises recorder status handling; freeze UTC to replace the five-second tolerance with an exact assertion. |
| `test_retry_dates_are_utc_past_dates_are_zero_and_bad_values_fall_back` | KEEP | Deterministic controls cover UTC dates, numeric values, expired values and fallback. |
| `test_trial_never_stitches_a_candidate_from_different_entries` | STRENGTHEN | RSS/HTML/JSON negative fixtures newly kill whole-page matching; assert observations exist to avoid vacuity and close SQLite explicitly. |
| `test_feed_and_html_candidates_keep_the_matching_entry_identity` | KEEP | Positive RSS, Atom and nested HTML establish the matching canonical entry. |
| `test_hidden_script_text_and_nonweb_links_cannot_identify_a_release` | KEEP | Rejects hidden script evidence and non-web identities. |
| `test_trial_positive_records_the_matching_link_not_unrelated_page_links` | STRENGTHEN | Checks persisted identity and scope at the runner boundary; close SQLite explicitly and isolate scheduling from wall time. |

## tests/test_wire_mirror.py

| Test function | Verdict | Evidence / next action |
|---|---|---|
| `test_known_wire_release_recovers_tables_with_durable_retrieval_provenance` | KEEP | Real article, tables and JSON roundtrip preserve canonical versus retrieval provenance. |
| `test_mirror_rejects_wrong_release_or_teaser` | KEEP | Issuer, period, ID, lookalike, teaser and template negatives; newly kills removal of exact wire-ID matching. |
| `test_no_candidate_for_unrecognized_wire_url` | KEEP | Rejects wrong language, lookalike wire host and invalid ID date. |
| `test_mirror_date_comes_from_publication_and_uses_unpadded_month_and_day` | KEEP | Protects a real publication date different from the wire-ID date. |
| `test_full_nested_story_survives_a_missing_date_spelling` | KEEP | Real nested article ensures complete report/tables and at most the documented fallback. |
| `test_mirror_backoff_is_shared_by_other_releases_and_spelling_fallbacks` | KEEP | Checks 403/429/503, case-insensitive headers, server wait and no second transfer during pause. |


## Fix results for tests/test_wire_mirror.py (branch tests-fix-d)

All six rows stay KEEP. Measured: the file passes in 0.24 s at most per test (the slowest is `test_full_nested_story_survives_a_missing_date_spelling`), uses no real waiting, and was passed unchanged in two random-order serial runs and in the whole-suite `-n auto` run. Nothing was changed in it. The server-wait case in `test_mirror_backoff_is_shared_by_other_releases_and_spelling_fallbacks` is already driven by response headers, not by sleeping.

### Fix pass: tests/test_source_observations.py
Retry-After test now freezes UTC and asserts exactly 3600; the module shrinks the host turn gap
(trial tests 1.2 s -> 0.15 s each); the stitching test asserts both polls were observed.
