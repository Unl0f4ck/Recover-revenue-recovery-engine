"""Attribution-blind incident matching. SPEC Sections 12.1 and 12.2.

The primary score is temporal IoU multiplied by affected-cell Jaccard. A
predicted mechanism family is deliberately excluded: using diagnosis in the
assignment would turn a correctly detected incident with a wrong attribution
into one false positive plus one false negative. That would contaminate
detection recall with attribution quality and double-penalise one diagnostic
mistake. Mechanism-family compatibility is therefore retained only in
``PairDebug`` and is never read while scores or assignments are computed.

Incident bounds are inclusive integer window indices, while ledger bounds are
half-open datetimes. The conversion below maps ``start_window..end_window`` to
``[start_window, end_window + 1)`` before computing interval lengths.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

import numpy as np
import yaml
from scipy.optimize import linear_sum_assignment

from ..incident import MINUTES_PER_WINDOW, Incident
from ..schema import TrueEpisode

EVAL_CONFIG = Path(__file__).resolve().parents[2] / "config" / "eval.yaml"
WINDOW_SIZE = timedelta(minutes=MINUTES_PER_WINDOW)


class FailureTag(str, Enum):
    FALSE_POSITIVE = "FALSE_POSITIVE"
    FALSE_NEGATIVE = "FALSE_NEGATIVE"
    SPLIT_ERROR = "SPLIT_ERROR"
    MERGE_ERROR = "MERGE_ERROR"


@dataclass(frozen=True)
class PairDebug:
    predicted_id: str
    true_episode_id: str
    predicted_mechanism_family: str | None
    true_mechanism_family: str
    mechanism_compatible: bool | None


@dataclass(frozen=True)
class MatchedPair:
    predicted: Incident
    true_episode: TrueEpisode
    score: float
    debug: PairDebug


@dataclass(frozen=True)
class TaggedFailure:
    tag: FailureTag
    predicted: Incident | None
    true_episode: TrueEpisode | None
    score: float | None = None


@dataclass(frozen=True)
class MatchingResult:
    matched_pairs: list[MatchedPair]
    false_positives: list[Incident]
    false_negatives: list[TrueEpisode]
    failures: list[TaggedFailure]
    pair_debug: list[PairDebug]
    min_score: float


def load_min_score(config_path: str | Path = EVAL_CONFIG) -> float:
    """Read and validate the pre-registered matching threshold."""
    config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    try:
        value = float(config["matching"]["min_score"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("eval config requires numeric matching.min_score") from exc
    if not 0.0 < value <= 1.0:
        raise ValueError("matching.min_score must be in (0, 1]")
    return value


def _duration(start: float | datetime, end: float | datetime) -> float:
    delta = end - start
    if isinstance(delta, timedelta):
        return delta.total_seconds()
    return float(delta)


def temporal_iou(
    first_start: float | datetime,
    first_end: float | datetime,
    second_start: float | datetime,
    second_end: float | datetime,
) -> float:
    """Intersection-over-union for two half-open time intervals."""
    if _duration(first_start, first_end) < 0.0:
        raise ValueError("first interval ends before it starts")
    if _duration(second_start, second_end) < 0.0:
        raise ValueError("second interval ends before it starts")

    intersection = max(
        0.0,
        _duration(max(first_start, second_start), min(first_end, second_end)),
    )
    union = (
        _duration(first_start, first_end)
        + _duration(second_start, second_end)
        - intersection
    )
    return intersection / union if union > 0.0 else 0.0


def topology_jaccard(
    predicted_cells: Iterable[str], true_cells: Iterable[str]
) -> float:
    """Jaccard overlap between affected cell-key sets."""
    predicted_set = set(predicted_cells)
    true_set = set(true_cells)
    union = predicted_set | true_set
    return len(predicted_set & true_set) / len(union) if union else 0.0


def _default_window_origin() -> datetime:
    # The stream owns the index-to-time mapping, so importing its epoch avoids
    # a second epoch constant that could silently drift from generated ledgers.
    from ..simulator.generate import EPOCH

    return EPOCH


def pair_score(
    predicted: Incident,
    true_episode: TrueEpisode,
    *,
    window_origin: datetime | None = None,
    window_size: timedelta = WINDOW_SIZE,
) -> float:
    """Score one candidate using time and affected cell keys only."""
    if predicted.end_window < predicted.start_window:
        raise ValueError("incident end_window precedes start_window")
    if window_size <= timedelta(0):
        raise ValueError("window_size must be positive")

    origin = window_origin if window_origin is not None else _default_window_origin()
    predicted_start = origin + predicted.start_window * window_size
    predicted_end = origin + (predicted.end_window + 1) * window_size
    time_overlap = temporal_iou(
        predicted_start,
        predicted_end,
        true_episode.start,
        true_episode.end,
    )
    # affected_nodes describes causal topology labels, not the evaluated cell
    # footprint; using it would compare unlike identifier spaces.
    topology_overlap = topology_jaccard(
        predicted.cells, true_episode.onset_offsets.keys()
    )
    return time_overlap * topology_overlap


def _pair_debug(
    predicted: Incident,
    true_episode: TrueEpisode,
    predicted_mechanisms: Mapping[str, str | None] | None,
) -> PairDebug:
    predicted_family = (
        predicted_mechanisms.get(predicted.incident_id)
        if predicted_mechanisms is not None
        else getattr(predicted, "mechanism_family", None)
    )
    compatible = (
        None
        if predicted_family is None
        else predicted_family == true_episode.mechanism
    )
    return PairDebug(
        predicted_id=predicted.incident_id,
        true_episode_id=true_episode.episode_id,
        predicted_mechanism_family=predicted_family,
        true_mechanism_family=true_episode.mechanism,
        mechanism_compatible=compatible,
    )


def _null_result(predicted: list[Incident], min_score: float) -> MatchingResult:
    # A ledger's sole "none" row marks scenario membership; it is not a
    # zero-duration episode against which an alert may earn a match.
    failures = [
        TaggedFailure(FailureTag.FALSE_POSITIVE, incident, None)
        for incident in predicted
    ]
    return MatchingResult(
        matched_pairs=[],
        false_positives=list(predicted),
        false_negatives=[],
        failures=failures,
        pair_debug=[],
        min_score=min_score,
    )


def match_incidents(
    predicted: Iterable[Incident],
    true_episodes: Iterable[TrueEpisode],
    *,
    predicted_mechanisms: Mapping[str, str | None] | None = None,
    window_origin: datetime | None = None,
    window_size: timedelta = WINDOW_SIZE,
    config_path: str | Path = EVAL_CONFIG,
) -> MatchingResult:
    """Globally match predicted incidents to non-null ledger episodes."""
    predictions = list(predicted)
    truths = list(true_episodes)
    min_score = load_min_score(config_path)

    null_rows = [episode for episode in truths if episode.mechanism == "none"]
    if null_rows:
        if len(truths) != 1:
            raise ValueError("a null ledger row cannot coexist with true episodes")
        return _null_result(predictions, min_score)

    if not predictions or not truths:
        false_positives = list(predictions)
        false_negatives = list(truths)
        failures = [
            TaggedFailure(FailureTag.FALSE_POSITIVE, incident, None)
            for incident in false_positives
        ] + [
            TaggedFailure(FailureTag.FALSE_NEGATIVE, None, episode)
            for episode in false_negatives
        ]
        return MatchingResult(
            matched_pairs=[],
            false_positives=false_positives,
            false_negatives=false_negatives,
            failures=failures,
            pair_debug=[],
            min_score=min_score,
        )

    origin = window_origin if window_origin is not None else _default_window_origin()
    scores = np.array(
        [
            [
                pair_score(
                    incident,
                    episode,
                    window_origin=origin,
                    window_size=window_size,
                )
                for episode in truths
            ]
            for incident in predictions
        ],
        dtype=float,
    )

    # Ineligible edges receive zero weight so they cannot displace a stronger
    # valid edge merely because a rectangular assignment must fill every row
    # or column. Zero-weight assignments are discarded below.
    eligible_scores = np.where(scores >= min_score, scores, 0.0)
    row_indices, column_indices = linear_sum_assignment(
        eligible_scores, maximize=True
    )
    accepted = [
        (int(i), int(j))
        for i, j in zip(row_indices, column_indices)
        if scores[i, j] >= min_score
    ]
    accepted.sort()

    debug_by_index = {
        (i, j): _pair_debug(incident, episode, predicted_mechanisms)
        for i, incident in enumerate(predictions)
        for j, episode in enumerate(truths)
    }
    pair_debug = [
        debug_by_index[(i, j)]
        for i in range(len(predictions))
        for j in range(len(truths))
    ]
    matched_pairs = [
        MatchedPair(
            predicted=predictions[i],
            true_episode=truths[j],
            score=float(scores[i, j]),
            debug=debug_by_index[(i, j)],
        )
        for i, j in accepted
    ]

    matched_prediction_indices = {i for i, _ in accepted}
    matched_truth_indices = {j for _, j in accepted}
    unmatched_prediction_indices = [
        i for i in range(len(predictions)) if i not in matched_prediction_indices
    ]
    unmatched_truth_indices = [
        j for j in range(len(truths)) if j not in matched_truth_indices
    ]
    false_positives = [predictions[i] for i in unmatched_prediction_indices]
    false_negatives = [truths[j] for j in unmatched_truth_indices]

    failures: list[TaggedFailure] = []
    for i in unmatched_prediction_indices:
        split_candidates = [
            j for j in matched_truth_indices if scores[i, j] >= min_score
        ]
        if split_candidates:
            j = max(split_candidates, key=lambda candidate: scores[i, candidate])
            failures.append(
                TaggedFailure(
                    FailureTag.SPLIT_ERROR,
                    predictions[i],
                    truths[j],
                    float(scores[i, j]),
                )
            )
        else:
            failures.append(
                TaggedFailure(FailureTag.FALSE_POSITIVE, predictions[i], None)
            )

    for j in unmatched_truth_indices:
        merge_candidates = [
            i for i in matched_prediction_indices if scores[i, j] >= min_score
        ]
        if merge_candidates:
            i = max(merge_candidates, key=lambda candidate: scores[candidate, j])
            failures.append(
                TaggedFailure(
                    FailureTag.MERGE_ERROR,
                    predictions[i],
                    truths[j],
                    float(scores[i, j]),
                )
            )
        else:
            failures.append(
                TaggedFailure(FailureTag.FALSE_NEGATIVE, None, truths[j])
            )

    return MatchingResult(
        matched_pairs=matched_pairs,
        false_positives=false_positives,
        false_negatives=false_negatives,
        failures=failures,
        pair_debug=pair_debug,
        min_score=min_score,
    )
