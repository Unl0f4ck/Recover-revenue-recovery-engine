"""Evaluation metrics. SPEC Sections 12.2 and 12.5."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from src.audit import AuditRecord
from src.eval.matching import (
    FailureTag,
    MatchedPair,
    MatchingResult,
    PairDebug,
    TaggedFailure,
)
from src.eval.metrics import (
    AccuracyAtCoverage,
    abstention_metrics,
    attribution_at_coverage,
    by_onset_arm,
    detection_metrics,
    failure_list,
    false_intervention_metrics,
    l3_metrics,
    split_merge_counts,
)
from src.incident import Incident
from src.schema import Action, Diagnosis, TrueEpisode

ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _incident(incident_id: str) -> Incident:
    return Incident(incident_id, 0, 1)


def _truth(
    episode_id: str,
    mechanism: str = "psp_degradation",
    onset_arm: str = "propagating",
    primary_cell: str | None = "issuer:ISSUER_A|card",
) -> TrueEpisode:
    return TrueEpisode(
        episode_id=episode_id,
        mechanism=mechanism,
        start=ORIGIN,
        end=ORIGIN + timedelta(minutes=10),
        affected_nodes=[] if mechanism == "none" else ["psp:PSP_1"],
        affected_methods=[] if mechanism == "none" else ["card"],
        severity=0.0 if mechanism == "none" else 1.0,
        primary_cell=None if mechanism == "none" else primary_cell,
        onset_arm=onset_arm,
        onset_offsets={},
        coupling_beta=0.0,
    )


def _pair(
    incident_id: str,
    mechanism: str = "psp_degradation",
    onset_arm: str = "propagating",
    primary_cell: str | None = "issuer:ISSUER_A|card",
) -> MatchedPair:
    predicted = _incident(incident_id)
    truth = _truth(
        f"true-{incident_id}",
        mechanism=mechanism,
        onset_arm=onset_arm,
        primary_cell=primary_cell,
    )
    debug = PairDebug(
        predicted_id=incident_id,
        true_episode_id=truth.episode_id,
        predicted_mechanism_family=None,
        true_mechanism_family=mechanism,
        mechanism_compatible=None,
    )
    return MatchedPair(predicted, truth, 0.8, debug)


def _result(
    pairs: list[MatchedPair] | None = None,
    false_positives: list[Incident] | None = None,
    false_negatives: list[TrueEpisode] | None = None,
    failures: list[TaggedFailure] | None = None,
) -> MatchingResult:
    matched_pairs = list(pairs or [])
    return MatchingResult(
        matched_pairs=matched_pairs,
        false_positives=list(false_positives or []),
        false_negatives=list(false_negatives or []),
        failures=list(failures or []),
        pair_debug=[pair.debug for pair in matched_pairs],
        min_score=0.3,
    )


def _diagnosis(
    incident_id: str,
    family: str | None = "psp_degradation",
    cause_node: str | None = "psp:PSP_1",
    level_used: str = "L2",
    l3_eligible: bool = False,
    l3_abstain_reason: str | None = None,
) -> Diagnosis:
    return Diagnosis(
        incident_id=incident_id,
        cause_node=cause_node,
        mechanism_family=family,
        level_used=level_used,
        confidence=0.7 if cause_node is not None else 0.0,
        l3_eligible=l3_eligible,
        l3_abstain_reason=l3_abstain_reason,
    )


def _audit(scenario_id: str, action: str | Action) -> AuditRecord:
    return AuditRecord(
        scenario_id=scenario_id,
        incident_id=f"{scenario_id}-incident",
        detected_at=ORIGIN,
        cells=[],
        dominant_source="gateway",
        dominant_step=None,
        level_used="L2",
        cause_node=None,
        mechanism_family=None,
        confidence=0.0,
        l3_eligible=False,
        l3_abstain_reason=None,
        action_chosen=Action.NO_ACTION.value,
        action_executed=action,
        bounds_fired=[],
        escalated=False,
        execution_mode="SIMULATED",
        amount_at_risk_paise=0,
    )


def test_detection_metrics_use_hand_computable_tp_fp_fn_counts():
    pairs = [_pair("p1"), _pair("p2")]
    result = _result(
        pairs,
        false_positives=[_incident("fp")],
        false_negatives=[_truth("fn1"), _truth("fn2")],
    )

    metrics = detection_metrics(result)

    assert metrics.true_positives == 2
    assert metrics.false_positives == 1
    assert metrics.false_negatives == 2
    assert metrics.detection_recall == pytest.approx(1 / 2)
    assert metrics.false_alarm_rate == pytest.approx(1 / 3)
    assert metrics.incident_precision == pytest.approx(2 / 3)
    assert metrics.incident_recall == pytest.approx(1 / 2)


def test_accuracy_at_coverage_cannot_be_constructed_without_coverage():
    with pytest.raises(TypeError):
        AccuracyAtCoverage(
            correct_count=1,
            covered_count=1,
            applicable_count=1,
            accuracy=1.0,
            accuracy_times_coverage=1.0,
        )


def test_accuracy_at_coverage_distinguishes_low_and_high_coverage():
    result = _result([_pair(f"p{i}") for i in range(10)])
    low_diagnoses = {"p0": _diagnosis("p0")}
    high_diagnoses = {
        f"p{i}": _diagnosis(f"p{i}") for i in range(9)
    }

    low = attribution_at_coverage(result, low_diagnoses)
    high = attribution_at_coverage(result, high_diagnoses)

    assert low.accuracy == high.accuracy == 1.0
    assert low.coverage == pytest.approx(0.1)
    assert high.coverage == pytest.approx(0.9)
    assert low.accuracy_times_coverage == pytest.approx(0.1)
    assert high.accuracy_times_coverage == pytest.approx(0.9)


def test_ood_recognition_rewards_abstention_and_rejects_confident_naming():
    result = _result(
        [
            _pair("abstain", "ood_unknown"),
            _pair("confident", "ood_unknown"),
        ]
    )
    diagnoses = {
        "abstain": _diagnosis("abstain", None, None, "ABSTAIN"),
        "confident": _diagnosis("confident", "psp_degradation"),
    }

    metrics = abstention_metrics(result, diagnoses)

    assert metrics.ood_recognized_count == 1
    assert metrics.ood_episode_count == 2
    assert metrics.ood_recognition_rate == pytest.approx(0.5)


def test_ood_denominator_excludes_known_episodes():
    result = _result(
        [_pair("ood", "ood_novel")]
        + [_pair(f"known-{i}") for i in range(4)]
    )
    diagnoses = {"ood": _diagnosis("ood", None, None, "ABSTAIN")}

    metrics = abstention_metrics(result, diagnoses)

    assert metrics.ood_recognized_count == 1
    assert metrics.ood_matched_count == 1
    assert metrics.ood_episode_count == 1
    assert metrics.ood_recognition_rate == 1.0


def test_ood_denominator_includes_undetected_ood_without_recognition_credit():
    result = _result(
        [_pair("seen", "ood_novel")],
        false_negatives=[_truth("missed", "ood_novel")],
    )
    diagnoses = {"seen": _diagnosis("seen", None, None, "ABSTAIN")}

    metrics = abstention_metrics(result, diagnoses)

    assert metrics.ood_recognized_count == 1
    assert metrics.ood_matched_count == 1
    assert metrics.ood_episode_count == 2
    assert metrics.ood_recognition_rate == pytest.approx(0.5)


def test_known_class_abstention_has_its_own_denominator():
    result = _result(
        [_pair("known-1"), _pair("known-2"), _pair("ood", "ood_novel")]
    )
    diagnoses = {
        "known-1": _diagnosis("known-1", None, None, "ABSTAIN"),
        "known-2": _diagnosis("known-2"),
        "ood": _diagnosis("ood", None, None, "ABSTAIN"),
    }

    metrics = abstention_metrics(result, diagnoses)

    assert metrics.known_abstention_count == 1
    assert metrics.known_episode_count == 2
    assert metrics.known_class_abstention_rate == pytest.approx(0.5)


def test_false_intervention_rate_is_zero_when_nulls_get_no_action():
    ledgers = {
        "null-1": [_truth("null-1-none", "none", "simultaneous")],
        "null-2": [_truth("null-2-none", "none", "simultaneous")],
        "known": [_truth("known-episode")],
    }
    audits = [
        _audit("null-1", Action.NO_ACTION),
        _audit("null-2", Action.NO_ACTION.value),
        _audit("known", Action.SWITCH_PSP.value),
    ]

    metrics = false_intervention_metrics(ledgers, audits)

    assert metrics.false_intervention_count == 0
    assert metrics.null_scenario_count == 2
    assert metrics.false_intervention_rate == 0.0


def test_false_intervention_rate_counts_null_scenarios_not_actions():
    ledgers = {
        "null-1": [_truth("null-1-none", "none", "simultaneous")],
        "null-2": [_truth("null-2-none", "none", "simultaneous")],
    }
    audits = [
        _audit("null-1", Action.SWITCH_PSP.value),
        _audit("null-1", Action.BACKOFF_REPRESENT.value),
        _audit("null-2", Action.NO_ACTION.value),
    ]

    metrics = false_intervention_metrics(ledgers, audits)

    assert metrics.false_intervention_count == 1
    assert metrics.null_scenario_count == 2
    assert metrics.false_intervention_rate == pytest.approx(0.5)


def test_l3_metrics_split_onset_arms_without_pooling():
    propagating = _pair(
        "propagating",
        onset_arm="propagating",
        primary_cell="issuer:ISSUER_A|card",
    )
    simultaneous = _pair("simultaneous", onset_arm="simultaneous")
    result = _result([propagating, simultaneous])
    diagnoses = {
        "propagating": _diagnosis(
            "propagating",
            cause_node="issuer:ISSUER_A|card",
            level_used="L3",
            l3_eligible=True,
        ),
        "simultaneous": _diagnosis(
            "simultaneous",
            level_used="L2",
            l3_eligible=True,
            l3_abstain_reason="no stable edges",
        ),
    }

    by_arm = l3_metrics(result, diagnoses)

    assert list(by_arm) == ["propagating", "simultaneous"]
    assert by_arm["propagating"].eligibility_denominator == 1
    assert by_arm["simultaneous"].eligibility_denominator == 1
    assert by_arm["propagating"].accuracy_at_coverage.accuracy == 1.0
    assert by_arm["propagating"].accuracy_at_coverage.coverage == 1.0
    assert by_arm["propagating"].abstention_rate == 0.0
    assert by_arm["simultaneous"].accuracy_at_coverage.coverage == 0.0
    assert by_arm["simultaneous"].abstention_rate == 1.0
    assert by_arm["simultaneous"].abstention_reasons == {
        "no stable edges": 1
    }


def test_by_onset_arm_always_returns_both_independent_partitions():
    result = _result(
        [
            _pair("p1", onset_arm="propagating"),
            _pair("s1", onset_arm="simultaneous"),
            _pair("s2", onset_arm="simultaneous"),
        ]
    )

    counts = by_onset_arm(result, lambda arm: len(arm.matched_pairs))

    assert counts == {"propagating": 1, "simultaneous": 2}


def test_l3_metrics_are_zero_and_empty_when_l3_is_not_present():
    result = _result([_pair("p", onset_arm="propagating")])
    diagnoses = {"p": _diagnosis("p", level_used="L2")}

    by_arm = l3_metrics(result, diagnoses)

    for metrics in by_arm.values():
        assert metrics.eligible_count == 0
        assert metrics.eligibility_rate == 0.0
        assert metrics.abstention_count == 0
        assert metrics.abstention_denominator == 0
        assert metrics.abstention_rate == 0.0
        assert metrics.abstention_reasons == {}
        assert metrics.accuracy_at_coverage.accuracy == 0.0
        assert metrics.accuracy_at_coverage.coverage == 0.0


def test_split_merge_counts_come_from_matcher_tags():
    incident = _incident("p")
    truth = _truth("t")
    failures = [
        TaggedFailure(FailureTag.SPLIT_ERROR, incident, truth, 0.5),
        TaggedFailure(FailureTag.MERGE_ERROR, incident, truth, 0.4),
        TaggedFailure(FailureTag.FALSE_POSITIVE, incident, None),
    ]

    counts = split_merge_counts(_result(failures=failures))

    assert counts.split_count == 1
    assert counts.merge_count == 1


def test_failure_list_names_every_required_category_and_is_json_safe():
    fp = _incident("fp")
    split = _incident("split")
    merged = _incident("merged")
    fn_truth = _truth("fn")
    split_truth = _truth("split-truth")
    merge_truth = _truth("merge-truth")
    wrong_pair = _pair("wrong")
    failures = [
        TaggedFailure(FailureTag.FALSE_POSITIVE, fp, None),
        TaggedFailure(FailureTag.FALSE_NEGATIVE, None, fn_truth),
        TaggedFailure(FailureTag.SPLIT_ERROR, split, split_truth, 0.6),
        TaggedFailure(FailureTag.MERGE_ERROR, merged, merge_truth, 0.5),
    ]
    result = _result(
        [wrong_pair],
        false_positives=[fp, split],
        false_negatives=[fn_truth, merge_truth],
        failures=failures,
    )
    diagnoses = {
        "wrong": _diagnosis("wrong", family="issuer_degradation")
    }

    records = failure_list(result, diagnoses)

    assert [record.category for record in records] == [
        "FP",
        "FN",
        "split",
        "merge",
        "wrong-family",
    ]
    assert records[0].predicted["incident_id"] == "fp"
    assert records[0].true is None
    assert records[1].predicted is None
    assert records[1].true["episode_id"] == "fn"
    assert records[-1].predicted["diagnosis"]["mechanism_family"] == (
        "issuer_degradation"
    )
    assert records[-1].true["mechanism"] == "psp_degradation"
    json.dumps([asdict(record) for record in records])
