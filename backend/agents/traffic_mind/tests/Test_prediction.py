"""Tests for features, baselines, the runtime predictor and training. No SUMO needed."""

import json

import numpy as np
import pandas as pd
import pytest

from backend.agents.traffic_mind.config import CongestionConfig, PredictionConfig
from backend.agents.traffic_mind.congestion_detector import CongestionDetector
from backend.agents.traffic_mind.exceptions import (
    InvalidTrafficDataError,
    ModelLoadError,
    PredictionError,
)
from backend.agents.traffic_mind.prediction_training import (
    add_labels,
    dataset_columns,
    evaluate_predictions,
    format_comparison,
    label_column,
    split_runs,
    train_for_horizon,
)
from backend.agents.traffic_mind.schemas import (
    EdgeMetrics,
    JunctionMetrics,
    NetworkMetrics,
    TrafficSnapshot,
)
from backend.agents.traffic_mind.traffic_predictor import (
    FeatureBuilder,
    ModelBundle,
    TrafficPredictor,
    classify_scores,
    feature_columns,
    make_model_info,
    persistence_levels,
    trend_levels,
    trend_scores,
)

UNSMOOTHED = CongestionConfig(smoothing_window_s=1, escalate_after_s=0, clear_after_s=0,
                              attention_medium_after_s=0)
CFG = PredictionConfig()
LENGTH = 200.0


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def road(edge_id, junction_id, queue_veh=0):
    """An approach road; more stopped cars -> busier."""
    vehicles = queue_veh + 2
    return EdgeMetrics(
        edge_id=edge_id, junction_id=junction_id, length_m=LENGTH, lane_count=1, speed_limit_mps=10.0,
        vehicle_count=vehicles, density_veh_per_km=vehicles * 5.0, density_veh_per_km_lane=vehicles * 5.0,
        occupancy_pct=min(100.0, 5.0 + 4.0 * queue_veh), queue_length_veh=queue_veh,
        queue_length_m=min(LENGTH, 7.5 * queue_veh), mean_waiting_time_s=3.0 * queue_veh,
        mean_speed_mps=10.0 * max(0.0, 1.0 - 0.1 * queue_veh), speed_ratio=max(0.0, 1.0 - 0.1 * queue_veh),
        flow_veh_per_hour=300.0,
    )


def snap(t, queues):
    """Snapshot with one junction (and one road) per entry of ``queues`` = {junction_id: queued cars}."""
    edges = [road(f"{j}_in", j, q) for j, q in queues.items()]
    junctions = [
        JunctionMetrics(
            junction_id=e.junction_id, incoming_edges=[e.edge_id], vehicle_count=e.vehicle_count,
            queue_length_veh=e.queue_length_veh, max_queue_length_m=e.queue_length_m,
            mean_waiting_time_s=e.mean_waiting_time_s, max_occupancy_pct=e.occupancy_pct,
            flow_veh_per_hour=e.flow_veh_per_hour, current_phase=0, phase_remaining_s=10.0)
        for e in edges
    ]
    return TrafficSnapshot(
        timestamp_s=t,
        network=NetworkMetrics(total_vehicles=sum(e.vehicle_count for e in edges),
                               halting_vehicles=sum(e.queue_length_veh for e in edges),
                               mean_waiting_time_s=1.0, mean_speed_ratio=0.8),
        junctions=junctions, edges=edges)


def feed(builder, detector, times, queues_at):
    for t in times:
        s = snap(t, queues_at(t))
        builder.update(s, detector.update(s))


def new_builder():
    return FeatureBuilder(CFG, UNSMOOTHED), CongestionDetector(UNSMOOTHED)


class StubEstimator:
    """Stands in for a trained classifier: always says the given probabilities."""

    def __init__(self, proba, classes=("HIGH", "LOW", "MEDIUM")):
        self.proba, self.classes_ = proba, np.array(classes)

    def predict_proba(self, x):
        return np.tile(np.asarray(self.proba, dtype=float), (len(x), 1))


def bundle_with(proba, horizon=60, columns=None, classes=("HIGH", "LOW", "MEDIUM")):
    cols = columns if columns is not None else feature_columns(CFG)
    return ModelBundle(
        estimator=StubEstimator(proba, classes), classes=list(classes), feature_columns=list(cols),
        horizon_s=horizon, info=make_model_info("stub", horizon, len(cols)),
        congestion_config={"medium_threshold": 0.25, "high_threshold": 0.50, "smoothing_window_s": 1})


def predictor(**kwargs):
    return TrafficPredictor(CFG, UNSMOOTHED, **kwargs)


# --------------------------------------------------------------------------- #
# features
# --------------------------------------------------------------------------- #
def test_feature_columns_are_unique_and_stable():
    cols = feature_columns(CFG)
    assert len(cols) == len(set(cols)) == 35
    assert "slope60_score" in cols and "d30_score" in cols


def test_history_ok_only_after_enough_history():
    builder, detector = new_builder()
    flags = {}
    for t in range(5, 125, 5):
        s = snap(t, {"J1": 3})
        builder.update(s, detector.update(s))
        flags[t] = builder.feature_rows()[0]["history_ok"]
    assert not flags[60]          # only 55 s of history
    assert flags[65] and flags[120]


def test_lag_features_are_differences_to_the_past():
    builder, detector = new_builder()
    feed(builder, detector, range(5, 125, 5), lambda t: {"J1": 0 if t <= 90 else 6})
    row = builder.feature_rows()[0]
    assert row["queue_veh"] == 6
    assert row["d30_queue_veh"] == 6 - 0       # at t=90 the queue was still 0
    assert row["d60_queue_veh"] == 6 - 0


def test_slope_is_positive_for_a_building_queue_and_negative_for_a_clearing_one():
    builder, detector = new_builder()
    feed(builder, detector, range(5, 125, 5), lambda t: {"J1": min(12, t // 10)})
    assert builder.feature_rows()[0]["slope60_score"] > 0
    builder, detector = new_builder()
    feed(builder, detector, range(5, 125, 5), lambda t: {"J1": max(0, 12 - t // 10)})
    assert builder.feature_rows()[0]["slope60_score"] < 0


def test_features_have_no_missing_values_once_history_is_complete():
    builder, detector = new_builder()
    feed(builder, detector, range(5, 125, 5), lambda t: {"J1": 2, "J2": 5})
    frame = builder.features()
    assert frame["history_ok"].all() and len(frame) == 2
    assert not frame[feature_columns(CFG)].isna().any().any()


def test_features_before_enough_history_are_nan_not_zero():
    builder, detector = new_builder()
    feed(builder, detector, [5, 10], lambda t: {"J1": 2})
    row = builder.feature_rows()[0]
    assert not row["history_ok"] and np.isnan(row["d60_score"]) and np.isnan(row["slope60_score"])


def test_history_is_pruned():
    builder, detector = new_builder()
    feed(builder, detector, range(5, 1000, 5), lambda t: {"J1": 2})
    assert len(builder._history["J1"]) <= CFG.history_s // CFG.sample_interval_s + 2


def test_snapshot_and_report_must_match():
    builder, detector = new_builder()
    a, b = snap(5, {"J1": 1}), snap(10, {"J1": 1})
    with pytest.raises(InvalidTrafficDataError, match="different times"):
        builder.update(a, detector.update(b))


def test_report_missing_a_junction_is_rejected_and_nothing_is_stored():
    builder, detector = new_builder()
    s = snap(5, {"J1": 1, "J2": 1})
    report = detector.update(snap(5, {"J1": 1}))
    with pytest.raises(InvalidTrafficDataError, match="no entry"):
        builder.update(s, report)
    assert builder.latest_time is None


def test_garbage_input_is_an_invalid_data_error():
    builder, _ = new_builder()
    with pytest.raises(InvalidTrafficDataError):
        builder.update({"nonsense": 1}, {"nonsense": 1})


def test_time_going_backwards_resets_history():
    builder, detector = new_builder()
    feed(builder, detector, range(5, 100, 5), lambda t: {"J1": 2})
    s = snap(5, {"J1": 2})
    builder.update(s, CongestionDetector(UNSMOOTHED).update(s))
    assert builder.history_available_s == 0.0


# --------------------------------------------------------------------------- #
# baselines
# --------------------------------------------------------------------------- #
def test_classify_scores_uses_detector_thresholds():
    out = classify_scores([0.0, 0.249, 0.25, 0.499, 0.5, 1.0])
    assert list(out) == ["LOW", "LOW", "MEDIUM", "MEDIUM", "HIGH", "HIGH"]


def _frame(score, slope, level="LOW"):
    return pd.DataFrame({"score": score, "slope60_score": slope, "current_level": level})


def test_persistence_baseline_returns_the_current_level():
    assert list(persistence_levels(_frame([0.1, 0.6], [0, 0], ["LOW", "HIGH"]))) == ["LOW", "HIGH"]


def test_trend_baseline_extrapolates_the_slope():
    frame = _frame([0.30], [0.10])           # +0.10 per minute
    assert trend_scores(frame, 60)[0] == pytest.approx(0.40)
    assert trend_scores(frame, 120, gain=2.0)[0] == pytest.approx(0.70)
    assert trend_levels(frame, 120)[0] == "HIGH"


def test_trend_baseline_is_clipped_and_gain_zero_is_persistence():
    assert trend_scores(_frame([0.9], [1.0]), 120)[0] == 1.0
    assert trend_scores(_frame([0.1], [-1.0]), 120)[0] == 0.0
    assert trend_scores(_frame([0.33], [0.2]), 60, gain=0.0)[0] == pytest.approx(0.33)


def test_trend_baseline_tolerates_missing_slope():
    assert trend_scores(_frame([0.3], [np.nan]), 60)[0] == pytest.approx(0.3)


# --------------------------------------------------------------------------- #
# runtime predictor
# --------------------------------------------------------------------------- #
def warmed(pred, queues=None, until=125):
    for t in range(5, until, 5):
        report = pred.predict(snap(t, queues or {"J1": 2}))
    return report


def test_predict_before_any_data_is_an_error():
    with pytest.raises(PredictionError, match="No traffic data"):
        predictor().predict()


def test_without_a_model_it_falls_back_to_persistence_and_says_so():
    report = warmed(predictor())
    assert report.method == "persistence" and report.model is None
    assert report.predictions[0].method == "persistence"
    assert report.predictions[0].predicted_level == report.predictions[0].current_level
    assert report.predictions[0].probabilities[report.predictions[0].current_level.value] == 1.0
    assert any("No trained model" in n for n in report.notes)
    assert report.early_warnings == []


def test_with_too_little_history_it_falls_back_even_with_a_model():
    pred = predictor()
    pred.add_model(bundle_with([0.9, 0.05, 0.05]))
    report = pred.predict(snap(5, {"J1": 2}))
    assert report.method == "persistence"
    assert any("of history" in n for n in report.notes)


def test_model_predictions_and_early_warnings():
    pred = predictor()
    pred.add_model(bundle_with([0.7, 0.1, 0.2]))          # HIGH 0.7
    report = warmed(pred, {"J1": 0, "J2": 0})
    assert report.method == "ml" and report.model.name == "stub"
    p = report.predictions[0]
    assert p.predicted_level.value == "HIGH" and p.expected_change == "worsening"
    assert p.confidence == pytest.approx(0.7)
    assert p.probabilities == {"LOW": 0.1, "MEDIUM": 0.2, "HIGH": 0.7}
    assert [w.junction_id for w in report.early_warnings] == ["J1", "J2"]
    assert "HIGH expected in 60 s" in report.early_warnings[0].message


def test_improving_and_steady_changes():
    pred = predictor()
    pred.add_model(bundle_with([0.05, 0.9, 0.05]))        # LOW predicted
    report = warmed(pred, {"J1": 8})                       # busy now
    assert report.predictions[0].expected_change == "improving" and report.early_warnings == []
    pred = predictor()
    pred.add_model(bundle_with([0.05, 0.9, 0.05]))
    assert warmed(pred, {"J1": 0}).predictions[0].expected_change == "steady"


def test_probabilities_follow_the_class_order_of_the_model():
    pred = predictor()
    pred.add_model(bundle_with([0.2, 0.3, 0.5], classes=("LOW", "MEDIUM", "HIGH")))
    p = warmed(pred, {"J1": 0}).predictions[0]
    assert p.probabilities == {"LOW": 0.2, "MEDIUM": 0.3, "HIGH": 0.5}


def test_report_is_json_serialisable():
    pred = predictor()
    pred.add_model(bundle_with([0.6, 0.3, 0.1]))
    text = json.dumps(warmed(pred, {"J1": 0}).to_dict())
    assert json.loads(text)["status"] == "ok"


def test_other_horizon_without_a_model_falls_back():
    pred = predictor()
    pred.add_model(bundle_with([0.9, 0.05, 0.05]))
    pred.update(snap(5, {"J1": 0}))
    report = pred.predict(horizon_s=120)
    assert report.horizon_s == 120 and report.method == "persistence"


@pytest.mark.parametrize("bad", [0, -5, 1.5, "60", True])
def test_invalid_horizon_is_rejected(bad):
    pred = predictor()
    pred.update(snap(5, {"J1": 0}))
    with pytest.raises(PredictionError, match="horizon_s"):
        pred.predict(horizon_s=bad)


def test_model_trained_on_other_features_is_refused():
    with pytest.raises(ModelLoadError, match="different features"):
        predictor().add_model(bundle_with([1, 0, 0], columns=["a", "b"]))


def test_model_trained_with_other_thresholds_adds_a_note():
    pred = predictor()
    b = bundle_with([0.1, 0.8, 0.1])
    b.congestion_config = {"medium_threshold": 0.4}
    pred.add_model(b)
    pred.update(snap(5, {"J1": 0}))
    assert any("different congestion settings" in n for n in pred.predict().notes)


def test_model_from_another_scikit_learn_version_adds_a_note():
    pred = predictor()
    b = bundle_with([0.1, 0.8, 0.1])
    b.sklearn_version = "0.20.0"
    pred.add_model(b)
    pred.update(snap(5, {"J1": 0}))
    assert any("scikit-learn 0.20.0" in n for n in pred.predict().notes)


def test_broken_estimator_gives_a_prediction_error():
    class Broken:
        classes_ = np.array(["HIGH", "LOW", "MEDIUM"])

        def predict_proba(self, x):
            raise RuntimeError("boom")

    pred = predictor()
    b = bundle_with([1, 0, 0])
    b.estimator = Broken()
    pred.add_model(b)
    with pytest.raises(PredictionError, match="boom"):
        warmed(pred)


def test_predictor_reset_forgets_history():
    pred = predictor()
    warmed(pred)
    pred.reset()
    with pytest.raises(PredictionError):
        pred.predict()


def test_uses_a_given_congestion_report_instead_of_its_own():
    detector = CongestionDetector(UNSMOOTHED)
    pred = predictor(detector=detector)
    s = snap(5, {"J1": 3})
    report = detector.update(s)
    pred.update(s, report)                  # must not call the detector a second time
    assert pred.predict().timestamp_s == 5


# --------------------------------------------------------------------------- #
# model files
# --------------------------------------------------------------------------- #
def _tiny_real_bundle():
    from sklearn.linear_model import LogisticRegression
    cols = feature_columns(CFG)
    rng = np.random.RandomState(0)
    x = pd.DataFrame(rng.rand(60, len(cols)), columns=cols)
    y = np.array(["LOW", "MEDIUM", "HIGH"] * 20)
    est = LogisticRegression(max_iter=200).fit(x, y)
    return ModelBundle(est, [str(c) for c in est.classes_], cols, 60,
                       make_model_info("logreg", 60, len(cols)), {"medium_threshold": 0.25})


def test_save_and_load_round_trip(tmp_path):
    path = _tiny_real_bundle().save(tmp_path / "m" / "traffic_model_h60.joblib")
    pred = predictor()
    assert pred.load_models(tmp_path / "m") == [60]
    report = warmed(pred, {"J1": 1})
    assert report.method == "ml"
    assert sum(report.predictions[0].probabilities.values()) == pytest.approx(1.0)
    assert ModelBundle.load(path).horizon_s == 60


def test_missing_model_file_and_folder():
    with pytest.raises(ModelLoadError, match="not found"):
        ModelBundle.load("does/not/exist.joblib")
    assert predictor().load_models("does/not/exist") == []      # not an error


def test_corrupt_model_file(tmp_path):
    bad = tmp_path / "traffic_model_h60.joblib"
    bad.write_text("not a model")
    with pytest.raises(ModelLoadError, match="Could not read"):
        predictor().load_models(tmp_path)


# --------------------------------------------------------------------------- #
# training helpers
# --------------------------------------------------------------------------- #
def test_add_labels_looks_up_the_future_level_of_the_same_junction():
    rows = [{"junction_id": j, "time_s": t, "current_level": lv}
            for j, levels in {"A": ["LOW", "LOW", "HIGH", "HIGH"], "B": ["HIGH"] * 4}.items()
            for t, lv in zip([0, 10, 20, 30], levels)]
    out = add_labels(pd.DataFrame(rows), [10, 20])
    a = out[out.junction_id == "A"].set_index("time_s")
    assert a.loc[0, "label_h10"] == "LOW" and a.loc[10, "label_h10"] == "HIGH"
    assert a.loc[0, "label_h20"] == "HIGH"
    assert pd.isna(a.loc[30, "label_h10"]) and pd.isna(a.loc[20, "label_h20"])
    assert (out[out.junction_id == "B"]["label_h10"].dropna() == "HIGH").all()


def test_evaluate_predictions_on_a_known_case():
    true = ["LOW", "LOW", "MEDIUM", "HIGH", "HIGH", "LOW"]
    pred = ["LOW", "MEDIUM", "MEDIUM", "HIGH", "MEDIUM", "LOW"]
    now = ["LOW", "LOW", "LOW", "MEDIUM", "MEDIUM", "LOW"]
    m = evaluate_predictions(true, pred, now)
    assert m["accuracy"] == pytest.approx(4 / 6, abs=1e-3)
    assert m["recall"] == {"LOW": pytest.approx(2 / 3, abs=1e-3), "MEDIUM": 1.0, "HIGH": 0.5}
    assert m["confusion_matrix"]["rows_true_cols_predicted"] == [[2, 1, 0], [0, 1, 0], [0, 1, 1]]
    # worsening rows: idx 2 (LOW->MEDIUM), 3 and 4 (MEDIUM->HIGH); predicted worse at 2 and 3
    assert m["worsening_events"] == 3 and m["onset_recall"] == pytest.approx(2 / 3, abs=1e-3)
    # not worsening: idx 0, 1, 5; predicted worse only at 1
    assert m["false_alarm_rate"] == pytest.approx(1 / 3, abs=1e-3)


def test_persistence_never_catches_an_onset():
    true, now = ["MEDIUM", "HIGH", "LOW"], ["LOW", "MEDIUM", "LOW"]
    assert evaluate_predictions(true, now, now)["onset_recall"] == 0.0


def test_split_runs_is_by_run_deterministic_and_leaves_named_runs_out():
    frame = pd.DataFrame({"run_id": [f"r{i}" for i in range(12)] + ["n1"],
                          "kind": ["random"] * 12 + ["named"]})
    train, test = split_runs(frame, 0.25, 1)
    assert set(train).isdisjoint(test) and len(train) + len(test) == 12 and len(test) == 3
    assert "n1" not in train + test
    assert split_runs(frame, 0.25, 1) == (train, test)


def test_split_runs_needs_enough_runs():
    with pytest.raises(PredictionError, match="at least 4"):
        split_runs(pd.DataFrame({"run_id": ["a", "b"], "kind": ["random", "random"]}))


def _synthetic_dataset(runs=8, rows=120, seed=0):
    """Rows whose future level follows the current score plus its trend (so it is learnable)."""
    rng = np.random.RandomState(seed)
    cols = feature_columns(CFG)
    parts = []
    for r in range(runs):
        x = pd.DataFrame(rng.rand(rows, len(cols)), columns=cols)
        later = (x["score"] + 0.5 * (x["slope60_score"] - 0.5)).to_numpy()
        x["current_level"] = classify_scores(x["score"].to_numpy())
        x[label_column(60)] = classify_scores(later)
        x["junction_id"], x["time_s"], x["history_ok"] = "J1", np.arange(rows) * 15, True
        x["run_id"], x["scenario"] = f"run{r}", f"run{r}"
        x["kind"] = "random"
        parts.append(x)
    named = parts[0].copy()
    named["run_id"], named["kind"] = "named_x", "named"
    return pd.concat(parts + [named], ignore_index=True)


def test_training_runs_end_to_end_and_beats_persistence_on_learnable_data():
    result = train_for_horizon(_synthetic_dataset(), 60, models=["logistic_regression", "random_forest"],
                               n_splits=3)
    m = result.metrics
    assert m["chosen_model"] in ("logistic_regression", "random_forest")
    assert set(m["train_runs"]).isdisjoint(m["test_runs"])
    assert m["test"]["model"]["macro_f1"] > m["test"]["persistence"]["macro_f1"]
    assert m["test"]["model"]["onset_recall"] > 0.0
    assert m["named_scenarios"] and "named_x" in m["named_scenarios_by_run"]
    assert result.bundle.feature_columns == feature_columns(CFG)
    assert result.bundle.info.test_macro_f1 == m["test"]["model"]["macro_f1"]
    assert "persistence" in format_comparison(m)
    json.dumps(m)                                            # metrics are JSON-serialisable


def test_training_rejects_unknown_model_and_missing_horizon():
    data = _synthetic_dataset()
    with pytest.raises(PredictionError, match="Unknown model"):
        train_for_horizon(data, 60, models=["magic"])
    with pytest.raises(PredictionError, match="no labels"):
        train_for_horizon(data, 120)


def test_dataset_columns_include_labels_for_every_horizon():
    cols = dataset_columns(CFG)
    assert all(label_column(h) in cols for h in CFG.horizons_s)
    assert cols[:3] == ["run_id", "scenario", "kind"]