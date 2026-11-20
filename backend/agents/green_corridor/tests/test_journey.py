"""Step 8 tests: journey metrics, tripinfo cross-check, baseline vs priority comparison.

These use the shared ``scenario`` and ``sim`` fixtures from conftest.py and real SUMO runs.
"""
import pytest

from backend.agents.green_corridor import priority_manager as pm
from backend.agents.green_corridor.intersection_monitor import IntersectionMonitor
from backend.agents.green_corridor.journey_monitor import (
    JourneyError, JourneyMonitor, compare_baseline_vs_priority, compare_journeys,
    monitor_emergency_journey, read_tripinfo, run_scenario)
from backend.agents.green_corridor.route_planner import RoutePlanner
from backend.agents.green_corridor.tests.conftest import requires_sumo

pytestmark = requires_sumo

VID = "amb_1"


@pytest.fixture(scope="module")
def baseline_run(scenario):
    return run_scenario(scenario, "baseline")


@pytest.fixture(scope="module")
def comparison(scenario):
    return compare_journeys(scenario)


def _journey_monitor(sc, detector):
    planner = RoutePlanner(sc)
    return JourneyMonitor(detector, IntersectionMonitor(sc, planner), planner)


def _drive_until(sc, jm, t):
    while sc.traci.simulation.getTime() < t:
        sc.step()
        jm.update(VID)


# ---- live monitoring ------------------------------------------------------------------------------
def test_not_started_before_departure(sim):
    sc, detector = sim
    jm = _journey_monitor(sc, detector)
    sc.step()
    jm.update(VID)
    with pytest.raises(JourneyError) as exc:
        jm.metrics(VID)
    assert exc.value.code == "JOURNEY_NOT_STARTED"
    env = monitor_emergency_journey(jm, VID)
    assert env["success"] is False and env["error"]["code"] == "JOURNEY_NOT_STARTED"


def test_invalid_vehicle_id(sim):
    sc, detector = sim
    jm = _journey_monitor(sc, detector)
    for bad in ("", "  ", None, 5):
        env = monitor_emergency_journey(jm, bad)
        assert env["success"] is False and env["error"]["code"] == "INVALID_INPUT"


def test_live_metrics_while_driving(sim):
    sc, detector = sim
    jm = _journey_monitor(sc, detector)
    _drive_until(sc, jm, 70)                       # ambulance departs at t=20
    env = monitor_emergency_journey(jm, VID)
    assert env["success"] is True
    m = env["data"]
    assert m["finished"] is False and m["arrival_time_s"] is None
    assert m["journey_time_s"] > 0 and m["distance_travelled_m"] > 0
    assert 0 < m["average_speed_kmh"] < 120
    assert m["estimated_journey_time_s"] is not None and m["estimated_journey_time_s"] > 0
    assert m["delay_vs_estimate_s"] is None        # only once finished


def test_distance_never_decreases(sim):
    sc, detector = sim
    jm = _journey_monitor(sc, detector)
    last = 0.0
    for _ in range(120):
        sc.step()
        jm.update(VID)
        if jm.has_started(VID):
            d = jm.metrics(VID).distance_travelled_m
            assert d >= last
            last = d
    assert last > 0


# ---- full baseline run ----------------------------------------------------------------------------
def test_baseline_completes(baseline_run):
    assert baseline_run.completed and baseline_run.stop_reason == "ARRIVED"
    m = baseline_run.metrics
    assert m.finished and not m.vehicle_lost
    assert m.arrival_time_s > m.depart_time_s


def test_baseline_distance_matches_route(baseline_run, scenario):
    ref = scenario.reference_route_length_m
    assert 0.95 * ref <= baseline_run.metrics.distance_travelled_m <= 1.5 * ref


def test_average_speed_is_distance_over_time(baseline_run):
    m = baseline_run.metrics
    assert m.average_speed_mps == pytest.approx(m.distance_travelled_m / m.journey_time_s, rel=1e-6)
    assert m.average_speed_kmh == pytest.approx(m.average_speed_mps * 3.6, rel=1e-6)


def test_intersections_crossed(baseline_run, scenario):
    m = baseline_run.metrics
    assert 1 <= m.signalised_intersections_crossed <= len(scenario.tls_ids)
    assert m.junctions_crossed >= m.signalised_intersections_crossed
    assert len(m.crossings) == m.signalised_intersections_crossed


def test_waiting_is_part_of_stopped_time(baseline_run):
    m = baseline_run.metrics
    assert 0 <= m.intersection_wait_time_s <= m.stopped_time_s + 1e-6
    assert m.stopped_time_s < m.journey_time_s


def test_estimate_excludes_signal_waiting(baseline_run):
    m = baseline_run.metrics
    assert m.estimated_journey_time_s is not None
    assert m.delay_vs_estimate_s == pytest.approx(m.journey_time_s - m.estimated_journey_time_s)


def test_matches_sumo_tripinfo(baseline_run):
    assert baseline_run.tripinfo is not None, "SUMO wrote no tripinfo entry for the ambulance"
    assert baseline_run.tripinfo["duration_s"] > 0
    cc = baseline_run.cross_check
    assert cc is not None and cc["consistent"], cc


def test_baseline_is_deterministic(baseline_run, comparison):
    assert comparison.baseline.metrics.journey_time_s == baseline_run.metrics.journey_time_s
    assert comparison.baseline.metrics.distance_travelled_m == pytest.approx(
        baseline_run.metrics.distance_travelled_m)


# ---- comparison -----------------------------------------------------------------------------------
def test_comparison_completes_both(comparison):
    assert comparison.completed_both
    assert comparison.baseline.mode == "baseline" and comparison.priority.mode == "priority"
    assert comparison.priority.priority_errors == []


def test_time_saved_is_the_measured_difference(comparison):
    b, p = comparison.baseline.metrics, comparison.priority.metrics
    assert comparison.time_saved_s == b.journey_time_s - p.journey_time_s     # not clamped, not invented
    assert comparison.intersection_wait_saved_s == pytest.approx(
        b.intersection_wait_time_s - p.intersection_wait_time_s)
    assert comparison.average_speed_change_kmh == pytest.approx(p.average_speed_kmh - b.average_speed_kmh)


def test_priority_run_matches_sumo_tripinfo(comparison):
    cc = comparison.priority.cross_check
    assert cc is not None and cc["consistent"], cc


def test_no_interventions_means_identical_runs(comparison):
    """If the manager never touched a signal, the two runs must be exactly the same."""
    if comparison.priority.priority["signal_interventions"] == 0:
        assert comparison.time_saved_s == 0
    else:
        assert comparison.priority.priority["actions"], "interventions were counted but not recorded"


def test_priority_summary_shape(comparison):
    s = comparison.priority.priority
    for key in ("requests", "by_state", "tls_ids", "signal_interventions", "extensions", "truncations",
                "restores", "actions"):
        assert key in s
    assert s["signal_interventions"] >= s["extensions"] + s["truncations"]


def test_report_is_json_serialisable(comparison):
    import json
    json.dumps(comparison.to_dict())


def test_public_comparison_envelope(scenario):
    env = compare_baseline_vs_priority(scenario)
    assert env["success"] is True and env["error"] is None
    assert env["data"]["completed_both"] is True
    assert "time_saved_s" in env["data"] and env["data"]["notes"]


def test_respect_min_dur_override_is_restored(scenario):
    before = pm.PRIORITY_RESPECT_MIN_DUR
    run = run_scenario(scenario, "priority", respect_min_dur=not before)
    assert run.respect_min_dur is (not before)
    assert pm.PRIORITY_RESPECT_MIN_DUR is before


# ---- invalid input and edge cases -----------------------------------------------------------------
def test_invalid_mode(scenario):
    with pytest.raises(JourneyError) as exc:
        run_scenario(scenario, "turbo")
    assert exc.value.code == "INVALID_INPUT"


def test_unknown_vehicle_runs_to_time_limit(scenario):
    run = run_scenario(scenario, "baseline", vehicle_id="nobody_1")
    assert run.completed is False and run.stop_reason == "TIME_LIMIT"
    assert run.metrics is None and run.cross_check is None


def test_read_tripinfo_missing_file(tmp_path):
    assert read_tripinfo(tmp_path / "nope.xml", VID) is None
    bad = tmp_path / "bad.xml"
    bad.write_text("<tripinfos><tripinfo")
    assert read_tripinfo(bad, VID) is None