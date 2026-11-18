"""Tests for CongestionDetector: scoring, levels, persistence, alerts, attention list."""

import json

import pytest

from backend.agents.traffic_mind import simulation_interface as si
from backend.agents.traffic_mind.config import CongestionConfig, SimulationConfig
from backend.agents.traffic_mind.congestion_detector import CongestionDetector
from backend.agents.traffic_mind.exceptions import InvalidTrafficDataError
from backend.agents.traffic_mind.schemas import (
    CongestionLevel,
    EdgeMetrics,
    JunctionMetrics,
    NetworkMetrics,
    TrafficSnapshot,
)
from backend.agents.traffic_mind.traffic_monitor import TrafficMonitor

LOW, MEDIUM, HIGH = CongestionLevel.LOW, CongestionLevel.MEDIUM, CongestionLevel.HIGH
LENGTH = 200.0


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def edge(edge_id="E1", junction_id="J1", vehicles=0, occupancy=0.0, queue_veh=0, queue_m=0.0,
         wait=0.0, speed_ratio=1.0, length=LENGTH):
    return EdgeMetrics(
        edge_id=edge_id, junction_id=junction_id, length_m=length, lane_count=1,
        speed_limit_mps=10.0, vehicle_count=vehicles,
        density_veh_per_km=vehicles / (length / 1000), density_veh_per_km_lane=vehicles / (length / 1000),
        occupancy_pct=occupancy, queue_length_veh=queue_veh, queue_length_m=queue_m,
        mean_waiting_time_s=wait, mean_speed_mps=speed_ratio * 10.0, speed_ratio=speed_ratio,
        flow_veh_per_hour=0.0,
    )


def junction(junction_id="J1", edge_ids=("E1",), vehicles=0, queue_veh=0, queue_m=0.0, wait=0.0):
    return JunctionMetrics(
        junction_id=junction_id, incoming_edges=list(edge_ids), vehicle_count=vehicles,
        queue_length_veh=queue_veh, max_queue_length_m=queue_m, mean_waiting_time_s=wait,
        max_occupancy_pct=0.0, flow_veh_per_hour=0.0,
    )


def snapshot(t, edges, junctions=None):
    if junctions is None:  # one junction whose metrics are derived from the given roads
        junctions = [junction(
            edge_ids=[e.edge_id for e in edges],
            vehicles=sum(e.vehicle_count for e in edges),
            queue_veh=sum(e.queue_length_veh for e in edges),
            queue_m=max((e.queue_length_m for e in edges), default=0.0),
            wait=max((e.mean_waiting_time_s for e in edges), default=0.0),
        )]
    return TrafficSnapshot(
        timestamp_s=t,
        network=NetworkMetrics(total_vehicles=sum(e.vehicle_count for e in edges), halting_vehicles=0,
                               mean_waiting_time_s=0.0, mean_speed_ratio=1.0),
        junctions=junctions, edges=edges,
    )


# an approach road in each state (scores under the default config)
def calm():
    return edge(vehicles=2, occupancy=5.0, speed_ratio=0.9)


def medium_road():
    # occupancy .33 queue .6 wait .28 speed .5 -> score ~0.43
    return edge(vehicles=8, occupancy=15.0, queue_veh=4, queue_m=60.0, wait=20.0, speed_ratio=0.5)


def jammed():
    # figures from a real jam in the sample network (road A1B1 at t=1400 s) -> score ~0.8
    return edge(vehicles=20, occupancy=42.4, queue_veh=17, queue_m=151.4, wait=8.15, speed_ratio=0.0084,
                length=235.6)


def run(detector, road_factory, times):
    """Feed the same kind of road at the given times; returns the last report."""
    report = None
    for t in times:
        report = detector.update(snapshot(t, [road_factory()]))
    return report


def junction_of(report, junction_id="J1"):
    return next(j for j in report.junctions if j.junction_id == junction_id)


# smoothing is switched off here so that each rule below is tested on its own
UNSMOOTHED = CongestionConfig(smoothing_window_s=0)
NO_DELAY = CongestionConfig(escalate_after_s=0, clear_after_s=0, smoothing_window_s=0)


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #
def test_each_factor_is_scaled_against_its_reference():
    detector = CongestionDetector()
    f = detector.edge_factors(edge(vehicles=10, occupancy=22.5, queue_m=25.0, wait=35.0, speed_ratio=0.5))
    assert f.occupancy == pytest.approx(0.5)      # 22.5 / 45
    assert f.queue == pytest.approx(0.25)         # (25 / 200) / 0.5
    assert f.waiting == pytest.approx(0.5)        # 35 / 70
    assert f.speed_drop == pytest.approx(0.5)     # 1 - 0.5


def test_factors_are_capped_at_one():
    f = CongestionDetector().edge_factors(
        edge(vehicles=30, occupancy=100.0, queue_m=200.0, wait=500.0, speed_ratio=0.0))
    assert (f.occupancy, f.queue, f.waiting, f.speed_drop) == (1.0, 1.0, 1.0, 1.0)


def test_score_is_the_weighted_sum():
    detector = CongestionDetector()
    f = detector.edge_factors(edge(vehicles=10, occupancy=22.5, queue_m=25.0, wait=35.0, speed_ratio=0.5))
    assert detector.score(f) == pytest.approx(0.25 * 0.5 + 0.35 * 0.25 + 0.20 * 0.5 + 0.20 * 0.5)


def test_single_stopped_car_is_not_congestion():
    # one car, stopped for a long time: waiting and speed signals are damped by the load factor
    detector = CongestionDetector()
    road = edge(vehicles=1, occupancy=2.5, queue_veh=1, queue_m=6.0, wait=60.0, speed_ratio=0.0)
    f = detector.edge_factors(road)
    assert f.waiting == pytest.approx(60 / 70 * 0.25) and f.speed_drop == pytest.approx(0.25)
    assert detector.classify(detector.score(f)) == LOW


def test_short_red_light_queue_is_not_congestion():
    # 2 cars waiting for the green (8 s so far)
    detector = CongestionDetector()
    road = edge(vehicles=2, occupancy=4.5, queue_veh=2, queue_m=14.0, wait=8.0, speed_ratio=0.0)
    assert detector.classify(detector.score(detector.edge_factors(road))) == LOW


def test_longer_red_light_queue_only_counts_if_it_persists():
    # 5 stopped cars (18 s wait) score MEDIUM for this one sample (raw level) ...
    def red_queue():
        return edge(vehicles=5, occupancy=11.0, queue_veh=5, queue_m=35.0, wait=18.0, speed_ratio=0.0)

    detector = CongestionDetector(UNSMOOTHED)
    run(detector, calm, [0])
    report = run(detector, red_queue, [5])
    assert junction_of(report).raw_level == MEDIUM and junction_of(report).level == LOW
    # ... and the green clears it before the 15 s persistence rule is met
    for t in (10, 15):
        report = run(detector, calm, [t])
    assert junction_of(report).level == LOW and report.alerts == []


def test_a_real_jam_is_high():
    detector = CongestionDetector(UNSMOOTHED)
    assert detector.classify(detector.score(detector.edge_factors(jammed()))) == HIGH


def test_an_empty_road_scores_zero():
    detector = CongestionDetector(UNSMOOTHED)
    assert detector.score(detector.edge_factors(edge())) == 0.0


@pytest.mark.parametrize("score,expected", [
    (0.0, LOW), (0.2499, LOW), (0.25, MEDIUM), (0.4999, MEDIUM), (0.50, HIGH), (1.0, HIGH),
])
def test_classification_thresholds(score, expected):
    assert CongestionDetector(UNSMOOTHED).classify(score) == expected


def test_thresholds_come_from_the_config():
    detector = CongestionDetector(CongestionConfig(medium_threshold=0.1, high_threshold=0.2))
    assert detector.classify(0.15) == MEDIUM and detector.classify(0.25) == HIGH


def test_dominant_factor_is_the_largest_weighted_contribution():
    detector = CongestionDetector(UNSMOOTHED)
    f = detector.edge_factors(edge(vehicles=10, occupancy=45.0, queue_m=0.0, wait=0.0, speed_ratio=1.0))
    assert detector.dominant_factor(f) == "occupancy"
    f = detector.edge_factors(edge(vehicles=10, occupancy=0.0, queue_m=100.0, wait=0.0, speed_ratio=1.0))
    assert detector.dominant_factor(f) == "queue"


@pytest.mark.parametrize("kwargs", [
    {"weight_occupancy": 0.5},                       # weights no longer add up to 1
    {"weight_queue": -0.1, "weight_waiting": 0.65},  # negative weight
    {"medium_threshold": 0.7, "high_threshold": 0.6},
    {"medium_threshold": 0.0},
    {"high_threshold": 1.5},
    {"waiting_ref_s": 0},
    {"min_vehicles_full_weight": 0},
    {"clear_after_s": -1},
    {"smoothing_window_s": -5},
])
def test_invalid_config_is_rejected(kwargs):
    with pytest.raises(ValueError):
        CongestionConfig(**kwargs)


# --------------------------------------------------------------------------- #
# junctions and roads
# --------------------------------------------------------------------------- #
def test_junction_takes_the_worst_approach_road():
    detector = CongestionDetector(NO_DELAY)
    roads = [edge("E1", vehicles=2), jammed().model_copy(update={"edge_id": "E2"}), medium_road().model_copy(update={"edge_id": "E3"})]
    report = detector.update(snapshot(10, roads, [junction(edge_ids=["E1", "E2", "E3"], vehicles=30,
                                                           queue_veh=21, queue_m=151.4, wait=8.0)]))
    j = junction_of(report)
    assert j.level == HIGH and j.worst_edge_id == "E2"
    assert j.score == max(e.score for e in report.edges)
    assert (j.vehicle_count, j.queue_length_veh, j.max_queue_length_m) == (30, 21, 151.4)
    assert {e.edge_id: e.level for e in report.edges} == {"E1": LOW, "E2": HIGH, "E3": MEDIUM}


def test_report_lists_every_road_and_junction_and_a_matching_summary():
    detector = CongestionDetector(NO_DELAY)
    roads = [edge("E1", "J1", vehicles=2), edge("E2", "J2", vehicles=0),
             jammed().model_copy(update={"edge_id": "E3", "junction_id": "J2"})]
    report = detector.update(snapshot(10, roads, [junction("J1", ["E1"]), junction("J2", ["E2", "E3"])]))
    assert len(report.edges) == 3 and len(report.junctions) == 2
    s = report.summary
    assert (s.junctions_low, s.junctions_medium, s.junctions_high) == (1, 0, 1)
    assert (s.roads_low, s.roads_medium, s.roads_high) == (2, 0, 1)


def test_junction_without_matching_roads_is_low():
    report = CongestionDetector(UNSMOOTHED).update(snapshot(5, [], [junction("J9", ["GONE"])]))
    j = junction_of(report, "J9")
    assert j.level == LOW and j.score == 0.0 and j.worst_edge_id is None


def test_empty_snapshot_is_handled():
    report = CongestionDetector(UNSMOOTHED).update(snapshot(5, [], []))
    assert report.alerts == [] and report.attention_junctions == [] and report.edges == []


# --------------------------------------------------------------------------- #
# smoothing
# --------------------------------------------------------------------------- #
def test_a_single_spike_is_averaged_out():
    calm_then_spike = [(t, calm) for t in range(0, 65, 5)] + [(65, jammed)]
    smoothed, instant = CongestionDetector(), CongestionDetector(UNSMOOTHED)
    for t, road in calm_then_spike:
        smoothed_report = run(smoothed, road, [t])
        instant_report = run(instant, road, [t])
    assert junction_of(instant_report).raw_level == HIGH
    assert junction_of(smoothed_report).raw_level == LOW
    assert junction_of(smoothed_report).score < junction_of(instant_report).score / 5


def test_sustained_congestion_is_reported_in_full():
    detector = CongestionDetector()          # smoothing 70 s, escalate after 15 s
    report = run(detector, jammed, range(0, 120, 5))
    j = junction_of(report)
    assert j.level == HIGH
    assert j.score == pytest.approx(CongestionDetector(UNSMOOTHED).update(
        snapshot(0, [jammed()])).junctions[0].score, abs=1e-3)


def test_reported_factors_are_the_average_over_the_window():
    detector = CongestionDetector(CongestionConfig(smoothing_window_s=100))
    run(detector, lambda: edge(vehicles=10, occupancy=45.0), [0])       # occupancy factor 1.0
    report = run(detector, lambda: edge(vehicles=10, occupancy=0.0), [5])   # occupancy factor 0.0
    assert report.edges[0].factors.occupancy == pytest.approx(0.5)


def test_samples_older_than_the_window_are_forgotten():
    detector = CongestionDetector(CongestionConfig(smoothing_window_s=30))
    run(detector, jammed, [0, 5, 10])
    report = run(detector, calm, [50])      # the jam is more than 30 s old
    assert report.edges[0].score == pytest.approx(
        CongestionDetector(UNSMOOTHED).update(snapshot(0, [calm()])).edges[0].score, abs=1e-3)


def test_reported_score_matches_the_reported_factors():
    detector = CongestionDetector()
    report = None
    for t in range(0, 100, 5):
        report = run(detector, jammed if t % 20 < 10 else calm, [t])
    e = report.edges[0]
    cfg = detector.config
    expected = (cfg.weight_occupancy * e.factors.occupancy + cfg.weight_queue * e.factors.queue
                + cfg.weight_waiting * e.factors.waiting + cfg.weight_speed_drop * e.factors.speed_drop)
    assert e.score == pytest.approx(expected, abs=1e-3)


def test_each_road_is_smoothed_on_its_own():
    detector = CongestionDetector()
    for t in range(0, 40, 5):
        report = detector.update(snapshot(t, [
            jammed().model_copy(update={"edge_id": "E1"}),
            calm().model_copy(update={"edge_id": "E2"}),
        ], [junction("J1", ["E1", "E2"])]))
    by_id = {e.edge_id: e for e in report.edges}
    assert by_id["E1"].raw_level == HIGH and by_id["E2"].raw_level == LOW


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #
def test_first_sample_sets_the_level_immediately():
    report = run(CongestionDetector(UNSMOOTHED), jammed, [100])
    assert junction_of(report).level == HIGH and junction_of(report).since_s == 100


def test_escalation_needs_the_condition_to_hold():
    detector = CongestionDetector(UNSMOOTHED)          # escalate after 15 s
    run(detector, calm, [0])
    for t in (5, 10, 15):
        report = run(detector, jammed, [t])
        j = junction_of(report)
        assert j.raw_level == HIGH and j.level == LOW, f"t={t}"
    report = run(detector, jammed, [20])     # HIGH has now held for 15 s
    j = junction_of(report)
    assert j.level == HIGH and j.since_s == 20


def test_short_spikes_do_not_flicker_the_level():
    detector = CongestionDetector(UNSMOOTHED)
    run(detector, calm, [0])
    for i in range(1, 30):
        report = run(detector, jammed if i % 2 else calm, [5 * i])
        assert junction_of(report).level == LOW


def test_clearing_is_slower_than_escalating():
    detector = CongestionDetector(UNSMOOTHED)          # clear after 30 s
    run(detector, jammed, [0, 5, 10, 15, 20])
    assert junction_of(detector.latest).level == HIGH
    for t in (25, 30, 35, 40, 45, 50):       # calm for 25 s
        assert junction_of(run(detector, calm, [t])).level == HIGH, f"t={t}"
    assert junction_of(run(detector, calm, [55])).level == LOW   # calm has held for 30 s


def test_level_steps_down_to_the_highest_level_seen_while_clearing():
    detector = CongestionDetector(UNSMOOTHED)
    run(detector, jammed, [0, 5, 10, 15, 20])
    report = None
    for t in range(25, 60, 5):
        report = run(detector, medium_road, [t])
    j = junction_of(report)
    assert j.raw_level == MEDIUM and j.level == MEDIUM


def test_zero_delay_config_follows_every_sample():
    detector = CongestionDetector(NO_DELAY)
    assert junction_of(run(detector, jammed, [0])).level == HIGH
    assert junction_of(run(detector, calm, [5])).level == LOW
    assert junction_of(run(detector, jammed, [10])).level == HIGH


def test_roads_and_junctions_are_stabilised_independently():
    detector = CongestionDetector(UNSMOOTHED)
    run(detector, calm, [0])
    report = run(detector, jammed, [5])
    assert report.edges[0].raw_level == HIGH and report.edges[0].level == LOW


def test_time_going_backwards_resets_state():
    detector = CongestionDetector(UNSMOOTHED)
    run(detector, jammed, [100])
    assert junction_of(detector.latest).level == HIGH
    report = run(detector, calm, [5])        # simulation was restarted
    assert junction_of(report).level == LOW and junction_of(report).since_s == 5


def test_reset_clears_everything():
    detector = CongestionDetector(UNSMOOTHED)
    run(detector, jammed, [0])
    detector.reset()
    assert detector.latest is None
    assert junction_of(run(detector, calm, [1])).level == LOW


# --------------------------------------------------------------------------- #
# alerts
# --------------------------------------------------------------------------- #
def test_no_alerts_when_traffic_is_calm():
    assert run(CongestionDetector(), calm, [0, 5, 10]).alerts == []


def test_alert_severity_follows_the_level():
    medium = run(CongestionDetector(NO_DELAY), medium_road, [0])
    high = run(CongestionDetector(NO_DELAY), jammed, [0])
    assert {(a.entity_type, a.level, a.severity) for a in medium.alerts} == {
        ("junction", MEDIUM, "warning"), ("road", MEDIUM, "warning")}
    assert {(a.entity_type, a.level, a.severity) for a in high.alerts} == {
        ("junction", HIGH, "critical"), ("road", HIGH, "critical")}


def test_alert_is_new_once_and_keeps_its_id():
    detector = CongestionDetector(NO_DELAY)
    first = junction_alert(run(detector, jammed, [0]))
    second = junction_alert(run(detector, jammed, [5]))
    assert first.is_new and not second.is_new
    assert first.alert_id == second.alert_id and second.since_s == 0


def test_alert_message_names_the_junction_road_and_numbers():
    alert = junction_alert(run(CongestionDetector(NO_DELAY), jammed, [0]))
    assert "Junction J1" in alert.message and "HIGH" in alert.message and "E1" in alert.message
    assert "17 stopped" in alert.message and "151 m" in alert.message
    assert "Right now" in alert.message
    assert "averaged" not in alert.message          # this detector does not smooth


def test_alert_message_says_when_the_score_is_averaged():
    detector = CongestionDetector(CongestionConfig(escalate_after_s=0, clear_after_s=0))
    alert = junction_alert(run(detector, jammed, [0]))
    assert "averaged over the last 70 s" in alert.message


def test_alerts_are_ordered_critical_first_junctions_before_roads():
    detector = CongestionDetector(NO_DELAY)
    roads = [medium_road().model_copy(update={"edge_id": "E1", "junction_id": "J1"}),
             jammed().model_copy(update={"edge_id": "E2", "junction_id": "J2"})]
    report = detector.update(snapshot(0, roads, [junction("J1", ["E1"]), junction("J2", ["E2"])]))
    order = [(a.level, a.entity_type, a.entity_id) for a in report.alerts]
    assert order == [(HIGH, "junction", "J2"), (HIGH, "road", "E2"),
                     (MEDIUM, "junction", "J1"), (MEDIUM, "road", "E1")]


def test_alert_disappears_once_the_level_has_cleared():
    detector = CongestionDetector(NO_DELAY)
    run(detector, jammed, [0])
    assert run(detector, calm, [5]).alerts == []


def junction_alert(report):
    return next(a for a in report.alerts if a.entity_type == "junction")


# --------------------------------------------------------------------------- #
# attention list
# --------------------------------------------------------------------------- #
def test_nothing_needs_attention_when_calm():
    assert run(CongestionDetector(), calm, [0, 5]).attention_junctions == []


def test_high_junction_needs_attention_at_once():
    report = run(CongestionDetector(NO_DELAY), jammed, [0])
    item = report.attention_junctions[0]
    assert (item.rank, item.junction_id, item.level) == (1, "J1", HIGH)
    assert item.worst_edge_id == "E1" and "HIGH" in item.reason
    assert junction_of(report).requires_attention


def test_medium_junction_needs_attention_only_after_a_while():
    detector = CongestionDetector(NO_DELAY)   # attention after 60 s of MEDIUM
    assert run(detector, medium_road, [0, 30, 59]).attention_junctions == []
    report = run(detector, medium_road, [60])
    assert [a.junction_id for a in report.attention_junctions] == ["J1"]
    assert "MEDIUM" in report.attention_junctions[0].reason and "60" in report.attention_junctions[0].reason


def test_attention_list_is_ranked_by_level_then_score():
    detector = CongestionDetector(NO_DELAY)
    worse = jammed().model_copy(update={"edge_id": "E1", "junction_id": "J1", "occupancy_pct": 20.0})
    worst = jammed().model_copy(update={"edge_id": "E2", "junction_id": "J2"})
    ok = calm().model_copy(update={"edge_id": "E3", "junction_id": "J3"})
    report = detector.update(snapshot(0, [worse, worst, ok], [
        junction("J1", ["E1"]), junction("J2", ["E2"]), junction("J3", ["E3"])]))
    assert [(a.rank, a.junction_id) for a in report.attention_junctions] == [(1, "J2"), (2, "J1")]


# --------------------------------------------------------------------------- #
# invalid data and output
# --------------------------------------------------------------------------- #
def good_dict():
    return snapshot(10, [calm()]).to_dict()


def test_dict_input_is_accepted():
    assert CongestionDetector().update(good_dict()).timestamp_s == 10


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("timestamp_s"),
    lambda d: d.pop("edges"),
    lambda d: d.update(timestamp_s=-5),
    lambda d: d.update(timestamp_s=float("nan")),
    lambda d: d.update(timestamp_s="soon"),
    lambda d: d["edges"][0].update(occupancy_pct=-1),
    lambda d: d["edges"][0].update(occupancy_pct=float("nan")),
    lambda d: d["edges"][0].update(speed_ratio=2.5),
    lambda d: d["edges"][0].update(length_m=0),
    lambda d: d["edges"][0].pop("vehicle_count"),
    lambda d: d.update(unexpected=1),
])
def test_invalid_snapshots_are_rejected(mutate):
    data = good_dict()
    mutate(data)
    with pytest.raises(InvalidTrafficDataError) as err:
        CongestionDetector().update(data)
    assert err.value.to_dict()["code"] == "INVALID_TRAFFIC_DATA"


def test_non_snapshot_input_is_rejected():
    with pytest.raises(InvalidTrafficDataError):
        CongestionDetector().update("not a snapshot")
    with pytest.raises(InvalidTrafficDataError):
        CongestionDetector().update(None)


def test_failed_update_leaves_detector_usable():
    detector = CongestionDetector(NO_DELAY)
    run(detector, jammed, [0])
    with pytest.raises(InvalidTrafficDataError):
        detector.update({"timestamp_s": 5})
    assert junction_of(run(detector, jammed, [10])).level == HIGH


def test_report_is_json_serializable_with_string_levels():
    report = run(CongestionDetector(NO_DELAY), jammed, [0])
    data = json.loads(json.dumps(report.to_dict()))
    assert data["status"] == "ok"
    assert set(data) == {"status", "timestamp_s", "summary", "junctions", "edges", "alerts",
                         "attention_junctions"}
    assert data["junctions"][0]["level"] == "HIGH" and data["alerts"][0]["severity"] == "critical"


# --------------------------------------------------------------------------- #
# with the simulation (needs SUMO)
# --------------------------------------------------------------------------- #
needs_sumo = pytest.mark.skipif(
    si.find_sumo_binary() is None or not SimulationConfig(scenario="mixed").resolved_sumocfg().is_file(),
    reason="SUMO or the built scenario is missing (run: python simulation/traffic_mind/build_scenario.py)",
)


def simulate(scenario, until):
    """Yield (time, report) every 5 s of simulated time."""
    with si.SimulationInterface(SimulationConfig(scenario=scenario)) as sim:
        monitor, detector = TrafficMonitor.from_source(sim), CongestionDetector()
        while sim.current_time < until:
            sim.step(5)
            yield sim.current_time, detector.update(monitor.sample(sim))


@needs_sumo
def test_mixed_scenario_goes_from_calm_to_congested():
    reports = dict(simulate("mixed", 1600))
    assert reports[300].summary.junctions_low == 9                    # quiet start
    peak = reports[1500]
    assert peak.summary.junctions_high >= 1                           # directional peak
    assert peak.attention_junctions and peak.attention_junctions[0].rank == 1
    assert any(a.severity == "critical" for a in peak.alerts)
    json.dumps(peak.to_dict())


@needs_sumo
def test_offpeak_traffic_is_never_high():
    worst = max(report.summary.junctions_high for _, report in simulate("offpeak", 1200))
    assert worst == 0