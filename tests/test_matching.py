"""Evaluation matching. SPEC Sections 12.1 and 12.2."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.detector.cusum import Alert
from src.eval.matching import (
    FailureTag,
    load_min_score,
    match_incidents,
    pair_score,
)
from src.incident import AlertContext, Incident
from src.schema import TrueEpisode

ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)
WINDOW = timedelta(minutes=5)


def _incident(
    incident_id: str,
    start: int,
    end: int,
    cells: set[str],
    source: str = "gateway",
) -> Incident:
    members = []
    for index, key in enumerate(sorted(cells)):
        issuer_key, method = key.split("|")
        issuer = issuer_key.removeprefix("issuer:")
        alert = Alert(index, start, end, 10.0)
        members.append(AlertContext(alert, issuer, method, source))
    return Incident(incident_id, start, end, members)


def _truth(
    episode_id: str,
    start: int,
    end_exclusive: int,
    cells: set[str],
    mechanism: str = "psp_degradation",
) -> TrueEpisode:
    return TrueEpisode(
        episode_id=episode_id,
        mechanism=mechanism,
        start=ORIGIN + start * WINDOW,
        end=ORIGIN + end_exclusive * WINDOW,
        # This deliberately disagrees with the cells so a passing score proves
        # the matcher uses onset_offsets rather than causal-node labels.
        affected_nodes=["psp:NOT_THE_CELL_FOOTPRINT"],
        affected_methods=sorted({key.split("|")[1] for key in cells}),
        severity=1.0,
        primary_cell=min(cells) if cells else None,
        onset_arm="simultaneous",
        onset_offsets={key: 0 for key in cells},
        coupling_beta=0.0,
    )


def _match(predictions, truths, **kwargs):
    return match_incidents(
        predictions,
        truths,
        window_origin=ORIGIN,
        **kwargs,
    )


def test_perfect_match_scores_one():
    cells = {"issuer:ISSUER_A|card", "issuer:ISSUER_B|card"}
    predicted = _incident("p", 10, 19, cells)
    truth = _truth("t", 10, 20, cells)

    result = _match([predicted], [truth])

    assert len(result.matched_pairs) == 1
    assert result.matched_pairs[0].score == pytest.approx(1.0)
    assert result.false_positives == []
    assert result.false_negatives == []


def test_disjoint_time_windows_score_zero():
    cells = {"issuer:ISSUER_A|card"}
    predicted = _incident("p", 0, 9, cells)
    truth = _truth("t", 10, 20, cells)

    assert pair_score(predicted, truth, window_origin=ORIGIN) == 0.0


def test_partial_overlap_matches_hand_computed_iou():
    cells = {"issuer:ISSUER_A|card"}
    predicted = _incident("p", 10, 19, cells)  # [10, 20)
    truth = _truth("t", 15, 25, cells)         # [15, 25)

    # Five intersecting windows over fifteen in the union gives 5/15 = 1/3.
    assert pair_score(predicted, truth, window_origin=ORIGIN) == pytest.approx(1 / 3)


def test_configured_threshold_excludes_a_weak_pair():
    predicted = _incident(
        "p",
        0,
        9,
        {"issuer:ISSUER_A|card"},
    )
    truth = _truth(
        "t",
        0,
        10,
        {
            "issuer:ISSUER_A|card",
            "issuer:ISSUER_B|card",
            "issuer:ISSUER_C|card",
            "issuer:ISSUER_D|card",
        },
    )

    assert load_min_score() == pytest.approx(0.3)
    assert pair_score(predicted, truth, window_origin=ORIGIN) == pytest.approx(0.25)
    result = _match([predicted], [truth])
    assert result.matched_pairs == []
    assert result.false_positives == [predicted]
    assert result.false_negatives == [truth]


def test_hungarian_assignment_beats_greedy_pair_selection():
    """The highest single edge is 2/3, but taking it leaves only a zero edge.
    The two cross edges total 1.0, so the global solution must take both.
    """
    a = "issuer:ISSUER_A|card"
    b = "issuer:ISSUER_B|card"
    c = "issuer:ISSUER_C|card"
    d = "issuer:ISSUER_D|card"
    p1 = _incident("p1", 0, 9, {a, b})
    p2 = _incident("p2", 0, 9, {a, c, d})
    t1 = _truth("t1", 0, 10, {a, b, c})
    t2 = _truth("t2", 0, 10, {b})

    assert pair_score(p1, t1, window_origin=ORIGIN) == pytest.approx(2 / 3)
    result = _match([p1, p2], [t1, t2])
    assignments = {
        (pair.predicted.incident_id, pair.true_episode.episode_id)
        for pair in result.matched_pairs
    }
    assert assignments == {("p1", "t2"), ("p2", "t1")}
    assert sum(pair.score for pair in result.matched_pairs) == pytest.approx(1.0)


def test_split_is_one_match_plus_tagged_false_positive():
    cells = {"issuer:ISSUER_A|card"}
    exact = _incident("exact", 0, 9, cells)
    duplicate = _incident("duplicate", 1, 9, cells)
    truth = _truth("truth", 0, 10, cells)

    result = _match([exact, duplicate], [truth])

    assert [pair.predicted for pair in result.matched_pairs] == [exact]
    assert result.false_positives == [duplicate]
    assert result.false_negatives == []
    assert [(failure.tag, failure.predicted) for failure in result.failures] == [
        (FailureTag.SPLIT_ERROR, duplicate)
    ]


def test_merge_is_one_match_plus_tagged_false_negative():
    cells = {"issuer:ISSUER_A|card"}
    predicted = _incident("merged", 0, 9, cells)
    exact = _truth("exact", 0, 10, cells)
    second = _truth("second", 1, 10, cells)

    result = _match([predicted], [exact, second])

    assert [pair.true_episode for pair in result.matched_pairs] == [exact]
    assert result.false_positives == []
    assert result.false_negatives == [second]
    assert [(failure.tag, failure.true_episode) for failure in result.failures] == [
        (FailureTag.MERGE_ERROR, second)
    ]


def test_null_scenario_turns_every_prediction_into_a_false_positive():
    cells = {"issuer:ISSUER_A|card"}
    predictions = [
        _incident("p1", 0, 9, cells),
        _incident("p2", 20, 29, cells),
    ]
    null = _truth("none", 0, 0, set(), mechanism="none")

    result = _match(predictions, [null])

    assert result.matched_pairs == []
    assert result.false_positives == predictions
    assert result.false_negatives == []
    assert result.pair_debug == []
    assert [failure.tag for failure in result.failures] == [
        FailureTag.FALSE_POSITIVE,
        FailureTag.FALSE_POSITIVE,
    ]


def test_wrong_mechanism_family_still_matches_and_is_debug_only():
    cells = {"issuer:ISSUER_A|card"}
    predicted = _incident("p", 0, 9, cells, source="issuer_bank")
    truth = _truth("t", 0, 10, cells, mechanism="issuer_degradation")

    result = _match(
        [predicted],
        [truth],
        predicted_mechanisms={"p": "psp_degradation"},
    )

    assert len(result.matched_pairs) == 1
    assert result.matched_pairs[0].score == pytest.approx(1.0)
    assert result.matched_pairs[0].debug.mechanism_compatible is False
    assert result.pair_debug[0].mechanism_compatible is False
    assert result.false_positives == []
    assert result.false_negatives == []
