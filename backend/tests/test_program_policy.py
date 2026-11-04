import pytest
from pydantic import ValidationError

from app.programs.policy import SourceInterval, TemporalPolicy, UnknownPolicy


def test_source_intervals_are_nonempty_bounded_integer_milliseconds():
    assert SourceInterval(start_ms=0, end_ms=1).end_ms == 1
    for values in [(1, 1), (2, 1), (-1, 1), (0, 120001), (True, 2), (0, 1.5)]:
        with pytest.raises(ValidationError):
            SourceInterval(start_ms=values[0], end_ms=values[1])


def test_policy_rejects_contradictory_or_unknown_semantics():
    assert TemporalPolicy().overlap_tolerance_ms == 0
    for values in [{'max_cycle_ms': 100, 'max_gap_ms': 101},
                   {'max_cycle_ms': 100, 'max_gap_ms': 0, 'overlap_tolerance_ms': 101},
                   {'endpoint_convention': 'closed'}, {'overlap_tolerance_ms': 501}]:
        with pytest.raises(ValidationError):
            TemporalPolicy(**values)
    with pytest.raises(ValidationError):
        UnknownPolicy(presentation='false')
    with pytest.raises(ValidationError):
        UnknownPolicy(negative_coverage='scan_complete')
