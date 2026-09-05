"""Evaluation metrics. SPEC Sections 12.2 and 12.5.

All incident metrics consume the attribution-blind ``MatchingResult`` rather
than matching again. Detection quality therefore cannot change when a
diagnosis changes, and attribution errors remain one attribution miss instead
of becoming both a false positive and a false negative.

Accuracy is coupled to coverage in one result type. Conditional accuracy on
its own rewards abstention, so every such result also carries the count-based
coverage and their product. L3 results are returned only by onset arm: the
public API has no pooled L3 summary that could hide the expected difference
between propagating and simultaneous episodes.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from math import isclose
from typing import TYPE_CHECKING, TypeVar

from ..schema import Action, Diagnosis, TrueEpisode
from .matching import (
    FailureTag,
    MatchedPair,
    MatchingResult,
    TaggedFailure,
)

if TYPE_CHECKING:
    from ..audit import AuditRecord


ONSET_ARMS = ("propagating", "simultaneous")
MetricT = TypeVar("MetricT")
DiagnosisInput = Mapping[str, Diagnosis] | Iterable[Diagnosis]


@dataclass(frozen=True)
class DetectionMetrics:
    true_positives: int
    false_positives: int
    false_negatives: int
    predicted_incidents: int
    true_incidents: int
    detection_recall: float
    false_alarm_rate: float
    incident_precision: float
    incident_recall: float


@dataclass(frozen=True)
class AccuracyAtCoverage:
    """Conditional accuracy, coverage, and effective accuracy as one value."""

    correct_count: int
    covered_count: int
    applicable_count: int
    accuracy: float
    coverage: float
    accuracy_times_coverage: float

    def __post_init__(self) -> None:
        if not 0 <= self.correct_count <= self.covered_count:
            raise ValueError("correct_count must be between 0 and covered_count")
        if not 0 <= self.covered_count <= self.applicable_count:
            raise ValueError("covered_count must be between 0 and applicable_count")
        expected_accuracy = (
            self.correct_count / self.covered_count
            if self.covered_count
            else 0.0
        )
        expected_coverage = (
            self.covered_count / self.applicable_count
            if self.applicable_count
            else 0.0
        )
        if not isclose(self.accuracy, expected_accuracy, abs_tol=1e-12):
            raise ValueError("accuracy must equal correct_count / covered_count")
        if not isclose(self.coverage, expected_coverage, abs_tol=1e-12):
            raise ValueError("coverage must equal covered_count / applicable_count")
        if not isclose(
            self.accuracy_times_coverage,
            self.accuracy * self.coverage,
            abs_tol=1e-12,
        ):
            raise ValueError("accuracy_times_coverage must equal accuracy * coverage")


@dataclass(frozen=True)
class AbstentionMetrics:
    known_abstention_count: int
    known_episode_count: int
    known_class_abstention_rate: float
    ood_recognized_count: int
    ood_matched_count: int
    ood_episode_count: int
    ood_recognition_rate: float


@dataclass(frozen=True)
class FalseInterventionMetrics:
    false_intervention_count: int
    null_scenario_count: int
    false_intervention_rate: float


@dataclass(frozen=True)
class SplitMergeCounts:
    split_count: int
    merge_count: int


@dataclass(frozen=True)
class L3ArmMetrics:
    accuracy_at_coverage: AccuracyAtCoverage
    eligible_count: int
    eligibility_denominator: int
    eligibility_rate: float
    abstention_count: int
    abstention_denominator: int
    abstention_rate: float
    abstention_reasons: dict[str, int]


@dataclass(frozen=True)
class FailureRecord:
    category: str
    predicted: dict[str, object] | None
    true: dict[str, object] | None
    score: float | None


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _accuracy_at_coverage(
    correct_count: int,
    covered_count: int,
    applicable_count: int,
) -> AccuracyAtCoverage:
    accuracy = _rate(correct_count, covered_count)
    coverage = _rate(covered_count, applicable_count)
    return AccuracyAtCoverage(
        correct_count=correct_count,
        covered_count=covered_count,
        applicable_count=applicable_count,
        accuracy=accuracy,
        coverage=coverage,
        accuracy_times_coverage=accuracy * coverage,
    )


def _diagnosis_index(diagnoses: DiagnosisInput | None) -> dict[str, Diagnosis]:
    if diagnoses is None:
        return {}
    if isinstance(diagnoses, Mapping):
        return dict(diagnoses)
    return {diagnosis.incident_id: diagnosis for diagnosis in diagnoses}


def _diagnosis_for(
    pair: MatchedPair, diagnoses: Mapping[str, Diagnosis]
) -> Diagnosis | None:
    return diagnoses.get(pair.predicted.incident_id)


def _is_ood(episode: TrueEpisode) -> bool:
    return episode.mechanism.startswith("ood_")


def _attribution_applies(episode: TrueEpisode) -> bool:
    return episode.mechanism != "none" and not _is_ood(episode)


def _is_abstention(diagnosis: Diagnosis | None) -> bool:
    return diagnosis is None or diagnosis.cause_node is None


def detection_metrics(result: MatchingResult) -> DetectionMetrics:
    """Compute TP/FP/FN detection measures from one completed match."""
    true_positives = len(result.matched_pairs)
    false_positives = len(result.false_positives)
    false_negatives = len(result.false_negatives)
    predicted_incidents = true_positives + false_positives
    true_incidents = true_positives + false_negatives

    # Matching results contain no true-negative population. The reportable
    # false-alarm rate is therefore the fraction of emitted incidents that did
    # not match truth, which also makes its denominator auditable.
    recall = _rate(true_positives, true_incidents)
    precision = _rate(true_positives, predicted_incidents)
    return DetectionMetrics(
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        predicted_incidents=predicted_incidents,
        true_incidents=true_incidents,
        detection_recall=recall,
        false_alarm_rate=_rate(false_positives, predicted_incidents),
        incident_precision=precision,
        incident_recall=recall,
    )


def attribution_at_coverage(
    result: MatchingResult,
    diagnoses: DiagnosisInput,
) -> AccuracyAtCoverage:
    """Score known-family attribution on matched, non-abstained incidents."""
    diagnosis_by_id = _diagnosis_index(diagnoses)
    applicable_pairs = [
        pair
        for pair in result.matched_pairs
        if _attribution_applies(pair.true_episode)
    ]
    covered = [
        pair
        for pair in applicable_pairs
        if not _is_abstention(_diagnosis_for(pair, diagnosis_by_id))
    ]
    correct = sum(
        _diagnosis_for(pair, diagnosis_by_id).mechanism_family
        == pair.true_episode.mechanism
        for pair in covered
    )
    return _accuracy_at_coverage(correct, len(covered), len(applicable_pairs))


def abstention_metrics(
    result: MatchingResult,
    diagnoses: DiagnosisInput,
) -> AbstentionMetrics:
    """Report known abstention and OOD recognition with separate denominators."""
    diagnosis_by_id = _diagnosis_index(diagnoses)
    known_pairs = [
        pair
        for pair in result.matched_pairs
        if _attribution_applies(pair.true_episode)
    ]
    ood_pairs = [
        pair for pair in result.matched_pairs if _is_ood(pair.true_episode)
    ]
    known_abstentions = sum(
        _is_abstention(_diagnosis_for(pair, diagnosis_by_id))
        for pair in known_pairs
    )
    ood_false_negatives = [
        episode for episode in result.false_negatives if _is_ood(episode)
    ]
    # An absent diagnosis is not the explicit UNKNOWN decision required for
    # OOD recognition. Undetected OOD episodes stay in the denominator, so a
    # detector cannot improve recognition by failing to surface hard cases.
    ood_recognized = sum(
        (diagnosis := _diagnosis_for(pair, diagnosis_by_id)) is not None
        and diagnosis.cause_node is None
        for pair in ood_pairs
    )
    ood_episode_count = len(ood_pairs) + len(ood_false_negatives)
    return AbstentionMetrics(
        known_abstention_count=known_abstentions,
        known_episode_count=len(known_pairs),
        known_class_abstention_rate=_rate(known_abstentions, len(known_pairs)),
        ood_recognized_count=ood_recognized,
        ood_matched_count=len(ood_pairs),
        ood_episode_count=ood_episode_count,
        ood_recognition_rate=_rate(ood_recognized, ood_episode_count),
    )


def false_intervention_metrics(
    ledgers_by_scenario: Mapping[str, Iterable[TrueEpisode]],
    audit_records: Iterable[AuditRecord],
) -> FalseInterventionMetrics:
    """Count null scenarios with at least one executed intervention."""
    null_scenarios: set[str] = set()
    for scenario_id, ledger in ledgers_by_scenario.items():
        episodes = list(ledger)
        if any(episode.mechanism == "none" for episode in episodes):
            if len(episodes) != 1 or episodes[0].mechanism != "none":
                raise ValueError(
                    "a null ledger row cannot coexist with true episodes"
                )
            null_scenarios.add(scenario_id)

    intervened: set[str] = set()
    for record in audit_records:
        if record.scenario_id not in null_scenarios:
            continue
        action = getattr(record.action_executed, "value", record.action_executed)
        if action != Action.NO_ACTION.value:
            # The unit is scenarios, not actions: repeated actions in one null
            # scenario must not make the rate exceed one.
            intervened.add(record.scenario_id)

    return FalseInterventionMetrics(
        false_intervention_count=len(intervened),
        null_scenario_count=len(null_scenarios),
        false_intervention_rate=_rate(len(intervened), len(null_scenarios)),
    )


def split_merge_counts(result: MatchingResult) -> SplitMergeCounts:
    """Count split and merge errors from the matcher's authoritative tags."""
    tags = [_tag_value(failure) for failure in result.failures]
    return SplitMergeCounts(
        split_count=tags.count(FailureTag.SPLIT_ERROR.value),
        merge_count=tags.count(FailureTag.MERGE_ERROR.value),
    )


def _tag_value(failure: TaggedFailure) -> str:
    return getattr(failure.tag, "value", str(failure.tag))


def _matching_result_for_arm(
    result: MatchingResult, onset_arm: str
) -> MatchingResult:
    matched_pairs = [
        pair
        for pair in result.matched_pairs
        if pair.true_episode.onset_arm == onset_arm
    ]
    false_negatives = [
        episode
        for episode in result.false_negatives
        if episode.onset_arm == onset_arm
    ]
    failures = [
        failure
        for failure in result.failures
        if failure.true_episode is not None
        and failure.true_episode.onset_arm == onset_arm
    ]
    # Only split FPs have a true episode and can be assigned to an onset arm.
    # Standalone FPs are intentionally absent from every truth-conditioned arm.
    false_positives = [
        failure.predicted
        for failure in failures
        if _tag_value(failure) == FailureTag.SPLIT_ERROR.value
        and failure.predicted is not None
    ]
    true_ids = {
        pair.true_episode.episode_id for pair in matched_pairs
    } | {episode.episode_id for episode in false_negatives}
    pair_debug = [
        debug
        for debug in result.pair_debug
        if debug.true_episode_id in true_ids
    ]
    return MatchingResult(
        matched_pairs=matched_pairs,
        false_positives=false_positives,
        false_negatives=false_negatives,
        failures=failures,
        pair_debug=pair_debug,
        min_score=result.min_score,
    )


def by_onset_arm(
    result: MatchingResult,
    compute: Callable[[MatchingResult], MetricT],
) -> dict[str, MetricT]:
    """Apply ``compute`` independently to each truth-defined onset arm.

    Both keys are always present, including when an arm is empty. A standalone
    false positive has no true onset arm and is omitted; split errors retain
    the arm of the true episode attached by the matcher.
    """
    observed_arms = {
        pair.true_episode.onset_arm for pair in result.matched_pairs
    } | {episode.onset_arm for episode in result.false_negatives}
    unknown_arms = observed_arms - set(ONSET_ARMS)
    if unknown_arms:
        raise ValueError(f"unknown onset arm(s): {sorted(unknown_arms)}")
    return {
        arm: compute(_matching_result_for_arm(result, arm))
        for arm in ONSET_ARMS
    }


def _l3_arm_metrics(
    result: MatchingResult,
    diagnoses: Mapping[str, Diagnosis],
) -> L3ArmMetrics:
    pairs = [
        pair
        for pair in result.matched_pairs
        if _attribution_applies(pair.true_episode)
    ]
    eligible_count = 0
    considered_count = 0
    reasons: dict[str, int] = {}
    covered_count = 0
    correct_count = 0

    for pair in pairs:
        diagnosis = _diagnosis_for(pair, diagnoses)
        if diagnosis is None:
            continue
        eligible = bool(getattr(diagnosis, "l3_eligible", False))
        reason = getattr(diagnosis, "l3_abstain_reason", None)
        level_used = getattr(diagnosis, "level_used", "")
        eligible_count += int(eligible)

        # L1/L2 diagnoses with untouched L3 fields were never considered.
        # Excluding them makes a not-yet-implemented L3 report 0/0, not 100%
        # abstention merely because an L2 result exists.
        considered = level_used == "L3" or eligible or reason is not None
        considered_count += int(considered)
        if reason is not None:
            reasons[reason] = reasons.get(reason, 0) + 1

        if level_used == "L3" and diagnosis.cause_node is not None:
            covered_count += 1
            # L3 refines to an observed causal root, whose ledger target is the
            # primary cell rather than L2's topology-level affected node.
            correct_count += int(
                diagnosis.cause_node == pair.true_episode.primary_cell
            )

    abstention_count = sum(reasons.values())
    return L3ArmMetrics(
        accuracy_at_coverage=_accuracy_at_coverage(
            correct_count, covered_count, len(pairs)
        ),
        eligible_count=eligible_count,
        eligibility_denominator=len(pairs),
        eligibility_rate=_rate(eligible_count, len(pairs)),
        abstention_count=abstention_count,
        abstention_denominator=considered_count,
        abstention_rate=_rate(abstention_count, considered_count),
        abstention_reasons=reasons,
    )


def l3_metrics(
    result: MatchingResult,
    diagnoses: DiagnosisInput,
) -> dict[str, L3ArmMetrics]:
    """Return L3 accuracy, eligibility, and abstention only by onset arm."""
    diagnosis_by_id = _diagnosis_index(diagnoses)
    return by_onset_arm(
        result,
        lambda arm_result: _l3_arm_metrics(arm_result, diagnosis_by_id),
    )


def _incident_summary(
    incident: object | None,
    diagnosis: Diagnosis | None,
) -> dict[str, object] | None:
    if incident is None:
        return None
    diagnosis_summary = None
    if diagnosis is not None:
        diagnosis_summary = {
            "cause_node": diagnosis.cause_node,
            "mechanism_family": diagnosis.mechanism_family,
            "level_used": diagnosis.level_used,
            "confidence": diagnosis.confidence,
        }
    return {
        "incident_id": getattr(incident, "incident_id"),
        "start_window": getattr(incident, "start_window"),
        "end_window": getattr(incident, "end_window"),
        "cells": list(getattr(incident, "cells")),
        "diagnosis": diagnosis_summary,
    }


def _truth_summary(episode: TrueEpisode | None) -> dict[str, object] | None:
    if episode is None:
        return None
    return {
        "episode_id": episode.episode_id,
        "mechanism": episode.mechanism,
        "start": episode.start.isoformat(),
        "end": episode.end.isoformat(),
        "affected_nodes": list(episode.affected_nodes),
        "primary_cell": episode.primary_cell,
        "onset_arm": episode.onset_arm,
    }


def failure_list(
    result: MatchingResult,
    diagnoses: DiagnosisInput | None = None,
) -> list[FailureRecord]:
    """Return matcher and family-attribution failures with JSON-safe detail."""
    diagnosis_by_id = _diagnosis_index(diagnoses)
    categories = {
        FailureTag.FALSE_POSITIVE.value: "FP",
        FailureTag.FALSE_NEGATIVE.value: "FN",
        FailureTag.SPLIT_ERROR.value: "split",
        FailureTag.MERGE_ERROR.value: "merge",
    }
    records: list[FailureRecord] = []
    for failure in result.failures:
        predicted = failure.predicted
        diagnosis = (
            diagnosis_by_id.get(predicted.incident_id)
            if predicted is not None
            else None
        )
        records.append(
            FailureRecord(
                category=categories[_tag_value(failure)],
                predicted=_incident_summary(predicted, diagnosis),
                true=_truth_summary(failure.true_episode),
                score=failure.score,
            )
        )

    # Without diagnoses the matcher failures are still meaningful, but there
    # is no evidence from which to manufacture attribution failures.
    if diagnoses is None:
        return records

    for pair in result.matched_pairs:
        diagnosis = _diagnosis_for(pair, diagnosis_by_id)
        if _is_ood(pair.true_episode):
            family_miss = not _is_abstention(diagnosis)
        elif _attribution_applies(pair.true_episode):
            family_miss = (
                _is_abstention(diagnosis)
                or diagnosis.mechanism_family != pair.true_episode.mechanism
            )
        else:
            family_miss = False
        if family_miss:
            records.append(
                FailureRecord(
                    category="wrong-family",
                    predicted=_incident_summary(pair.predicted, diagnosis),
                    true=_truth_summary(pair.true_episode),
                    score=pair.score,
                )
            )
    return records
