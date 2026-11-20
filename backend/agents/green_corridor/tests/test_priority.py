"""GreenCorridor - Step 7 tests.

I could not run these myself (no SUMO/traci in my sandbox). Run in WSL:

    python -m pytest backend/agents/green_corridor/tests -q -p no:cacheprovider

and paste me the summary line. These poll the running simulation for the
actual condition (e.g. "ambulance's signal is currently green") rather than
scripting an exact timestep, since I don't know your scenario's seeded
traffic well enough to predict a phase down to the second; a condition that
never occurs within the step budget is skipped, not failed, so a skip here
means "the scenario never produced this case," not "the code is broken."
"""
from __future__ import annotations

import pytest

from backend.agents.green_corridor.intersection_monitor import IntersectionMonitor
from backend.agents.green_corridor.priority_manager import (
    PRIORITY_REQUEST_HORIZON_M,
    PriorityManager,
    PriorityState,
    generate_signal_priority_request,
    restore_normal_operation,
)
from .conftest import requires_sumo

pytestmark = requires_sumo

AMBULANCE_ID = "amb_1"
# Comfortably under the `scenario` fixture's sim_end_s=600: stepping past a
# SUMO run's configured end can make the process exit mid-loop, which is a
# different failure than anything these tests are checking for.
MAX_STEPS = 580


@pytest.fixture
def priority_env(sim):
    """Builds on the shared `sim` fixture: (sc, detector) -> (sc, monitor, manager)."""
    sc, _detector = sim
    monitor = IntersectionMonitor(sc)
    manager = PriorityManager(monitor)
    return sc, monitor, manager


def _step_until(sc, predicate, max_steps=MAX_STEPS):
    """Advance the sim one step at a time until predicate() is true."""
    for _ in range(max_steps):
        sc.step()
        if predicate():
            return True
    return False


def _next_signal(monitor):
    try:
        status = monitor.status(AMBULANCE_ID)
    except Exception as exc:
        # Same benign case as in priority_manager.py: before depart time the
        # vehicle has never been seen, so there is no "next signal" yet.
        if getattr(exc, "code", None) == "VEHICLE_NOT_FOUND":
            return None
        raise
    if status.next_intersection is None:
        return None
    return status.next_intersection


class TestPriorityRequestLifecycle:
    def test_no_requests_before_ambulance_is_near_a_signal(self, priority_env):
        sc, monitor, manager = priority_env
        result = generate_signal_priority_request(manager, AMBULANCE_ID)
        assert result["success"] is True
        assert result["data"]["active_requests"] == []

    def test_request_created_once_within_horizon(self, priority_env):
        sc, monitor, manager = priority_env
        found = _step_until(
            sc,
            lambda: (ui := _next_signal(monitor)) is not None
            and ui.distance_m <= PRIORITY_REQUEST_HORIZON_M,
        )
        assert found, "ambulance never came within the priority horizon of a signal"
        result = generate_signal_priority_request(manager, AMBULANCE_ID)
        assert result["success"] is True
        reqs = result["data"]["active_requests"]
        assert len(reqs) == 1
        assert reqs[0]["tls_id"] == _next_signal(monitor).tls_id
        assert reqs[0]["state"] in (
            PriorityState.REQUESTED.value,
            PriorityState.GRANTED_TRUNCATING.value,
            PriorityState.GRANTED_EXTENDING.value,
            PriorityState.ACTIVE_GREEN.value,
        )

    def test_never_shortens_a_clearance_or_all_red_phase(self, priority_env):
        """Core safety test: once the signal is in CLEARANCE or ALL_RED,
        GreenCorridor must leave its remaining time untouched."""
        sc, monitor, manager = priority_env
        found = _step_until(
            sc,
            lambda: (ui := _next_signal(monitor)) is not None
            and ui.signal is not None
            and ui.signal.phase_kind != "GREEN"
            and ui.distance_m <= PRIORITY_REQUEST_HORIZON_M,
        )
        if not found:
            pytest.skip("ambulance never approached during a clearance/all-red phase")
        before = _next_signal(monitor).signal.phase_remaining_s
        generate_signal_priority_request(manager, AMBULANCE_ID)
        after = _next_signal(monitor).signal.phase_remaining_s
        assert after == pytest.approx(before, abs=1e-6)

    def test_truncation_never_undercuts_the_configured_floor(self, priority_env):
        """Whatever the remaining time was the instant before this call, the
        remaining time right after must equal min(remaining_before, floor) --
        never less than that floor (the safety requirement), but also never
        asserted to be >= floor outright: if the natural countdown had
        already decayed below the floor on its own (nothing to do with us),
        that is not a violation, since we only ever shorten, never extend,
        a conflicting green."""
        sc, monitor, manager = priority_env
        found = _step_until(
            sc,
            lambda: (ui := _next_signal(monitor)) is not None
            and ui.signal is not None
            and ui.signal.phase_kind == "GREEN"
            and ui.signal.ambulance_signal != "GREEN"
            and ui.distance_m <= PRIORITY_REQUEST_HORIZON_M,
        )
        if not found:
            pytest.skip("ambulance never faced a conflicting green within the horizon")

        ui = _next_signal(monitor)  # same sim tick as the predicate above, no step in between
        tls_id = ui.tls_id
        logic = monitor._logic(tls_id)
        phase_idx = ui.signal.phase_index
        remaining_before = ui.signal.phase_remaining_s

        from backend.agents.green_corridor import priority_manager as pm
        phase = logic.phases[phase_idx]
        floor = phase.minDur if pm.PRIORITY_RESPECT_MIN_DUR and phase.minDur > 0 else pm.PRIORITY_MIN_GREEN_FLOOR_S
        expected_after = min(remaining_before, floor)

        generate_signal_priority_request(manager, AMBULANCE_ID)
        remaining_after = sc.traci.trafficlight.getNextSwitch(tls_id) - sc.traci.simulation.getTime()

        assert remaining_after == pytest.approx(expected_after, abs=1e-6)
        # And independently of the above: never *increase* a conflicting green.
        assert remaining_after <= remaining_before + 1e-6

    def test_restores_after_ambulance_passes(self, priority_env):
        sc, monitor, manager = priority_env
        restored_tls = []
        for _ in range(MAX_STEPS):
            sc.step()
            result = generate_signal_priority_request(manager, AMBULANCE_ID)
            if not result["success"]:
                break
            for r in result["data"]["active_requests"]:
                if r["state"] == PriorityState.RESTORED.value and r["tls_id"] not in restored_tls:
                    restored_tls.append(r["tls_id"])
        assert len(restored_tls) >= 1, "no signal was ever restored after the ambulance passed it"

    def test_never_holds_two_active_requests_for_the_same_tls(self, priority_env):
        sc, monitor, manager = priority_env
        last_result = None
        for _ in range(MAX_STEPS):
            sc.step()
            last_result = generate_signal_priority_request(manager, AMBULANCE_ID)
            if not last_result["success"]:
                break
        tls_ids = [r["tls_id"] for r in last_result["data"]["active_requests"]]
        assert len(tls_ids) == len(set(tls_ids))

    def test_closed_connection_is_reported_not_crashed(self, priority_env):
        sc, monitor, manager = priority_env
        sc.close()  # a graceful close here; a real mid-run crash is covered by test_connection.py
        result = generate_signal_priority_request(manager, AMBULANCE_ID)
        assert result["success"] is False
        assert result["error"]["code"] == "NOT_CONNECTED"

    def test_restore_normal_operation_errors_on_unmanaged_tls(self, priority_env):
        sc, monitor, manager = priority_env
        result = restore_normal_operation(manager, "no_such_tls")
        assert result["success"] is False
        assert result["error"]["code"] == "TLS_NOT_MANAGED"