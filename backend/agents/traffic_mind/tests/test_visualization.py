"""Tests for the Matplotlib dashboard. Everything is drawn off screen (no window, no SUMO)."""

import pandas as pd
import pytest
from matplotlib.figure import Figure

from backend.agents.traffic_mind.config import CongestionConfig, SignalConfig
from backend.agents.traffic_mind.congestion_detector import CongestionDetector
from backend.agents.traffic_mind.exceptions import InvalidTrafficDataError
from backend.agents.traffic_mind.schemas import (
    EdgeMetrics,
    JunctionMetrics,
    JunctionPrediction,
    NetworkMetrics,
    PredictionReport,
    RawSignalProgram,
    TrafficSnapshot,
)
from backend.agents.traffic_mind.signal_controller import SignalAdvisor
from backend.agents.traffic_mind.visualization import (
    LiveDashboard,
    RunRecorder,
    build_dashboard,
    busiest_junction,
    junction_positions,
    save_dashboard,
)

UNSMOOTHED = CongestionConfig(smoothing_window_s=1, escalate_after_s=0, clear_after_s=0, attention_medium_after_s=0)
LENGTH = 200.0
EDGES = ("A", "B", "C", "D")
PROGRAM = RawSignalProgram(
    junction_id="J1", link_edges=list(EDGES),
    phases=[{"state": "GGrr", "duration_s": 30}, {"state": "yyrr", "duration_s": 3}, {"state": "rrrr", "duration_s": 2},
            {"state": "rrGG", "duration_s": 30}, {"state": "rryy", "duration_s": 3}, {"state": "rrrr", "duration_s": 2}])


def road(edge_id, queue_veh, junction_id):
    vehicles = queue_veh + 2
    return EdgeMetrics(
        edge_id=edge_id, junction_id=junction_id, length_m=LENGTH, lane_count=1, speed_limit_mps=10.0,
        vehicle_count=vehicles, density_veh_per_km=vehicles * 5.0, density_veh_per_km_lane=vehicles * 5.0,
        occupancy_pct=min(100.0, 5.0 + 4.0 * queue_veh), queue_length_veh=queue_veh,
        queue_length_m=min(LENGTH, 7.5 * queue_veh), mean_waiting_time_s=3.0 * queue_veh,
        mean_speed_mps=10.0 * max(0.0, 1.0 - 0.1 * queue_veh), speed_ratio=max(0.0, 1.0 - 0.1 * queue_veh),
        flow_veh_per_hour=300.0)


def snap(t, queue=0, phase=0, remaining=20.0):
    edges = [road(e, queue if e in "AB" else 0, "J1") for e in EDGES]
    junction = JunctionMetrics(
        junction_id="J1", incoming_edges=list(EDGES), vehicle_count=sum(e.vehicle_count for e in edges),
        queue_length_veh=sum(e.queue_length_veh for e in edges), max_queue_length_m=max(e.queue_length_m for e in edges),
        mean_waiting_time_s=max(e.mean_waiting_time_s for e in edges), max_occupancy_pct=max(e.occupancy_pct for e in edges),
        flow_veh_per_hour=1200.0, current_phase=phase, phase_remaining_s=remaining)
    return TrafficSnapshot(
        timestamp_s=t, network=NetworkMetrics(total_vehicles=sum(e.vehicle_count for e in edges),
                                              halting_vehicles=queue * 2, mean_waiting_time_s=1.0, mean_speed_ratio=0.8),
        junctions=[junction], edges=edges)


def prediction(t, predicted, horizon=60, method="ml"):
    probs = {"LOW": 0.0, "MEDIUM": 0.0, "HIGH": 0.0, predicted: 1.0}
    return PredictionReport(
        timestamp_s=t, horizon_s=horizon, method=method, history_available_s=120.0, early_warnings=[],
        predictions=[JunctionPrediction(junction_id="J1", current_level="LOW", current_score=0.1,
                                        predicted_level=predicted, probabilities=probs, confidence=1.0,
                                        expected_change="worsening", method=method)])


def recorded(samples=40, with_forecast=True, with_signals=True):
    """Queue grows after t=100 s; forecasts are always right (they predict the level 60 s ahead)."""
    detector = CongestionDetector(UNSMOOTHED)
    advisor = SignalAdvisor({"J1": PROGRAM}, SignalConfig(), UNSMOOTHED)
    queue_at = lambda t: 0 if t < 100 else 8
    rec = RunRecorder("test")
    for i in range(1, samples + 1):
        t = 5.0 * i
        s = snap(t, queue_at(t), remaining=20 - (i % 3))
        congestion = detector.update(s)
        later = "HIGH" if t + 60 >= 100 else "LOW"
        rec.add(s, congestion, prediction(t, later) if with_forecast else None,
                advisor.recommend(s, congestion) if with_signals else None)
    return rec


# --------------------------------------------------------------------------- #
# recorder
# --------------------------------------------------------------------------- #
def test_recorder_keeps_one_row_per_sample_and_junction():
    rec = recorded(10)
    assert len(rec) == 10
    network, junctions = rec.network_frame(), rec.junction_frame()
    assert list(network.columns) == ["time_s", "vehicles", "stopped", "mean_wait_s"] and len(network) == 10
    assert len(junctions) == 10 and set(junctions["junction_id"]) == {"J1"}
    assert rec.horizon_s == 60 and rec.junction_ids == ["J1"] and rec.latest_time_s == 50.0


def test_recorder_stores_levels_forecasts_and_signal_advice():
    j = recorded(40).junction_frame()
    assert j.iloc[0]["level"] == "LOW" and j.iloc[-1]["level"] == "HIGH"
    assert j.iloc[0]["predicted_level"] == "LOW" and j.iloc[10]["predicted_level"] == "HIGH"   # t=5 s vs t=55 s
    assert (j["shift_s"].dropna().abs() > 0).any() and (j["action"] == "rebalance").any()


def test_forecasts_and_signals_are_optional():
    j = recorded(10, with_forecast=False, with_signals=False).junction_frame()
    assert j["predicted_level"].isna().all() and j["shift_s"].isna().all() and j["action"].isna().all()


def test_fallback_predictions_are_not_recorded():
    rec = RunRecorder()
    s = snap(5.0)
    rec.add(s, CongestionDetector(UNSMOOTHED).update(s), prediction(5.0, "LOW", method="persistence"))
    assert rec.horizon_s is None and rec.junction_frame()["predicted_level"].isna().all()


def test_reports_must_match_the_snapshot_time():
    rec, detector = RunRecorder(), CongestionDetector(UNSMOOTHED)
    with pytest.raises(InvalidTrafficDataError, match="congestion report"):
        rec.add(snap(5.0), detector.update(snap(10.0)))
    s = snap(15.0)
    with pytest.raises(InvalidTrafficDataError, match="prediction report"):
        rec.add(s, CongestionDetector(UNSMOOTHED).update(s), prediction(20.0, "LOW"))


def test_samples_must_be_in_time_order():
    rec = RunRecorder()
    s2, s1 = snap(10.0), snap(5.0)
    rec.add(s2, CongestionDetector(UNSMOOTHED).update(s2))
    with pytest.raises(InvalidTrafficDataError, match="time order"):
        rec.add(s1, CongestionDetector(UNSMOOTHED).update(s1))


def test_garbage_input_is_an_invalid_data_error():
    with pytest.raises(InvalidTrafficDataError):
        RunRecorder().add({"x": 1}, {"y": 2})


def test_csv_export(tmp_path):
    paths = recorded(8).save_csv(tmp_path / "out")
    assert [p.name for p in paths] == ["network.csv", "junctions.csv"]
    assert len(pd.read_csv(paths[0])) == 8 and "predicted_level" in pd.read_csv(paths[1]).columns


def test_forecast_accuracy_counts_matches_against_the_level_reached():
    rec = recorded(40)                       # forecasts are right whenever the target time was recorded
    accuracy, checked = rec.forecast_accuracy("J1")
    assert accuracy == 1.0 and checked == 40 - 12          # the last 60 s (12 samples) have no outcome yet
    assert recorded(10, with_forecast=False).forecast_accuracy("J1") is None


def test_forecast_accuracy_drops_when_forecasts_are_wrong():
    detector, rec = CongestionDetector(UNSMOOTHED), RunRecorder()
    for i in range(1, 31):
        s = snap(5.0 * i, 0)                                 # always calm, but "HIGH" is forecast
        rec.add(s, detector.update(s), prediction(5.0 * i, "HIGH"))
    assert rec.forecast_accuracy("J1")[0] == 0.0


# --------------------------------------------------------------------------- #
# layout
# --------------------------------------------------------------------------- #
def test_grid_positions_come_from_the_junction_names():
    pos = junction_positions(["A0", "B1", "C2"])
    assert pos == {"A0": (0.0, 0.0), "B1": (1.0, 1.0), "C2": (2.0, 2.0)}


def test_odd_junction_names_are_laid_out_on_a_line():
    pos = junction_positions(["north", "south"])
    assert sorted(pos.values()) == [(0.0, 0.0), (1.0, 0.0)]


def test_busiest_junction_is_the_one_with_most_time_at_high():
    assert busiest_junction(recorded(40)) == "J1"
    assert busiest_junction(RunRecorder()) is None


# --------------------------------------------------------------------------- #
# static dashboard
# --------------------------------------------------------------------------- #
def titles(fig):
    return {ax.get_title(loc="left") for ax in fig.axes}


def test_dashboard_has_all_six_panels():
    fig = build_dashboard(recorded(40))
    assert isinstance(fig, Figure)
    assert {"Network now", "Vehicles in the network", "Congestion by junction", "Recommended green shift",
            "Forecast vs actual, junction J1  -  100% of 28 forecasts matched", "Time spent congested"} == titles(fig) - {""}


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_dashboard_saves_a_png(tmp_path, theme):
    path = save_dashboard(recorded(40), tmp_path / "sub" / "dash.png", theme=theme, dpi=60)
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 10_000


def test_dashboard_renders_without_forecasts_or_signals(tmp_path):
    rec = recorded(20, with_forecast=False, with_signals=False)
    fig = build_dashboard(rec)
    texts = {t.get_text() for ax in fig.axes for t in ax.texts}
    assert any("No forecasts recorded" in t for t in texts)
    assert any("No signal recommendations recorded" in t for t in texts)
    save_dashboard(rec, tmp_path / "d.png", dpi=50)


def test_dashboard_renders_a_single_sample():
    fig = build_dashboard(recorded(1))
    assert len(fig.axes) >= 6


def test_map_can_show_an_earlier_moment():
    rec = recorded(40)
    early = build_dashboard(rec, at_s=50.0)
    late = build_dashboard(rec)
    def map_text(fig):
        return " ".join(t.get_text() for t in fig.axes[0].texts)
    assert "J1  Low" in map_text(early) and "J1  High" in map_text(late)


def test_forecast_ring_appears_only_when_the_forecast_is_worse():
    rec = recorded(40)
    legend = build_dashboard(rec, at_s=50.0).axes[0].get_legend()           # LOW now, HIGH forecast
    assert any("forecast worse" in t.get_text() for t in legend.get_texts())
    legend = build_dashboard(rec).axes[0].get_legend()                      # HIGH now: nothing worse
    assert not any("forecast worse" in t.get_text() for t in legend.get_texts())


def test_unknown_junction_theme_and_empty_recorder():
    with pytest.raises(InvalidTrafficDataError, match="Unknown junction"):
        build_dashboard(recorded(5), junction="Z9")
    with pytest.raises(ValueError, match="theme"):
        build_dashboard(recorded(5), theme="neon")
    with pytest.raises(InvalidTrafficDataError, match="Nothing recorded"):
        build_dashboard(RunRecorder())


def test_status_colours_are_never_the_only_cue():
    fig = build_dashboard(recorded(40))
    legend_markers = {line.get_marker() for line in fig.axes[0].get_legend().get_lines()}
    assert {"o", "s", "^"} <= legend_markers                               # a shape for every level
    assert any(t.get_text().startswith("J1  High") for t in fig.axes[0].texts)   # and a text label


# --------------------------------------------------------------------------- #
# live view
# --------------------------------------------------------------------------- #
def test_live_dashboard_draws_and_saves_frames(tmp_path):
    rec = RunRecorder("live")
    live = LiveDashboard(rec, show=False)
    detector = CongestionDetector(UNSMOOTHED)
    advisor = SignalAdvisor({"J1": PROGRAM}, SignalConfig(), UNSMOOTHED)
    for i in range(1, 30):
        s = snap(5.0 * i, 0 if i < 10 else 8, remaining=10)
        congestion = detector.update(s)
        rec.add(s, congestion, None, advisor.recommend(s, congestion))
        live.refresh()
    frame = live.save_frame(tmp_path / "frame.png")
    assert frame.read_bytes()[:4] == b"\x89PNG"
    advice = " ".join(t.get_text() for t in live.ax_text.texts)
    assert "J1" in advice and "green" in advice


def test_live_dashboard_shows_only_the_last_ten_minutes():
    rec = RunRecorder()
    live = LiveDashboard(rec, show=False)
    detector = CongestionDetector(UNSMOOTHED)
    for i in range(1, 400):                                # 33 minutes
        s = snap(5.0 * i, 0)
        rec.add(s, detector.update(s))
    live.refresh()
    left, right = live.ax_series.get_xlim()
    assert right - left < 12 and right > 30


def test_live_dashboard_before_any_data():
    live = LiveDashboard(RunRecorder(), show=False)
    live.refresh()                                          # must not raise
    assert "No data yet" in {t.get_text() for t in live.ax_map.texts}