# Test audit E: summaries (15 files)

Baseline (measured): 173 test functions, 333 collected cases, 333 passed in 14.91 s. Worktree detached at origin/main (0dc9d97), clean after all mutation runs (`git status` empty). No model call, network or Discord post was made. Only pytest ran; the offline CLI tests in test_summary_evaluation.py run `scripts/summary_prepare.py` / `summary_eval.py`, which build request JSON only.

## What is live and what is not (read from source)

| Module | Status | Evidence |
|---|---|---|
| summary_validation, summary_format (discord_brief), summary_request.structured_request, summary_policy.SummaryFact/normalized/change_reference, summary.Summarizer legacy path | LIVE | `Summarizer.write` calls `structured_request` -> `validate_facts` -> `discord_brief` |
| source_document (document_from_html/text), DeliveryStore summary_documents/summary_evidence tables | LIVE (extraction + persistence); the evidence blob itself only exists in opt-in mode | pipeline builds a `SourceDocument` and calls `write_document`, which defaults to the legacy path |
| summary_binding, binding_text, summary_evidence, summary_bound_format, referenced_request, `Summarizer(references=True)` | OPT-IN (`SOURCE_REFERENCED_SUMMARIES`, default false) | `summarizer_from` passes `settings.source_referenced_summaries` |
| driver_profile, driver_profiles, driver_profile_store | OPT-IN (`LIVE_DRIVER_PROFILES`, default false) | `bot.py` passes the flag into `unattended` |
| summary_evaluation, summary_policy.evidence_issues, DEFAULT_COMPARISON | OFFLINE (scripts/summary_eval.py, summary_prepare.py) | no src caller |
| summary_policy.yoy, cash_priority, MetricKey, Observation, SummaryFact.identity | DEAD: no caller in src/ or scripts/ (only docs/summaries.md and tests) | grep |
| summary_review (review_variant, headline_repair_request, BRIEF_EXAMPLES) | UNWIRED: no caller in src/ or scripts/ (only docs and its own test) | grep |

Column key below: LIVE / OPT-IN / OFFLINE / DEAD / UNWIRED.

## Do test_driver_profiles.py and test_driver_profile.py overlap?

No meaningful overlap. They test different modules:
- test_driver_profile.py (6 tests) tests `driver_profile.py` (pure `parse_profile`, `profile_request`, reservation math) and `driver_profile_store.py` (claim/finish SQL). The two store tests live in the wrong file name.
- test_driver_profiles.py (5 tests) tests the orchestrator `driver_profiles.py` (`DriverProfiles.ensure`, pause rules, cancellation, `/track` reuse).
- Mild shared ground: "an uncertain paid call is never repeated" is asserted at the store layer (test_driver_profile::test_a_sent_profile_call_is_not_repeated_after_a_restart) and the orchestrator layer (test_driver_profiles::test_an_uncertain_profile_call_is_not_repeated_after_cancellation). Different failure modes (restart vs cancellation), so keep both.
- The real smell is naming and coupling: near-identical file names, and test_driver_profiles.py imports `SOURCE, TODAY, content` (and, inside a test, `parsed`) from test_driver_profile.py. Rename to test_driver_profile_parsing.py (+ store) and test_driver_profiles_service.py and move the shared fixtures to conftest.

## Per-file tables

### tests/test_summary_binding.py (39) — all OPT-IN (summary_binding + binding_text)

| test | live or opt-in | verdict | reason |
|---|---|---|---|
| wrong_row_wrong_period_wrong_sign_wrong_scale_and_wrong_unit_are_rejected | OPT-IN | STRENGTHEN | killed M4/M5, but a for-loop of 7 cases in one test; parametrize so a failure names the case |
| the_two_reproduced_semantic_holes_are_closed_with_a_valid_control | OPT-IN | MERGE | same wrong-metric / wrong-period input classes as the row above, only on plain text; add a text-document case to the parametrized test |
| a_comparative_table_without_column_periods_is_not_assigned_the_headline_period | OPT-IN | KEEP | adversarial plus control |
| split_accounting_signs_are_kept_and_cannot_turn_a_loss_positive | OPT-IN | KEEP | sign both ways |
| qualified_physical_units_do_not_inherit_the_money_caption | OPT-IN | KEEP | accept and reject |
| a_business_update_cannot_remove_a_negative_sign | OPT-IN | KEEP | |
| a_number_from_a_different_metric_in_the_same_sentence_does_not_bind | OPT-IN | KEEP | two controls plus the attack |
| a_metric_name_embedded_in_a_margin_does_not_bind_the_absolute_measure | OPT-IN | KEEP | |
| guidance_ranges_keep_old_and_new_columns_separate | OPT-IN | KEEP | |
| growth_columns_are_not_actuals_and_percent_headers_supply_the_unit | OPT-IN | KEEP | |
| a_bound_but_noncomparable_prior_quarter_cannot_become_a_yoy_pair | OPT-IN | KEEP | killed M3 |
| growth_for_a_different_accounting_basis_is_not_attached_to_the_actual | OPT-IN | KEEP | killed M2 |
| column_duration_and_reported_basis_are_bound_without_global_row_search | OPT-IN | KEEP | |
| group_attribution_is_checked_against_its_source_section | OPT-IN | KEEP | |
| an_invalid_optional_prior_is_dropped_while_the_bound_current_value_survives | OPT-IN | KEEP | |
| unsupported_business_consequences_are_held_back (3 params) | OPT-IN | KEEP | |
| a_measure_is_named_by_its_key_words_not_its_exact_label | OPT-IN | KEEP | rounding boundary included |
| each_basis_in_one_sentence_binds_to_its_own_number | OPT-IN | KEEP | killed M1 |
| year_over_year_is_not_read_as_more_than | OPT-IN | KEEP | |
| a_decrease_is_a_negative_growth_rate | OPT-IN | KEEP | |
| a_fiscal_label_is_compared_with_calendar_columns_through_the_release_offset | OPT-IN | KEEP | |
| a_change_column_is_headed_by_the_year_it_compares_with | OPT-IN | KEEP | |
| a_count_can_name_what_it_counts_after_the_number | OPT-IN | KEEP | |
| text_laid_out_in_a_table_cell_is_read_as_text | OPT-IN | KEEP | |
| a_layout_table_before_the_reporting_title_does_not_erase_its_period (2 params) | OPT-IN | KEEP | |
| a_reporting_title_cannot_turn_an_outlook_value_into_an_actual (2 params) | OPT-IN | KEEP | |
| a_full_year_label_cannot_hide_a_quarter_guidance_duration | OPT-IN | KEEP | |
| unstated_labels_are_dropped_and_a_bad_fact_does_not_sink_the_brief | OPT-IN | KEEP | |
| positions_alone_never_bind_a_value | OPT-IN | KEEP | |
| a_segment_row_is_measured_by_the_sentence_that_introduces_its_table | OPT-IN | KEEP | |
| a_segment_row_is_measured_by_its_value_s_column_header | OPT-IN | KEEP | |
| an_abbreviation_the_release_defines_names_its_segment | OPT-IN | KEEP | |
| net_can_be_left_out_of_a_measure_unless_the_release_lists_both | OPT-IN | KEEP | |
| guidance_and_growth_name_a_value_s_slot_not_its_measure | OPT-IN | KEEP | |
| a_measure_named_right_after_its_number_binds_only_as_that_measure | OPT-IN | KEEP | |
| earnings_per_share_can_follow_the_net_income_it_divides | OPT-IN | KEEP | |
| a_group_naming_the_issuer_or_the_fact_s_role_is_no_segment | OPT-IN | KEEP | |
| a_heading_row_above_a_segment_names_its_measure | OPT-IN | KEEP | |
| a_count_of_people_or_things_is_never_bound_as_money | OPT-IN | KEEP | |

### tests/test_summary_format.py (25) — LIVE (discord_brief / format_*); one test also touches OFFLINE display_lines

| test | live or opt-in | verdict | reason |
|---|---|---|---|
| units_and_ranges_keep_their_meaning (29 params) | LIVE | KEEP | rule table, not a snapshot of one string |
| fiscal_periods_are_explicit_without_duplicate_prefixes (18 params) | LIVE | KEEP | |
| duplicate_acronyms_are_removed_without_dropping_the_measure | LIVE | KEEP | |
| compound_legacy_values_keep_their_unit_note_without_guessing | LIVE | KEEP | |
| model_audit_reason_is_separate_from_the_display | LIVE | KEEP | the `not in` line is redundant with the equality above it |
| cash_flow_duration_survives_the_live_and_replay_formatters | LIVE+OFFLINE | KEEP | guards the live/replay formatter agreeing |
| basis_is_preserved_and_only_equivalent_labels_are_deduplicated (4) | LIVE | KEEP | |
| all_selected_kpis_and_guides_survive_a_long_brief | LIVE | KEEP | |
| the_replay_uses_safe_formatting_and_preserves_late_kpis | OFFLINE | KEEP | tests summary_evaluation.display_lines |
| change_notation_preserves_direction_and_known_comparison (10) | LIVE | KEEP | killed M17 |
| explicit_basis_can_label_a_change_without_guessing_from_the_current_quarter | LIVE | KEEP | |
| identical_metric_names_keep_their_distinct_segment_and_country_identity | LIVE | KEEP | |
| existing_scope_labels_and_neutral_consolidation_are_not_repeated | LIVE | KEEP | |
| a_growth_rate_without_a_level_reads_as_the_rate | LIVE | KEEP | |
| long_or_repeated_basis_notes_stay_out_of_the_label | LIVE | KEEP | overlaps basis_is_preserved; fold into one basis parametrization |
| the_card_groups_facts_by_period_and_kind | LIVE | KEEP | full-card exact string, but each line asserts a grouping rule |
| negative_rounded_and_approximate_amounts_read_cleanly (11) | LIVE | MERGE | same function as units_and_ranges (`format_value`); one row duplicates `$2,365-$2,395 million`; merge tables |
| one_quarter_has_one_label (8) | LIVE | MERGE | same function as fiscal_periods (`format_period`); merge tables |
| a_count_keeps_what_it_counts_and_a_margin_its_unusual_base | LIVE | KEEP | |
| default_bases_and_consolidated_scopes_stay_off_the_label (6) | LIVE | KEEP | |
| a_level_gets_its_change_from_the_year_ago_figure | LIVE | KEEP | |
| headline_results_and_outlook_lead_the_brief | LIVE | KEEP | |
| a_quarter_written_out_joins_the_quarter | LIVE | KEEP | |
| segments_share_one_line_largest_first_with_their_growth | LIVE | KEEP | |
| the_company_named_as_a_scope_on_most_lines_is_dropped | LIVE | KEEP | |

### tests/test_summary_evidence.py (12) — OPT-IN (parse_referenced_brief, referenced_request), plus OFFLINE script prepare

| test | live or opt-in | verdict | reason |
|---|---|---|---|
| values_keep_separate_periods_bases_scales_and_material_comparison_qualifiers | OPT-IN | KEEP | also proves frozen model |
| malformed_numeric_candidates_are_rejected_without_coercion (12) | OPT-IN | KEEP | `pytest.raises(ValueError)` with no `match`; a wrong-reason ValueError would pass (low risk, add match) |
| evidence_cannot_escape_its_document_block_or_table (8) | OPT-IN | KEEP | each case asserts its reason |
| every_value_and_every_citation_is_checked_including_old_guidance | OPT-IN | KEEP | |
| updates_keep_their_supported_consequence_and_plain_text_has_no_cell_geometry | OPT-IN | KEEP | |
| nested_outer_layout_table_is_not_independent_cell_evidence | OPT-IN | KEEP | killed M23 |
| existing_references_are_explicitly_not_a_metric_or_period_correctness_check | OPT-IN | DELETE | pins that the integrity guard accepts a wrong metric/period (`type(candidate).__name__ == "UnverifiedBrief"`); an anti-test for a limitation that test_summary_binding already covers; it fails the day someone adds the check here |
| request_preserves_geometry_uses_ember_low_and_keeps_the_schema_stable | OPT-IN | STRENGTHEN | five concerns in one test (geometry, model, schema stability, prompt substrings, schema defensive copy); split |
| input_limits_reject_rather_than_silently_truncate | OPT-IN | KEEP | |
| zero_negative_values_and_qualified_counts_keep_their_explicit_units (3) | OPT-IN | KEEP | |
| empty_facts_are_rejected (2) | OPT-IN | KEEP | |
| offline_preparation_uses_html_when_saved_and_plain_blocks_otherwise | OFFLINE | KEEP | imports `scripts.summary_prepare` in-process (good, fast) |

### tests/test_summary_validation.py (16) — LIVE (legacy checker)

| test | live or opt-in | verdict | reason |
|---|---|---|---|
| real_margin_is_not_growth_and_valid_arithmetic_survives | LIVE | KEEP | |
| margin_as_growth_fails_without_prior_or_arithmetic | LIVE | KEEP | |
| table_percentages_ignore_the_money_scale_and_cash_burn_keeps_sign | LIVE | KEEP | |
| real_ytd_cash_flow_cannot_be_full_year_or_quarter | LIVE | KEEP | |
| combined_table_ytd_column_is_not_quarter | LIVE | KEEP | |
| numbers_elsewhere_in_release_do_not_validate_a_quote | LIVE | KEEP | |
| count_currency_and_empty_display_fields_are_rejected | LIVE | KEEP | killed M15 |
| unknown_quote_period_units_and_reference_are_not_repaired | LIVE | STRENGTHEN | loop only asserts "some issue"; M14 (drop the missing-period rule) survived because another rule shadows it; assert the expected reason per mutation |
| model_quoting_styles_still_bind_to_the_release | LIVE | KEEP | killed M16 |
| same_quarter_adjustment_reference_and_exclusion_scope | LIVE | KEEP | |
| per_share_figures_keep_their_units_in_a_millions_table | LIVE | KEEP | |
| an_exclusion_is_supported_by_its_word_in_the_quote | LIVE | KEEP | |
| a_range_scale_written_once_covers_both_ends | LIVE | KEEP | |
| a_yoy_label_comes_only_from_the_quoted_wording | LIVE | KEEP | |
| a_table_caption_sets_the_scale_however_it_names_the_currency (6) | LIVE | KEEP | |
| prose_about_millions_is_not_a_table_caption | LIVE | KEEP | |

### tests/test_summary_evaluation.py (12) — OFFLINE

| test | live or opt-in | verdict | reason |
|---|---|---|---|
| coverage_does_not_reward_a_different_measure (7) | OFFLINE | KEEP | killed M27 |
| total_revenue_alias_and_explicit_growth_engine_can_match | OFFLINE | KEEP | |
| equivalent_measure_labels_are_not_marked_missing (8) | OFFLINE | KEEP | |
| label_normalization_preserves_meaningful_differences (4) | OFFLINE | KEEP | |
| actuals_do_not_cover_guidance_and_one_line_cannot_fill_two_items | OFFLINE | KEEP | |
| an_outlook_is_never_coverage_of_a_reported_result (2) | OFFLINE | KEEP | |
| a_segment_total_is_not_company_revenue | OFFLINE | KEEP | killed M28 |
| replay_exposes_evidence_and_period_issues_without_calling_them_accuracy | OFFLINE | KEEP | |
| only_shared_issuer_run_pairs_are_compared | OFFLINE | KEEP | |
| offline_cli_uses_default_pair_and_code_baseline_and_writes_blind_key | OFFLINE | KEEP | real subprocess (0.18 s), checks blind key hides model names |
| future_requests_share_policy_schema_and_default_pair | OFFLINE | REWRITE | 6.30 s (subprocess + cold imports); `sys.executable scripts/...` depends on cwd being the repo root; mostly system-prompt substring snapshots; call `prepare()` in-process as test_summary_evidence does |
| future_requests_never_silently_truncate_a_release | OFFLINE | REWRITE | 3.08 s subprocess for one ValueError; `pytest.raises` on `prepare()` in-process |

### tests/test_summary_bound_format.py (11) — OPT-IN

| test | live or opt-in | verdict | reason |
|---|---|---|---|
| a_window_ending_on_a_date_is_not_a_point_in_time (4) | OPT-IN | REWRITE | tests `binding_text.duration` (inline import), not the formatter; move to the binding tests |
| a_growth_rate_keeps_its_rolling_observation_window | OPT-IN | KEEP | |
| year_only_guidance_keeps_its_verified_quarter_horizon | OPT-IN | KEEP | |
| actual_prior_and_material_comparable_growth_survive_together | OPT-IN | KEEP | |
| margin_changes_are_basis_points_and_level_qualifiers_are_not_dropped | OPT-IN | KEEP | |
| zero_negative_prior_or_losses_show_levels_instead_of_misleading_growth (3) | OPT-IN | KEEP | killed M26 |
| qualified_counts_keep_units_and_do_not_get_precise_growth_from_approximations | OPT-IN | KEEP | M38 (drop the "exact only" condition on the `+` sign) survived, so the sign rule for qualified values is untested |
| guidance_ranges_and_quarter_actuals_keep_their_own_periods | OPT-IN | KEEP | |
| only_bound_values_reach_the_renderer_and_a_rejected_comparison_is_absent | OPT-IN | KEEP | |
| no_fixed_word_line_or_fact_limit_and_multiple_growth_periods_are_not_collapsed | OPT-IN | KEEP | |
| per_share_precision_currency_and_previous_only_guidance_remain_explicit | OPT-IN | KEEP | three concerns, still cheap |

### tests/test_source_document.py (11) — LIVE (extraction); references/cell resolve are consumed by the OPT-IN binder

| test | live or opt-in | verdict | reason |
|---|---|---|---|
| blocks_and_references_are_stable_and_scoped_to_the_document_version | LIVE | KEEP | |
| cells_keep_source_rows_columns_headers_blanks_and_decorations | LIVE | KEEP | |
| rowspan_keeps_the_header_over_the_original_column | LIVE | KEEP | |
| nested_layout_tables_remain_distinct_and_mark_the_outer_binding_uncertain | LIVE | KEEP | |
| evidence_extraction_preserves_the_live_readers_text (4) | LIVE | KEEP | guards the live text reader against drift |
| plain_pdf_text_does_not_gain_invented_table_cells | LIVE | KEEP | |
| persisted_document_restores_frozen_blocks_and_cell_references (2) | LIVE | KEEP | |
| unknown_persisted_document_version_is_rejected | LIVE | KEEP | |
| an_extraction_version_change_invalidates_old_reference_hashes | LIVE | KEEP | |
| huge_declared_spans_are_bounded_without_allocating_a_square_grid | LIVE | KEEP | killed M13 (clamp lives in text.py) |
| empty_sources_have_no_bindable_document (2) | LIVE | KEEP | |

### tests/test_summary.py (5) — LIVE (legacy writer)

| test | live or opt-in | verdict | reason |
|---|---|---|---|
| confirmed_facts_are_laid_out_and_unconfirmed_ones_held_back | LIVE | KEEP | end-to-end with fake stream; killed M18 |
| document_entry_point_preserves_the_current_model_request_and_validation | LIVE | KEEP | proves write_document == write with references off |
| advisory_context_reaches_the_request_but_cannot_verify_a_claim | LIVE | KEEP | killed M32 |
| a_refusal_a_cut_off_or_an_unconfirmed_answer_is_an_error (4) | LIVE | KEEP | API-status and connection errors are not covered (see untested) |
| summaries_need_the_switch_and_the_key | LIVE | KEEP | killed M33 (cost gate) |

### tests/test_driver_profiles.py (5) — OPT-IN

| test | live or opt-in | verdict | reason |
|---|---|---|---|
| track_research_builds_and_reuses_a_durable_profile | OPT-IN | KEEP | restart loop plus one-call assertion; patches `latest_profile_source` and DateDiscovery privates (`_check`, `_prepare_cache`) |
| a_window_opening_during_profile_source_retrieval_defers_the_paid_call | OPT-IN | KEEP | M11 survived, but the lock-guard recheck below it makes the mutated line redundant (equivalent mutant, not a test gap) |
| an_uncertain_profile_call_is_not_repeated_after_cancellation | OPT-IN | KEEP | |
| an_incomplete_profile_does_not_replace_previous_valid_context (3) | OPT-IN | STRENGTHEN | M8 (remove the `stop == "end_turn"` check) survived: the stub answer is `"{}"`, which `parse_profile` rejects anyway, so the refusal and max_tokens params prove nothing. Return a valid profile body with `stop="max_tokens"`/`"refusal"` |
| deferred_research_cannot_start_profile_source_retrieval | OPT-IN | KEEP | |

### tests/test_referenced_writer.py (5) — OPT-IN

| test | live or opt-in | verdict | reason |
|---|---|---|---|
| reference_mode_binds_each_value_and_preserves_auditable_rejections | OPT-IN | KEEP | |
| reference_failures_do_not_fall_back_to_the_legacy_checker (4) | OPT-IN | KEEP | killed M7; asserts exactly one request |
| reference_mode_requires_explicit_opt_in_and_disables_sdk_retries | OPT-IN | KEEP | killed M6; also the only test that default mode is legacy |
| reference_result_survives_pipeline_persistence_and_restart_without_a_new_call | OPT-IN | KEEP | |
| evidence_versions_and_attempts_never_attach_old_binding_to_a_new_result | OPT-IN | KEEP | |

### tests/test_driver_profile.py (6) — OPT-IN

| test | live or opt-in | verdict | reason |
|---|---|---|---|
| profile_keeps_evidence_for_every_category_and_dates_from_code | OPT-IN | KEEP | killed M9 |
| an_existing_quote_does_not_support_an_invented_driver_label (2) | OPT-IN | KEEP | |
| a_new_review_date_cannot_freshen_an_old_source | OPT-IN | KEEP | |
| profile_request_uses_only_ember_low_and_rejects_oversized_input | OPT-IN | KEEP | killed M29 |
| a_sent_profile_call_is_not_repeated_after_a_restart | OPT-IN | KEEP | killed M10; store test in a parse-test file |
| profile_reservations_bound_both_money_and_call_count | OPT-IN | STRENGTHEN | hard-coded 250_000 / 5 / 1000 silently depend on DAILY_PROFILE_MICROS / DAILY_PROFILE_CALLS; import the constants |

### tests/test_source_document_flow.py (5) — LIVE (reader/store); opt-in only for the evidence blob

| test | live or opt-in | verdict | reason |
|---|---|---|---|
| html_cell_references_reach_the_writer_and_survive_a_store_restart | LIVE | KEEP | uses private `release._text.read_document`; 60x padding is unexplained |
| pdf_reader_retains_plain_text_without_inventing_cells | LIVE | KEEP | |
| classified_exhibit_text_is_not_reinterpreted_as_html | LIVE | KEEP | asserts no fetch made |
| evidence_versions_are_immutable_and_scoped_to_channel_and_release | LIVE | KEEP | killed M12 |
| legacy_summary_schema_upgrades_without_discarding_a_written_result | LIVE | KEEP | migration test on a real old schema |

### tests/test_summary_policy.py (14)

| test | live or opt-in | verdict | reason |
|---|---|---|---|
| default_writer_and_comparison_follow_the_selected_models | LIVE/OFFLINE | KEEP | pins Ember low as the live model (`(EMBER.model,"low") == (MODEL, EFFORT)` is near-tautological, the useful part is the model name) |
| yoy_only_uses_the_same_fiscal_period_and_metric_identity | DEAD | DELETE | tests `yoy`, which has no caller; killed M20 but the feature is unused |
| close_values_do_not_override_incompatible_context (6) | DEAD | DELETE | `yoy` |
| unsafe_growth_is_left_missing (4) | DEAD | DELETE | `yoy`; killed M19 |
| unknown_currency_and_basis_cannot_support_computed_growth | DEAD | DELETE | `yoy` |
| annuals_compare_without_a_quarter_but_unlabeled_quarters_do_not | DEAD | DELETE | `yoy` |
| margin_change_is_bps_and_does_not_match_profit_amount | DEAD | DELETE | `yoy`; name also claims a profit-amount mismatch it never checks |
| cash_priority_handles_burners_without_capex_and_unknown_data | DEAD | DELETE | `cash_priority`; killed M21 |
| equal_sized_businesses_and_accounting_bases_remain_distinct | DEAD | DELETE | `SummaryFact.identity` has no caller |
| source_check_rejects_empty_or_partially_matching_quotes | OFFLINE | KEEP | `evidence_issues` feeds the offline replay |
| guidance_never_infers_fiscal_period_from_a_year_or_dollar_value | OFFLINE | KEEP | |
| an_update_requires_status_or_detail_even_when_its_title_is_long | OFFLINE | KEEP | |
| unknown_change_reference_is_flagged_without_guessing_yoy | OFFLINE | KEEP | |
| quarter_and_full_year_in_one_fact_are_flagged_but_not_deleted | OFFLINE | KEEP | |

Verdict for the DELETE rows is "delete together with the dead code in summary_policy.py", or keep both if the owner plans to wire `yoy`; docs/summaries.md describes them as intended behavior.

### tests/test_summary_review.py (5) — UNWIRED (no caller outside docs)

| test | live or opt-in | verdict | reason |
|---|---|---|---|
| request_variants_preserve_default_and_add_only_opted_in_review | UNWIRED | KEEP | killed M25; non-mutation check included |
| compact_requests_preserve_original_coordinates_and_all_nonempty_evidence | UNWIRED | KEEP | actually tests `referenced_request` compaction (OPT-IN); belongs in test_summary_evidence |
| repair_is_triggered_by_code_rejection_of_a_headline_or_guide (3) | UNWIRED | KEEP | killed M24 |
| malformed_answers_do_not_blindly_start_another_paid_request (4) | UNWIRED | KEEP | |
| an_accepted_headline_needs_no_repair_request | UNWIRED | KEEP | |

Module-level flag: if summary_review.py is an abandoned experiment, these five go with it.

### tests/test_summary_request.py (2) — LIVE

| test | live or opt-in | verdict | reason |
|---|---|---|---|
| shared_request_is_ember_only_with_schema_and_no_word_cap | LIVE | KEEP | pins `max_tokens == 16000` and a prompt substring; acceptable policy pin |
| no_silent_input_truncation (3) | LIVE | KEEP | killed M22; `pytest.raises(ValueError)` has no `match` |

## Verdict counts (measured, 173 test functions)

KEEP 153, STRENGTHEN 5, MERGE 3, REWRITE 3, DELETE 9.
- STRENGTHEN: binding wrong_row loop; evidence request test; validation unknown_quote loop; driver_profiles incomplete-profile; driver_profile reservations.
- MERGE: binding two_reproduced_semantic_holes; format negative_rounded (into units_and_ranges); format one_quarter_has_one_label (into fiscal_periods).
- REWRITE: bound_format window-ending test (wrong module); evaluation future_requests_share_policy; evaluation future_requests_never_silently_truncate.
- DELETE: evidence existing_references_are_explicitly_not_a_metric_or_period_correctness_check; policy yoy/cash_priority/identity tests (8 functions, dead code).

## Mutation checks (measured; every mutation reverted, final `git status` clean)

| id | test(s) run | mutation | result |
|---|---|---|---|
| M1 | binding each_basis_in_one_sentence | drop `adjusted not in stated` basis rule | KILLED |
| M2 | binding growth_for_a_different_accounting_basis | drop growth period/basis pair issue | KILLED |
| M3 | binding noncomparable_prior_quarter | drop prior-year comparability issue | KILLED |
| M4 | binding wrong_row... | `_identity_issues` row mismatch returns [] | KILLED |
| M5 | binding wrong_row... | drop currency conflict | KILLED |
| M6 | referenced_writer opt-in | SDK retries 0 even when references off | KILLED |
| M7 | referenced_writer failures | remove `stop_reason != end_turn` check | KILLED (max_tokens case) |
| M8 | driver_profiles | remove `stop == end_turn` check | **SURVIVED** (stub answer `{}` fails parsing anyway) |
| M9 | driver_profile | `supported()` always True | KILLED |
| M10 | driver_profile store | `INSERT OR IGNORE` -> `OR REPLACE` in claim | KILLED |
| M11 | driver_profiles | remove first paused re-check after retrieval | SURVIVED (equivalent mutant: the lock-guard recheck below does the same job) |
| M12 | source_document_flow | evidence `INSERT OR IGNORE` -> `OR REPLACE` | KILLED |
| M13 | source_document | span clamp 1000 -> 100000 (text.py) | KILLED |
| M14 | validation (whole file) | drop "missing fiscal period" rule | **SURVIVED** (another rule shadows it; no test isolates it) |
| M15 | validation | drop count-has-currency rule | KILLED |
| M16 | validation + summary | exact-source-passage check relaxed | KILLED |
| M17 | format | accounting parenthesis no longer negative | KILLED |
| M18 | summary | skip `validate_facts` (publish every fact) | KILLED |
| M19 | policy | `yoy` drop `prior <= 0` guard | KILLED (dead code) |
| M20 | policy | `yoy` drop same-quarter guard | KILLED (dead code) |
| M21 | policy | cash_priority threshold 0.15 -> 0.9 | KILLED (dead code) |
| M22 | request | drop 200,000-char limit | KILLED |
| M23 | evidence | drop nested-outer-table guard | KILLED |
| M24 | review | repair for all roles, not just headline/guidance | KILLED |
| M25 | review | examples never injected | KILLED |
| M26 | bound_format | allow growth from non-positive prior | KILLED |
| M27 | evaluation | drop adjusted-mismatch rule | KILLED |
| M28 | evaluation | drop segment-not-company-revenue rule | KILLED |
| M29 | driver_profile | 200,000 -> 2,000,000 limit | KILLED |
| M30 | driver_profiles | drop 120-day stale-release guard | **SURVIVED** (see M35) |
| M31/M38 | bound_format | `+` sign for non-exact qualifiers | **SURVIVED** |
| M32 | summary | legacy request ignores advisory context | KILLED |
| M33 | summary | `summarizer_from` ignores `live_summaries` | KILLED |
| M34 | referenced_writer | `write_document` always legacy | SURVIVED (equivalent mutant: `write()` re-checks `references` itself, so the delegation is redundant) |
| M35 | driver_profiles + driver_profile | same as M30, run on both files | **SURVIVED**: no test that an old release makes no paid call (`parse_profile` also rejects stale data, but only after money is spent) |

Real survivors: M8, M14, M30/M35, M38. Equivalent mutants (not test gaps): M11, M34.

## Redundancy groups

1. test_summary_format: `units_and_ranges` + `negative_rounded_and_approximate_amounts` (both `format_value`, one duplicated row); `fiscal_periods` + `one_quarter_has_one_label` (both `format_period`); `basis_is_preserved` + `long_or_repeated_basis_notes` + `default_bases_and_consolidated_scopes` (three basis-label tables).
2. test_summary_binding: `two_reproduced_semantic_holes` repeats the wrong-metric/wrong-period classes of the first test and `a_number_from_a_different_metric`.
3. Uncertain-paid-call-not-repeated is asserted at two layers (store restart, orchestrator cancellation): keep both, different failures.
4. test_summary_review::compact_requests... tests `referenced_request` and belongs next to test_summary_evidence::request_preserves_geometry...

## Slow tests (measured; everything else under 0.2 s)

- test_summary_evaluation::future_requests_share_policy_schema_and_default_pair, 6.30 s: runs `scripts/summary_prepare.py` twice as a subprocess with cold imports.
- test_summary_evaluation::future_requests_never_silently_truncate_a_release, 3.08 s: subprocess for one ValueError.
- test_summary_evaluation::offline_cli_uses_default_pair_and_code_baseline_and_writes_blind_key, 0.18 s (acceptable, a genuine CLI smoke test).
Together these are about 9.5 s of the 14.9 s total.

## Untested important behavior (coverage run, measured per-module)

Per-module line coverage from these 15 files: summary_binding 96%, summary_evidence 100%, driver_profile 100%, source_document 99%, summary_format 98%, summary_validation 92%, driver_profiles 81%, summary 83%.
1. summary.py `_generate`: `anthropic.APIStatusError` -> SummaryError and `APIConnectionError` -> SummaryError (lines ~137-141) have no test; the live path's failure handling for a paid call.
2. driver_profiles.latest_profile_source (lines 75-86): every test monkeypatches it away, so source selection (6-K vs 10-K history, `published <= now`, `is_results_release`, `reported_period`) is untested, as is `ProfileWriter.__call__` (lines 50-55).
3. driver_profiles.ensure: the "do not pay to profile a release older than 120 days" guard (M35 survived), the no-source note ("Awaiting a recent verified results release") and the generic-exception path (lines 122-123, 168-170).
4. summary_validation.validate_facts: the `InvalidOperation/ValueError` -> "ambiguous numeric or table binding" branch and part of the optional-field salvage list (lines 276-291).
5. summary_bound_format: sign handling for qualified (non-exact) change values (line ~52, M38).

## Suite-wide practice issues

- Cross-test-module imports as shared helpers: `from tests.test_summary_evidence import fact, value` (binding, bound_format, referenced_writer), `from tests.test_summary_binding import TABLE`, `from tests.test_summary import summarizer_answering`, `from tests.test_pipeline import ...`, `from tests.test_driver_profile import SOURCE, TODAY, content, parsed`. Move to conftest.py/tests/builders.py; importing a test module from another couples collection and hides the fixtures.
- Inline imports inside tests (`from earnings_bot.binding_text import duration`, `from tests.test_driver_profile import parsed`).
- test_summary_binding repeats a 6-line evidence-reference dict (`{"document_sha256": ..., "block_id": ..., "cell_ids": [...], "quote": ...}`) plus `parse_referenced_brief(json.dumps({"schema_version": ...}))` about 10 times, while a helper (`report`/`bound`) already exists for the common case. Extract one `ref_for(block)` and `candidate_json(fact)`.
- For-loops of cases inside one test (binding wrong_row, group attribution, group_naming_issuer; validation unknown_quote) hide which case failed; parametrize or `pytest.param(id=...)`.
- `pytest.raises(ValueError)` without `match=` in evidence malformed-numeric, request truncation and a few others: any ValueError passes.
- Private-state reach-ins: `release._text.read_document`, `profiles.checked`, `profiles.notes`, `store.db.execute("SELECT ...")`, monkeypatching `DateDiscovery._check/_prepare_cache`; acceptable for orchestration tests but couples to internals.
- Subprocess tests with cwd-relative `scripts/...` paths and `sys.executable` (evaluation file): cwd-dependent, slow; call the functions in-process.
- Magic constants mirroring production limits (250_000, 5, 16000, 4000 * 1.5) instead of importing them.
- Prompt-text assertions (`"no editorial word or line limit" in system`, `"EPS is optional" in system`, `"check selection internally"`) are snapshots of wording rather than behavior; keep few, and only for rules that must not be reworded away.
- Determinism: no real sleeps, no real network, no platform skips, no `datetime.now()` in these files; timeouts via `asyncio.wait_for(..., 1)` only. `DeliveryStore` writes use the wall clock internally but no test asserts on it.
- Naming: test_driver_profile(s).py and the `test_source_document` / `test_source_document_flow` pair are easy to mix up; name by layer.
- Dead production code carries its own tests (`yoy`, `cash_priority`, `MetricKey`, `Observation`, `SummaryFact.identity`, summary_review): decide wire-or-delete and let the tests follow.
