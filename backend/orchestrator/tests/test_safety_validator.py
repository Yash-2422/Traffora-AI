"""Unit tests for the independent safety validator.

The validator is the last line of defence: nothing it rejects may ever reach
SUMO. These tests pin every rule, including the regression that let a sequence
of short holds keep a phase green past the maximum green.
"""

from __future__ import annotations

import pytest

from backend.orchestrator.config import SafetyLimits
from backend.orchestrator.safety_validator import SafetyValidator
from backend.orchestrator.schemas import ActionType, ValidationStatus

from .conftest import make_signal_action


def _state(**overrides):
    state = {
        "tls_id": "A1",
        "phase": 0,
        "elapsed_s": 5.0,
        "remaining_s": 20.0,
        "num_phases": 4,
        "phase_kind": "GREEN",
        "state": "GGGrrr",
        "min_dur": 15.0,
        "max_dur": 60.0,
    }
    state.update(overrides)
    return state


@pytest.fixture
def validator() -> SafetyValidator:
    v = SafetyValidator(SafetyLimits())
    v.set_known_tls({"A1", "B1"})
    return v


def test_valid_extension_is_accepted(validator):
    result = validator.validate(make_signal_action("A1", 30.0), _state())
    assert result.valid is True
    assert result.status is ValidationStatus.VALID


def test_unknown_traffic_light_is_rejected(validator):
    result = validator.validate(make_signal_action("Z9", 20.0), _state(tls_id="Z9"))
    assert result.status is ValidationStatus.REJECTED
    assert "unknown traffic light" in result.reasons[0]


def test_phase_change_is_rejected(validator):
    result = validator.validate(make_signal_action("A1", 20.0, phase_index=2), _state())
    assert result.status is ValidationStatus.REJECTED
    assert "may only adjust the duration" in result.reasons[0]


def test_clearance_interval_cannot_be_touched(validator):
    result = validator.validate(
        make_signal_action("A1", 20.0), _state(phase=1, phase_kind="YELLOW"))
    assert result.status is ValidationStatus.REJECTED
    assert "clearance" in result.reasons[0]


def test_missing_live_state_is_deferred_not_applied(validator):
    result = validator.validate(make_signal_action("A1", 20.0), None)
    assert result.valid is False
    assert result.status is ValidationStatus.DEFERRED


def test_minimum_green_is_enforced(validator):
    # elapsed 2 s + 1 s requested = 3 s served, below the 15 s minimum.
    result = validator.validate(make_signal_action("A1", 1.0), _state(elapsed_s=2.0, remaining_s=1.0))
    assert result.status is ValidationStatus.REJECTED
    assert "minimum green" in result.reasons[0]


def test_emergency_preemption_may_cut_a_green_short(validator):
    # 4 s is the emergency floor, even though the program's minDur is 15 s.
    result = validator.validate(
        make_signal_action("A1", 4.0, emergency_vehicle_id="amb_1"),
        _state(elapsed_s=1.0, remaining_s=4.0), emergency=True)
    assert result.valid is True


def test_emergency_action_without_vehicle_id_is_rejected(validator):
    result = validator.validate(make_signal_action("A1", 20.0), _state(), emergency=True)
    assert result.status is ValidationStatus.REJECTED
    assert "no emergency_vehicle_id" in result.reasons[0]


def test_maximum_green_is_measured_over_the_whole_phase(validator):
    """Regression: repeated short holds must not pin a green past max_green.

    ``setPhaseDuration`` sets the *remaining* time, so a request shorter than
    the elapsed time cannot extend the phase at all: requesting 11 s at 55 s
    elapsed still serves 55 s and is within the 60 s ceiling. Requesting 75 s
    at 55 s elapsed would serve 75 s and must be refused.
    """
    accepted = validator.validate(make_signal_action("A1", 11.0),
                                  _state(elapsed_s=55.0, remaining_s=11.0))
    assert accepted.valid is True

    rejected = validator.validate(make_signal_action("A1", 75.0),
                                  _state(elapsed_s=55.0, remaining_s=75.0))
    assert rejected.status is ValidationStatus.REJECTED
    assert "maximum green" in rejected.reasons[0]


def test_program_max_dur_lowers_the_ceiling(validator):
    result = validator.validate(make_signal_action("A1", 40.0),
                                _state(elapsed_s=0.0, remaining_s=27.0, max_dur=27.0))
    assert result.status is ValidationStatus.REJECTED
    assert "maximum green" in result.reasons[0]


def test_extension_cap_is_enforced(validator):
    result = validator.validate(make_signal_action("A1", 55.0),
                                _state(elapsed_s=5.0, remaining_s=20.0, max_dur=60.0))
    assert result.status is ValidationStatus.REJECTED
    assert "max_extension_s" in result.reasons[0]


def test_non_positive_duration_is_rejected(validator):
    with pytest.raises(Exception):
        make_signal_action("A1", 0.0)


def test_action_type_must_be_permitted(validator):
    action = make_signal_action("A1", 20.0)
    action.action_type = ActionType.NONE
    result = validator.validate(action, _state())
    assert result.status is ValidationStatus.REJECTED
    assert "not permitted" in result.reasons[0]


def test_batch_rejects_two_actions_for_the_same_light(validator):
    actions = [make_signal_action("A1", 20.0, action_id="a1"),
               make_signal_action("A1", 25.0, action_id="a2")]
    accepted, results = validator.validate_batch(actions, {"A1": _state()})
    assert len(accepted) == 1
    assert results[0].valid is True
    assert results[1].status is ValidationStatus.REJECTED
    assert "conflicting simultaneous actions" in results[1].reasons[0]


def test_batch_accepts_independent_lights(validator):
    actions = [make_signal_action("A1", 20.0, action_id="a1"),
               make_signal_action("B1", 20.0, action_id="b1")]
    accepted, results = validator.validate_batch(
        actions, {"A1": _state(), "B1": _state(tls_id="B1")})
    assert len(accepted) == 2
    assert all(r.valid for r in results)


def test_is_safe_shortcut(validator):
    assert validator.is_safe(make_signal_action("A1", 20.0), _state()) is True
    assert validator.is_safe(make_signal_action("A1", 20.0, phase_index=1), _state()) is False
