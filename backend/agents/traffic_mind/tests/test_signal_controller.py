"""Tests for signal programs, the safety validator and SignalAdvisor. No SUMO needed."""

import json
import random

import pytest

from backend.agents.traffic_mind.config import CongestionConfig, SignalConfig
from backend.agents.traffic_mind.congestion_detector import CongestionDetector
from backend.agents.traffic_mind.exceptions import InvalidTrafficDataError, UnsafeSignalPlanError
from backend.agents.traffic_mind.schemas import (
    CongestionLevel,
    EdgeMetrics,
    JunctionMetrics,
    JunctionPrediction,
    NetworkMetrics,
    PredictionReport,
    RawSignalProgram,
    TrafficSnapshot,
)
from backend.agents.traffic_mind.signal_controller import (
    SignalAdvisor,
    build_signal_program,
    validate_plan,
)

UNSMOOTHED = CongestionConfig(smoothing_window_s=1, escalate_after_s=0, clear_after_s=0,
                              attention_medium_after_s=0)
CFG = SignalConfig()
LENGTH = 200.0
EDGES = ("A", "B", "C", "D")          # A, B -> stage 1;  C, D -> stage 2


def raw_program(junction_id="J1", g1=30, g2=30, yellow=3, red=2):
    return RawSignalProgram(
        junction_id=junction_id, link_edges=list(EDGES),
        phases=[{"state": "GGrr", "duration_s": g1}, {"state": "yyrr", "duration_s": yellow},
                {"state": "rrrr", "duration_s": red}, {"state": "rrGG", "duration_s": g2},
                {"state": "rryy", "duration_s": yellow}, {"state": "rrrr", "duration_s": red}])


def program(**kwargs):
    return build_signal_program(raw_program(**kwargs))


# phase indexes: 0 green1, 1 yellow1, 2 red1, 3 green2, 4 yellow2, 5 red2
def road(edge_id, queue_veh=0, junction_id="J1"):
    vehicles = queue_veh + 2
    return EdgeMetrics(
        edge_id=edge_id, junction_id=junction_id, length_m=LENGTH, lane_count=1, speed_limit_mps=10.0,
        vehicle_count=vehicles, density_veh_per_km=vehicles * 5.0, density_veh_per_km_lane=vehicles * 5.0,
        occupancy_pct=min(100.0, 5.0 + 4.0 * queue_veh), queue_length_veh=queue_veh,
        queue_length_m=min(LENGTH, 7.5 * queue_veh), mean_waiting_time_s=3.0 * queue_veh,
        mean_speed_mps=10.0 * max(0.0, 1.0 - 0.1 * queue_veh), speed_ratio=max(0.0, 1.0 - 0.1 * queue_veh),
        flow_veh_per_hour=300.0)


def snap(queues, phase=0, remaining=20.0, t=100.0, junction_id="J1"):
    """One junction J1 with roads A-D; ``queues`` = stopped cars per road (0 calm .. 8 jammed)."""
    edges = [road(e, queues.get(e, 0), junction_id) for e in EDGES]
    junction = JunctionMetrics(
        junction_id=junction_id, incoming_edges=list(EDGES), vehicle_count=sum(e.vehicle_count for e in edges),
        queue_length_veh=sum(e.queue_length_veh for e in edges),
        max_queue_length_m=max(e.queue_length_m for e in edges),
        mean_waiting_time_s=max(e.mean_waiting_time_s for e in edges),
        max_occupancy_pct=max(e.occupancy_pct for e in edges), flow_veh_per_hour=1200.0,
        current_phase=phase, phase_remaining_s=remaining)
    return TrafficSnapshot(
        timestamp_s=t, network=NetworkMetrics(total_vehicles=sum(e.vehicle_count for e in edges),
                                              halting_vehicles=0, mean_waiting_time_s=1.0, mean_speed_ratio=0.8),
        junctions=[junction], edges=edges)


def advisor(**kwargs):
    config = kwargs.pop("config", CFG)
    return SignalAdvisor({"J1": raw_program(**kwargs)}, config, UNSMOOTHED)


def recommend(adv, queues, **kwargs):
    return adv.recommend(snap(queues, **kwargs)).recommendations[0]


def stage_changes(rec):
    return {s.stage_id: s.change_s for s in rec.stages}


NS_BUSY = {"A": 8, "B": 6}        # stage 1 busy, stage 2 calm
EW_BUSY = {"C": 8, "D": 6}


# --------------------------------------------------------------------------- #
# program -> stages
# --------------------------------------------------------------------------- #
def test_stages_are_built_from_the_phases():
    p = program()
    assert [s.stage_id for s in p.stages] == [1, 2] and p.cycle_s == 70
    s1, s2 = p.stages
    assert s1.edges == ["A", "B"] and s2.edges == ["C", "D"]
    assert (s1.green_phase, s1.clearance_phases, s1.green_s, s1.yellow_s, s1.all_red_s) == (0, [1, 2], 30, 3, 2)
    assert (s2.green_phase, s2.clearance_phases) == (3, [4, 5])
    assert [ph.kind for ph in p.phases] == ["green", "yellow", "all_red", "green", "yellow", "all_red"]


def test_program_starting_with_clearance_phases_wraps_around():
    raw = raw_program()
    raw = RawSignalProgram(junction_id="J1", link_edges=raw.link_edges,
                           phases=raw.phases[2:] + raw.phases[:2])      # red, G2, y2, red, G1, y1
    p = build_signal_program(raw)
    assert [s.green_phase for s in p.stages] == [1, 4]
    assert p.stages[1].clearance_phases == [5, 0]


def test_program_without_green_is_rejected():
    raw = RawSignalProgram(junction_id="J1", link_edges=["A"], phases=[{"state": "r", "duration_s": 10}])
    with pytest.raises(InvalidTrafficDataError, match="no green"):
        build_signal_program(raw)


def test_phase_length_must_match_links():
    raw = RawSignalProgram(junction_id="J1", link_edges=["A", "B"], phases=[{"state": "G", "duration_s": 10}])
    with pytest.raises(InvalidTrafficDataError, match="signals but"):
        build_signal_program(raw)


def test_green_serving_no_road_is_rejected():
    raw = RawSignalProgram(junction_id="J1", link_edges=[None, "B"], phases=[{"state": "Gr", "duration_s": 10}])
    with pytest.raises(InvalidTrafficDataError, match="serves no road"):
        build_signal_program(raw)


def test_garbage_program_is_an_invalid_data_error():
    with pytest.raises(InvalidTrafficDataError):
        build_signal_program({"junction_id": "J1"})


# --------------------------------------------------------------------------- #
# safety validator
# --------------------------------------------------------------------------- #
def test_current_program_is_valid():
    rules = validate_plan(program(), {1: 30, 2: 30})
    assert {"every_stage_served", "min_green", "max_green", "max_cycle"} <= set(rules)
    assert "max_change_per_step" not in rules


def test_change_rule_is_checked_only_with_a_previous_plan():
    assert "max_change_per_step" in validate_plan(program(), {1: 35, 2: 25}, previous_greens={1: 30, 2: 30})


@pytest.mark.parametrize("greens,fragment", [
    ({1: 30}, "stage 2 is not served"),
    ({1: 30, 2: 0}, "stage 2 is not served"),
    ({1: 30, 2: float("nan")}, "stage 2 is not served"),
    ({1: 30, 2: 14}, "below the minimum of 15"),
    ({1: 46, 2: 14}, "above the maximum of 45"),
    ({1: 30, 2: 30, 3: 5}, "unknown stage"),
])
def test_unsafe_plans_are_rejected(greens, fragment):
    with pytest.raises(UnsafeSignalPlanError, match=fragment):
        validate_plan(program(), greens)


def test_a_stage_may_never_be_skipped_even_if_the_other_gets_all_the_time():
    with pytest.raises(UnsafeSignalPlanError, match="stage 2 is not served"):
        validate_plan(program(), {1: 60})


def test_clearance_can_not_be_shortened():
    p = program()
    with pytest.raises(UnsafeSignalPlanError, match="yellow 2 s is shorter"):
        validate_plan(p, {1: 30, 2: 30}, yellow_s={1: 2, 2: 3})
    with pytest.raises(UnsafeSignalPlanError, match="all-red 1 s is shorter"):
        validate_plan(p, {1: 30, 2: 30}, all_red_s={1: 2, 2: 1})
    validate_plan(p, {1: 30, 2: 30}, yellow_s={1: 4, 2: 4}, all_red_s={1: 3, 2: 3})   # longer is fine


def test_configured_floors_apply_even_if_the_program_is_shorter():
    p = program(yellow=2, red=1)
    with pytest.raises(UnsafeSignalPlanError, match="yellow"):
        validate_plan(p, {1: 30, 2: 30})


def test_cycle_limit():
    with pytest.raises(UnsafeSignalPlanError, match="cycle"):
        validate_plan(program(), {1: 44, 2: 44})         # 88 + 10 = 98 s > 90 s


def test_change_limit():
    with pytest.raises(UnsafeSignalPlanError, match="11 s, more than the 10 s"):
        validate_plan(program(), {1: 40, 2: 19}, previous_greens={1: 29, 2: 30})


def test_every_broken_rule_is_reported_at_once():
    with pytest.raises(UnsafeSignalPlanError) as info:
        validate_plan(program(), {1: 5, 2: 60})
    message = str(info.value)
    assert "below the minimum" in message and "above the maximum" in message


def test_error_is_structured():
    with pytest.raises(UnsafeSignalPlanError) as info:
        validate_plan(program(), {1: 30})
    assert info.value.to_dict()["code"] == "UNSAFE_SIGNAL_PLAN"


# --------------------------------------------------------------------------- #
# rebalancing
# --------------------------------------------------------------------------- #
def test_balanced_traffic_keeps_the_plan():
    rec = recommend(advisor(), {"A": 4, "C": 4})
    assert rec.action == "keep" and stage_changes(rec) == {1: 0, 2: 0}
    assert rec.safety.validated and "similar degree" in rec.reason or "balanced" in rec.reason


def test_light_traffic_everywhere_needs_no_change_even_if_uneven():
    rec = recommend(advisor(), {"A": 2})              # score ~0.2 vs ~0.03: uneven but light
    assert rec.action == "keep" and "light" in rec.reason and rec.priority == "none"


def test_reason_keeps_road_names_as_written():
    rec = recommend(advisor(), NS_BUSY)
    assert rec.reason.startswith("Stage 1 (A, B) is the busiest")


def test_uneven_traffic_moves_green_to_the_busier_stage():
    rec = recommend(advisor(), NS_BUSY)
    changes = stage_changes(rec)
    assert rec.action == "rebalance" and changes[1] > 0 and changes[2] == -changes[1]
    assert abs(changes[1]) <= CFG.max_change_s
    assert "stage 1 (A, B)" in rec.reason and "same" in rec.reason


def test_other_direction():
    changes = stage_changes(recommend(advisor(), EW_BUSY))
    assert changes[2] > 0 and changes[1] < 0


def test_change_is_gradual_even_if_demand_is_extreme():
    rec = recommend(advisor(), {"A": 8, "B": 8, "C": 0, "D": 0})
    assert stage_changes(rec)[1] == CFG.max_change_s


def test_cycle_length_is_unchanged_by_rebalancing():
    rec = recommend(advisor(), NS_BUSY)
    assert rec.cycle_s == 70 and sum(s.recommended_green_s for s in rec.stages) == 60


def test_clearance_times_are_reported_unchanged():
    rec = recommend(advisor(), NS_BUSY)
    assert (rec.yellow_s, rec.all_red_s) == (3, 2)


def test_greens_stay_within_the_limits_and_both_stages_keep_green():
    adv = advisor()
    for _ in range(10):                       # apply the advice repeatedly while stage 1 stays busy
        rec = recommend(adv, NS_BUSY)
        adv.set_current_plan("J1", {s.stage_id: s.recommended_green_s for s in rec.stages})
    plan = adv.current_plan("J1")
    assert plan[1] == CFG.max_green_s and plan[2] == 60 - CFG.max_green_s
    assert plan[2] >= CFG.min_green_s


def test_idempotent_until_the_plan_is_applied():
    adv = advisor()
    first = recommend(adv, NS_BUSY).stages
    second = recommend(adv, NS_BUSY, t=105.0).stages
    assert [s.recommended_green_s for s in first] == [s.recommended_green_s for s in second]


def test_next_recommendation_builds_on_the_applied_plan():
    adv = advisor()
    rec = recommend(adv, NS_BUSY)
    adv.set_current_plan("J1", {s.stage_id: s.recommended_green_s for s in rec.stages})
    again = recommend(adv, NS_BUSY, t=200.0)
    assert again.stages[0].current_green_s == rec.stages[0].recommended_green_s


def test_already_at_the_limit_means_no_change():
    adv = advisor()
    adv.set_current_plan("J1", {1: 45, 2: 15})
    rec = recommend(adv, NS_BUSY)
    assert rec.action == "keep" and stage_changes(rec) == {1: 0, 2: 0}


def test_high_demand_everywhere_says_so():
    rec = recommend(advisor(), {"A": 8, "B": 8, "C": 8, "D": 8})
    assert rec.action == "keep" and "would not help" in rec.reason and rec.priority == "high"


def test_set_current_plan_rejects_unsafe_plans_and_unknown_junctions():
    adv = advisor()
    with pytest.raises(UnsafeSignalPlanError):
        adv.set_current_plan("J1", {1: 60, 2: 5})
    with pytest.raises(InvalidTrafficDataError, match="No signal program"):
        adv.set_current_plan("X", {1: 30, 2: 30})
    assert adv.current_plan("J1") == {1: 30, 2: 30}          # unchanged after a rejected plan


def test_running_plan_outside_the_limits_is_left_alone():
    adv = SignalAdvisor({"J1": raw_program(g1=60, g2=10)}, CFG, UNSMOOTHED)
    rec = recommend(adv, NS_BUSY)
    assert rec.action == "keep" and not rec.safety.validated and "outside the safety limits" in rec.reason


def test_recommendations_are_always_safe_for_random_traffic():
    rng = random.Random(3)
    adv = advisor()
    program_ = adv.program("J1")
    for i in range(300):
        queues = {e: rng.randint(0, 9) for e in EDGES}
        phase, remaining = rng.choice([(0, rng.uniform(1, 30)), (1, 2), (2, 1), (3, rng.uniform(1, 30)), (4, 2), (5, 1)])
        rec = recommend(adv, queues, phase=phase, remaining=remaining, t=100.0 + 5 * i)
        plan = {s.stage_id: s.recommended_green_s for s in rec.stages}
        validate_plan(program_, plan, CFG, previous_greens=adv.current_plan("J1"))
        assert rec.safety.validated and rec.cycle_s <= CFG.max_cycle_s
        if rng.random() < 0.5:                         # sometimes the orchestrator applies it
            adv.set_current_plan("J1", plan)


# --------------------------------------------------------------------------- #
# action for the running phase
# --------------------------------------------------------------------------- #
def test_extend_a_green_that_is_about_to_end_while_traffic_is_heavy():
    rec = recommend(advisor(), NS_BUSY, phase=0, remaining=5)
    assert rec.immediate.action == "extend_green" and rec.immediate.extend_s == 15
    assert rec.phase_elapsed_s == 25
    assert "extend" in rec.reason


def test_green_is_never_extended_beyond_the_maximum_over_repeated_extensions():
    """Drive a whole green phase second by second, applying every extension, as an orchestrator would."""
    adv = advisor()
    remaining, now, total_extension = 30.0, 100.0, 0.0
    while remaining > 0:
        rec = adv.recommend(snap(NS_BUSY, phase=0, remaining=remaining, t=now)).recommendations[0]
        if rec.immediate.action == "extend_green":
            remaining += rec.immediate.extend_s
            total_extension += rec.immediate.extend_s
        remaining -= 5.0
        now += 5.0
    assert 0 < total_extension <= 15
    assert now - 100.0 <= CFG.max_green_s + 5          # the green (one sample late) stays within the maximum


def test_elapsed_green_counts_time_since_the_phase_started():
    adv = advisor()
    adv.recommend(snap(NS_BUSY, phase=0, remaining=30, t=100.0))
    rec = adv.recommend(snap(NS_BUSY, phase=0, remaining=20, t=110.0)).recommendations[0]
    assert rec.phase_elapsed_s == 10
    adv.recommend(snap(NS_BUSY, phase=0, remaining=25, t=115.0))     # 5 s were added to the green
    rec = adv.recommend(snap(NS_BUSY, phase=0, remaining=20, t=120.0)).recommendations[0]
    assert rec.phase_elapsed_s == 20                                  # real time, not plan - remaining


def test_no_extension_while_plenty_of_green_is_left():
    assert recommend(advisor(), NS_BUSY, phase=0, remaining=20).immediate.action == "none"


def test_extension_is_limited_by_the_maximum_green():
    adv = advisor()
    adv.set_current_plan("J1", {1: 40, 2: 20})
    rec = recommend(adv, NS_BUSY, phase=0, remaining=3)          # 37 s run + 3 left = 40; 5 s of room
    assert rec.immediate.action == "extend_green" and rec.immediate.extend_s == 5


def test_no_extension_at_the_maximum_green():
    adv = advisor()
    adv.set_current_plan("J1", {1: 45, 2: 15})
    assert recommend(adv, NS_BUSY, phase=0, remaining=3).immediate.action == "none"


def test_no_extension_if_the_other_stage_is_just_as_busy():
    assert recommend(advisor(), {"A": 8, "C": 8}, phase=0, remaining=4).immediate.action == "none"


def test_no_extension_for_the_quieter_stage():
    assert recommend(advisor(), NS_BUSY, phase=3, remaining=4).immediate.action != "extend_green"


def test_end_an_idle_green_early_when_another_stage_waits():
    rec = recommend(advisor(), EW_BUSY, phase=0, remaining=12)    # elapsed 18 s >= minimum green
    assert rec.immediate.action == "end_green_early" and "yellow and all-red still follow" in rec.immediate.reason


def test_never_end_a_green_before_its_minimum():
    rec = recommend(advisor(), EW_BUSY, phase=0, remaining=22)    # only 8 s elapsed
    assert rec.immediate.action == "none"


def test_never_end_early_if_nobody_is_waiting():
    assert recommend(advisor(), {}, phase=0, remaining=12).immediate.action == "none"


@pytest.mark.parametrize("phase", [1, 2, 4, 5])
def test_yellow_and_all_red_are_never_touched(phase):
    rec = recommend(advisor(), NS_BUSY, phase=phase, remaining=1)
    assert rec.immediate.action == "in_clearance" and rec.immediate.extend_s == 0


def test_unknown_phase_gives_no_immediate_action():
    snapshot = snap(NS_BUSY)
    snapshot.junctions[0].current_phase = None
    rec = advisor().recommend(snapshot).recommendations[0]
    assert rec.immediate.action == "none" and rec.phase_elapsed_s is None


# --------------------------------------------------------------------------- #
# priority, predictions, inputs
# --------------------------------------------------------------------------- #
def test_priority_follows_the_congestion_level():
    assert recommend(advisor(), {}).priority == "none"
    assert recommend(advisor(), {"A": 4, "C": 4}).priority == "medium"
    assert recommend(advisor(), NS_BUSY).priority == "high"


def prediction(predicted, method="ml", current="LOW"):
    probs = {"LOW": 0.0, "MEDIUM": 0.0, "HIGH": 0.0}
    probs[predicted] = 1.0
    return PredictionReport(
        timestamp_s=100.0, horizon_s=60, method=method, history_available_s=120.0, early_warnings=[],
        predictions=[JunctionPrediction(
            junction_id="J1", current_level=current, current_score=0.1, predicted_level=predicted,
            probabilities=probs, confidence=1.0, expected_change="worsening", method=method)])


def test_a_forecast_of_worsening_raises_the_priority():
    adv = advisor()
    rec = adv.recommend(snap({}), prediction=prediction("HIGH")).recommendations[0]
    assert rec.level is CongestionLevel.LOW and rec.predicted_level is CongestionLevel.HIGH
    assert rec.priority == "high"


def test_a_fallback_prediction_is_ignored_with_a_note():
    report = advisor().recommend(snap({}), prediction=prediction("LOW", method="persistence"))
    assert report.recommendations[0].predicted_level is None
    assert any("fallback" in n for n in report.notes)


def test_no_prediction_is_noted():
    assert any("No prediction" in n for n in advisor().recommend(snap({})).notes)


def test_summary_counts():
    adv = advisor()
    report = adv.recommend(snap(NS_BUSY, phase=0, remaining=5))
    assert (report.summary.junctions_rebalance, report.summary.junctions_keep) == (1, 0)
    assert report.summary.immediate_actions == 1 and report.mode == "recommend_only"


def test_junction_without_a_program_is_skipped_with_a_note():
    snapshot = snap(NS_BUSY, junction_id="J9")
    report = advisor().recommend(snapshot)
    assert report.recommendations == [] and any("J9 skipped" in n for n in report.notes)


def test_report_is_json_serialisable():
    text = json.dumps(advisor().recommend(snap(NS_BUSY, remaining=5)).to_dict())
    assert json.loads(text)["recommendations"][0]["safety"]["validated"] is True


def test_report_and_snapshot_must_be_from_the_same_time():
    adv = advisor()
    other = CongestionDetector(UNSMOOTHED).update(snap(NS_BUSY, t=50.0))
    with pytest.raises(InvalidTrafficDataError, match="different times"):
        adv.recommend(snap(NS_BUSY, t=100.0), other)


def test_garbage_input_is_an_invalid_data_error():
    with pytest.raises(InvalidTrafficDataError):
        advisor().recommend({"nonsense": 1})


def test_a_given_congestion_report_is_used():
    adv = advisor()
    s = snap(NS_BUSY)
    report = CongestionDetector(UNSMOOTHED).update(s)
    assert adv.recommend(s, report).recommendations[0].action == "rebalance"


def test_program_given_under_the_wrong_name():
    with pytest.raises(InvalidTrafficDataError, match="under the name"):
        SignalAdvisor({"J2": raw_program("J1")})


def test_reset_restores_the_original_plans():
    adv = advisor()
    adv.set_current_plan("J1", {1: 40, 2: 20})
    adv.reset()
    assert adv.current_plan("J1") == {1: 30, 2: 30}


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kwargs", [
    {"min_green_s": 2}, {"min_green_s": 20, "max_green_s": 10}, {"max_change_s": 0},
    {"min_yellow_s": 2}, {"max_cycle_s": 39}, {"imbalance_deadband": 0.0}, {"imbalance_deadband": 1.0}, {"min_demand_to_act": 1.0},
    {"extend_step_s": 10, "max_extension_s": 5}, {"end_early_demand": 1.0},
])
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        SignalConfig(**kwargs)