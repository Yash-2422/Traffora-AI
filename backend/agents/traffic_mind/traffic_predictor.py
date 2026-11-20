"""Traffic prediction: forecasts each junction's congestion level a short time ahead.

What is predicted
-----------------
For every signalised junction, the congestion level (LOW / MEDIUM / HIGH) that the
``CongestionDetector`` will report ``horizon_s`` seconds from now (default 60 s).
Predicting the junction rather than every road keeps the output to what the signal
logic and the attention list work with; the border roads are almost never congested.

Parts
-----
* ``FeatureBuilder``     - turns the recent snapshot / congestion-report history of each
  junction into a row of numeric features. The same code builds the training data and
  the live inputs, so the model sees identical features in both places.
* ``persistence_levels`` / ``trend_scores`` - the two rule-based baselines the model has
  to beat ("nothing changes" and "the score keeps moving the way it has been").
* ``ModelBundle``        - a trained scikit-learn classifier plus what is needed to use it.
* ``TrafficPredictor``   - the runtime: feed it snapshots, ask for ``predict()``. If no model
  is available, or there is too little history, it says so and assumes nothing changes.

Pure Python + pandas; no SUMO import. Loading a model needs ``joblib`` (installed with
scikit-learn). ``joblib`` files are pickles: only load model files you trust.

Typical use::

    predictor = TrafficPredictor()
    predictor.load_models()                      # models/traffic_model_h60.joblib
    report = predictor.predict(snapshot)         # call once per snapshot, in time order
    print(report.to_dict())
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
from pydantic import ValidationError

from .config import MODEL_DIR, CongestionConfig, PredictionConfig
from .congestion_detector import CongestionDetector
from .exceptions import InvalidTrafficDataError, ModelLoadError, PredictionError
from .schemas import (
    CongestionLevel,
    CongestionReport,
    EarlyWarning,
    JunctionPrediction,
    ModelInfo,
    PredictionReport,
    TrafficSnapshot,
)

logger = logging.getLogger(__name__)

LEVELS: Tuple[str, ...] = ("LOW", "MEDIUM", "HIGH")
_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

# --------------------------------------------------------------------------- #
# Feature definition
# --------------------------------------------------------------------------- #
# per-junction values read from the snapshot / congestion report at each sample
BASE_FEATURES: Tuple[str, ...] = (
    "vehicles",          # vehicles on the incoming roads
    "queue_veh",         # stopped vehicles
    "max_queue_m",       # longest queue (m)
    "wait_s",            # mean waiting time (s)
    "max_occ_pct",       # highest road occupancy (%)
    "flow_vph",          # vehicles entering per hour
    "score",             # congestion score (smoothed, worst approach)
    "f_occupancy",       # the four congestion signals of the worst approach
    "f_queue",
    "f_waiting",
    "f_speed_drop",
    "inc_speed_ratio",   # mean speed / limit over the incoming roads
    "queue_fill",        # longest queue as a share of the road length
    "phase",             # current signal phase index
    "phase_remaining_s",
)
# whole-network context
NETWORK_FEATURES: Tuple[str, ...] = (
    "net_vehicles",
    "net_stopped_frac",
    "net_speed_ratio",
    "net_mean_score",
    "net_junctions_medium_plus",
)
# values whose change over the lags is a feature
LAGGED_VARIABLES: Tuple[str, ...] = (
    "score", "vehicles", "queue_veh", "max_occ_pct", "flow_vph", "wait_s",
)
ID_COLUMNS: Tuple[str, ...] = ("junction_id", "time_s", "history_ok", "current_level")


def feature_columns(config: Optional[PredictionConfig] = None) -> List[str]:
    """Names of the model input columns, in order."""
    cfg = config or PredictionConfig()
    columns = list(BASE_FEATURES) + list(NETWORK_FEATURES)
    for lag in cfg.lags_s:
        columns += [f"d{lag}_{name}" for name in LAGGED_VARIABLES]
    window = cfg.trend_window_s
    columns += [f"mean{window}_score", f"max{window}_score", f"slope{window}_score"]
    return columns


# --------------------------------------------------------------------------- #
# Feature builder
# --------------------------------------------------------------------------- #
class FeatureBuilder:
    """Builds model features from the history of snapshots and congestion reports.

    Call ``update(snapshot, report)`` once per sample, in time order; then
    ``feature_rows()`` / ``features()`` give one row per junction for the latest time.
    A row's ``history_ok`` is False until enough history exists for every lag and the
    trend window; its lag / trend features are NaN until then.
    """

    def __init__(
        self,
        config: Optional[PredictionConfig] = None,
        congestion_config: Optional[CongestionConfig] = None,
    ) -> None:
        self.config = config or PredictionConfig()
        self.congestion_config = congestion_config or CongestionConfig()
        self.columns = feature_columns(self.config)
        self._history: Dict[str, Deque[Tuple[float, Dict[str, float]]]] = {}
        self._network: Deque[Tuple[float, Dict[str, float]]] = deque()
        self._first_time: Optional[float] = None
        self._last_time: Optional[float] = None

    # ------------------------------------------------------------------ #
    @property
    def latest_time(self) -> Optional[float]:
        return self._last_time

    @property
    def history_available_s(self) -> float:
        if self._first_time is None or self._last_time is None:
            return 0.0
        return self._last_time - self._first_time

    def reset(self) -> None:
        self._history.clear()
        self._network.clear()
        self._first_time = None
        self._last_time = None

    # ------------------------------------------------------------------ #
    def update(
        self,
        snapshot: Union[TrafficSnapshot, Dict[str, Any]],
        report: Union[CongestionReport, Dict[str, Any]],
    ) -> None:
        """Add one sample.

        Raises:
            InvalidTrafficDataError: malformed input, or the snapshot and the
                congestion report are not from the same time / junctions.
        """
        snap = self._validate(snapshot, TrafficSnapshot, "snapshot")
        rep = self._validate(report, CongestionReport, "congestion report")
        if abs(snap.timestamp_s - rep.timestamp_s) > 1e-6:
            raise InvalidTrafficDataError(
                f"Snapshot (t={snap.timestamp_s}) and congestion report (t={rep.timestamp_s}) "
                "are from different times."
            )
        now = snap.timestamp_s
        if self._last_time is not None and now < self._last_time:
            logger.warning("Sample time went backwards (%.1f -> %.1f); resetting features.",
                           self._last_time, now)
            self.reset()

        edges = {e.edge_id: e for e in snap.edges}
        congestion = {j.junction_id: j for j in rep.junctions}
        missing = [j.junction_id for j in snap.junctions if j.junction_id not in congestion]
        if missing:
            raise InvalidTrafficDataError(
                f"Congestion report has no entry for junctions {missing[:5]}.")

        records = {jm.junction_id: self._junction_record(jm, congestion[jm.junction_id], edges)
                   for jm in snap.junctions}
        network = {
            "net_vehicles": float(snap.network.total_vehicles),
            "net_stopped_frac": snap.network.halting_vehicles / max(1, snap.network.total_vehicles),
            "net_speed_ratio": snap.network.mean_speed_ratio,
            "net_mean_score": (sum(r["score"] for r in records.values()) / len(records)) if records else 0.0,
            "net_junctions_medium_plus": float(sum(1 for r in records.values() if r["level_rank"] >= 1)),
        }

        # nothing is stored before every junction has been processed (all-or-nothing)
        for junction_id, record in records.items():
            self._history.setdefault(junction_id, deque()).append((now, record))
        self._network.append((now, network))
        if self._first_time is None:
            self._first_time = now
        self._last_time = now
        self._prune(now)

    @staticmethod
    def _validate(value: Any, model: Any, what: str) -> Any:
        try:
            return value if isinstance(value, model) else model.model_validate(value)
        except ValidationError as exc:
            raise InvalidTrafficDataError(f"Invalid {what}: {exc}") from exc

    @staticmethod
    def _junction_record(jm: Any, cj: Any, edges: Dict[str, Any]) -> Dict[str, float]:
        incoming = [edges[e] for e in jm.incoming_edges if e in edges]
        vehicles = sum(e.vehicle_count for e in incoming)
        speed_ratio = (sum(e.speed_ratio * e.vehicle_count for e in incoming) / vehicles
                       if vehicles else 1.0)
        queue_fill = max((e.queue_length_m / e.length_m for e in incoming), default=0.0)
        return {
            "vehicles": float(jm.vehicle_count),
            "queue_veh": float(jm.queue_length_veh),
            "max_queue_m": jm.max_queue_length_m,
            "wait_s": jm.mean_waiting_time_s,
            "max_occ_pct": jm.max_occupancy_pct,
            "flow_vph": jm.flow_veh_per_hour,
            "score": cj.score,
            "f_occupancy": cj.factors.occupancy,
            "f_queue": cj.factors.queue,
            "f_waiting": cj.factors.waiting,
            "f_speed_drop": cj.factors.speed_drop,
            "inc_speed_ratio": speed_ratio,
            "queue_fill": queue_fill,
            "phase": float(jm.current_phase if jm.current_phase is not None else -1),
            "phase_remaining_s": jm.phase_remaining_s or 0.0,
            "level_rank": float(cj.level.rank),
        }

    def _prune(self, now: float) -> None:
        horizon = now - self.config.history_s
        for history in self._history.values():
            while len(history) > 1 and history[1][0] <= horizon:
                history.popleft()
        while len(self._network) > 1 and self._network[1][0] <= horizon:
            self._network.popleft()

    # ------------------------------------------------------------------ #
    def feature_rows(self) -> List[Dict[str, Any]]:
        """One dict per junction for the latest sample (ids + features)."""
        if self._last_time is None:
            return []
        now = self._last_time
        tol = self.config.sample_interval_s / 2.0   # a lag point must be within half an interval
        window = self.config.trend_window_s
        net_now = self._network[-1][1]
        rows: List[Dict[str, Any]] = []

        for junction_id in sorted(self._history):
            history = self._history[junction_id]
            if history[-1][0] != now:
                continue  # junction not in the latest sample
            current = history[-1][1]
            row: Dict[str, Any] = {
                "junction_id": junction_id,
                "time_s": now,
                "current_level": LEVELS[int(current["level_rank"])],
            }
            features: Dict[str, float] = {name: current[name] for name in BASE_FEATURES}
            features.update(net_now)

            history_ok = True
            for lag in self.config.lags_s:
                past = _record_near(history, now - lag, tol)
                if past is None:
                    history_ok = False
                for name in LAGGED_VARIABLES:
                    features[f"d{lag}_{name}"] = (current[name] - past[name]) if past else math.nan

            recent = [(t, rec["score"]) for t, rec in history if t >= now - window]
            covered = history[0][0] <= now - window + tol and len(recent) >= 3
            if covered:
                scores = [s for _, s in recent]
                features[f"mean{window}_score"] = sum(scores) / len(scores)
                features[f"max{window}_score"] = max(scores)
                features[f"slope{window}_score"] = _slope_per_minute(recent)
            else:
                history_ok = False
                features[f"mean{window}_score"] = math.nan
                features[f"max{window}_score"] = math.nan
                features[f"slope{window}_score"] = math.nan

            row["history_ok"] = history_ok
            row.update({name: features[name] for name in self.columns})
            rows.append(row)
        return rows

    def features(self) -> pd.DataFrame:
        """``feature_rows()`` as a DataFrame (columns: ids first, then the features)."""
        return pd.DataFrame(self.feature_rows(), columns=list(ID_COLUMNS) + self.columns)


def _record_near(
    history: Deque[Tuple[float, Dict[str, float]]], target: float, tol: float
) -> Optional[Dict[str, float]]:
    """The record closest in time to ``target`` (within ``tol`` seconds), else None."""
    best, best_gap = None, None
    for t, record in reversed(history):
        gap = abs(t - target)
        if best_gap is None or gap < best_gap:
            best, best_gap = record, gap
        if t < target - tol:
            break
    return best if best_gap is not None and best_gap <= tol else None


def _slope_per_minute(points: List[Tuple[float, float]]) -> float:
    """Least-squares slope of value against time, in value per minute."""
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    denominator = sum((t - mean_t) ** 2 for t, _ in points)
    if denominator == 0:
        return 0.0
    return 60.0 * sum((t - mean_t) * (v - mean_v) for t, v in points) / denominator


# --------------------------------------------------------------------------- #
# Rule-based baselines
# --------------------------------------------------------------------------- #
def classify_scores(scores: Any, config: Optional[CongestionConfig] = None) -> np.ndarray:
    """Vectorised score -> level ("LOW" / "MEDIUM" / "HIGH") with the detector's thresholds."""
    cfg = config or CongestionConfig()
    arr = np.asarray(scores, dtype=float)
    return np.select([arr >= cfg.high_threshold, arr >= cfg.medium_threshold],
                     ["HIGH", "MEDIUM"], default="LOW")


def persistence_levels(features: pd.DataFrame) -> np.ndarray:
    """Baseline 1: the level stays what it is now."""
    return features["current_level"].to_numpy()


def trend_scores(
    features: pd.DataFrame, horizon_s: int, gain: float = 1.0,
    config: Optional[PredictionConfig] = None,
) -> np.ndarray:
    """Baseline 2: the score keeps moving the way it has over the trend window.

    ``score_later = score + gain * slope_per_minute * horizon_s / 60``, kept within 0..1.
    ``gain`` 0 is the same as persistence.
    """
    window = (config or PredictionConfig()).trend_window_s
    slope = features[f"slope{window}_score"].fillna(0.0).to_numpy()
    return np.clip(features["score"].to_numpy() + gain * slope * horizon_s / 60.0, 0.0, 1.0)


def trend_levels(
    features: pd.DataFrame, horizon_s: int, gain: float = 1.0,
    config: Optional[PredictionConfig] = None,
    congestion_config: Optional[CongestionConfig] = None,
) -> np.ndarray:
    """Baseline 2 as levels."""
    return classify_scores(trend_scores(features, horizon_s, gain, config), congestion_config)


# --------------------------------------------------------------------------- #
# Trained model
# --------------------------------------------------------------------------- #
@dataclass
class ModelBundle:
    """A trained classifier and everything needed to use it."""

    estimator: Any                       # scikit-learn classifier with predict_proba
    classes: List[str]                   # class labels in the estimator's column order
    feature_columns: List[str]
    horizon_s: int
    info: ModelInfo
    congestion_config: Dict[str, Any]    # thresholds the labels were made with
    sklearn_version: str = ""

    def save(self, path: Union[str, Path]) -> Path:
        """Write the bundle to ``path`` (a joblib file)."""
        import joblib

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "estimator": self.estimator, "classes": list(self.classes),
            "feature_columns": list(self.feature_columns), "horizon_s": self.horizon_s,
            "info": self.info.to_dict(), "congestion_config": self.congestion_config,
            "sklearn_version": self.sklearn_version,
        }
        joblib.dump(payload, target, compress=3)
        return target

    @classmethod
    def load(cls, path: Union[str, Path]) -> "ModelBundle":
        """Read a bundle written by ``save``.

        Raises:
            ModelLoadError: the file is missing or is not a valid model bundle.
        """
        source = Path(path)
        if not source.is_file():
            raise ModelLoadError(f"Model file not found: {source}")
        try:
            import joblib

            payload = joblib.load(source)
            return cls(
                estimator=payload["estimator"], classes=[str(c) for c in payload["classes"]],
                feature_columns=list(payload["feature_columns"]),
                horizon_s=int(payload["horizon_s"]), info=ModelInfo.model_validate(payload["info"]),
                congestion_config=dict(payload["congestion_config"]),
                sklearn_version=str(payload.get("sklearn_version", "")),
            )
        except ModelLoadError:
            raise
        except Exception as exc:  # noqa: BLE001 - any unpickling / format problem is a load error
            raise ModelLoadError(f"Could not read model file {source}: {exc}") from exc


def make_model_info(
    name: str, horizon_s: int, feature_count: int, training_rows: Optional[int] = None,
    training_runs: Optional[int] = None, test_macro_f1: Optional[float] = None,
) -> ModelInfo:
    return ModelInfo(
        name=name, horizon_s=horizon_s, feature_count=feature_count,
        trained_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        training_rows=training_rows, training_runs=training_runs, test_macro_f1=test_macro_f1,
    )


def _sklearn_version() -> str:
    try:
        import sklearn

        return str(sklearn.__version__)
    except ImportError:
        return ""


# --------------------------------------------------------------------------- #
# Runtime predictor
# --------------------------------------------------------------------------- #
class TrafficPredictor:
    """Predicts each junction's congestion level ``horizon_s`` seconds ahead.

    Feed it one snapshot per sample (``update`` or ``predict(snapshot)``), in time order.
    If you already run a ``CongestionDetector`` pass its report too; otherwise the
    predictor runs its own.
    """

    def __init__(
        self,
        config: Optional[PredictionConfig] = None,
        congestion_config: Optional[CongestionConfig] = None,
        detector: Optional[CongestionDetector] = None,
    ) -> None:
        self.config = config or PredictionConfig()
        self.congestion_config = congestion_config or CongestionConfig()
        self._detector = detector or CongestionDetector(self.congestion_config)
        self._builder = FeatureBuilder(self.config, self.congestion_config)
        self._models: Dict[int, ModelBundle] = {}
        self._notes: List[str] = []

    # ------------------------------------------------------------------ #
    # Models
    # ------------------------------------------------------------------ #
    @property
    def horizons(self) -> List[int]:
        """Horizons (seconds) that have a trained model loaded."""
        return sorted(self._models)

    def add_model(self, bundle: ModelBundle) -> None:
        """Use a bundle for its horizon.

        Raises:
            ModelLoadError: the model was trained on different features than this code builds.
        """
        expected = self._builder.columns
        if list(bundle.feature_columns) != expected:
            raise ModelLoadError(
                "The model was trained with different features than this version builds "
                f"({len(bundle.feature_columns)} vs {len(expected)} columns). Re-train it."
            )
        trained_with = bundle.congestion_config or {}
        current = asdict(self.congestion_config)
        differing = sorted(k for k in ("medium_threshold", "high_threshold", "smoothing_window_s")
                           if k in trained_with and trained_with[k] != current.get(k))
        if differing:
            self._notes.append(
                f"The {bundle.horizon_s} s model was trained with different congestion settings "
                f"({', '.join(differing)}); predictions may be off. Re-train it."
            )
        installed = _sklearn_version()
        if bundle.sklearn_version and installed and bundle.sklearn_version.split(".")[:2] != installed.split(".")[:2]:
            self._notes.append(
                f"The {bundle.horizon_s} s model was trained with scikit-learn {bundle.sklearn_version} "
                f"but {installed} is installed; if predictions look wrong, re-train it "
                "(python -m backend.agents.traffic_mind.scripts.train_model).")
        self._models[bundle.horizon_s] = bundle

    def load_model(self, path: Union[str, Path]) -> int:
        """Load one model file; returns its horizon."""
        bundle = ModelBundle.load(path)
        self.add_model(bundle)
        return bundle.horizon_s

    def load_models(self, directory: Optional[Union[str, Path]] = None) -> List[int]:
        """Load every ``traffic_model_h*.joblib`` in ``directory`` (default: the models folder).

        A missing folder or no files is not an error (the predictor then falls back to
        persistence and says so); an unreadable or mismatching file raises ModelLoadError.
        Returns the horizons loaded.
        """
        folder = Path(directory) if directory is not None else MODEL_DIR
        loaded = []
        for path in sorted(folder.glob("traffic_model_h*.joblib")) if folder.is_dir() else []:
            loaded.append(self.load_model(path))
        if not loaded:
            logger.warning("No trained models found in %s", folder)
        return sorted(loaded)

    # ------------------------------------------------------------------ #
    # Data in
    # ------------------------------------------------------------------ #
    def update(
        self,
        snapshot: Union[TrafficSnapshot, Dict[str, Any]],
        report: Optional[Union[CongestionReport, Dict[str, Any]]] = None,
    ) -> None:
        """Add one sample (and its congestion report; computed if not given)."""
        snap = FeatureBuilder._validate(snapshot, TrafficSnapshot, "snapshot")
        if report is None:
            report = self._detector.update(snap)
        self._builder.update(snap, report)

    def reset(self) -> None:
        """Forget all history (e.g. after a simulation restart)."""
        self._builder.reset()
        self._detector.reset()

    # ------------------------------------------------------------------ #
    # Prediction
    # ------------------------------------------------------------------ #
    def predict(
        self,
        snapshot: Optional[Union[TrafficSnapshot, Dict[str, Any]]] = None,
        report: Optional[Union[CongestionReport, Dict[str, Any]]] = None,
        horizon_s: Optional[int] = None,
    ) -> PredictionReport:
        """Predict every junction's level ``horizon_s`` seconds ahead.

        Pass a ``snapshot`` to add it first (the usual call, once per sample).

        Raises:
            InvalidTrafficDataError: malformed snapshot.
            PredictionError: no data has been given yet, or ``horizon_s`` is invalid.
        """
        if snapshot is not None:
            self.update(snapshot, report)
        if self._builder.latest_time is None:
            raise PredictionError("No traffic data yet: pass a snapshot to predict() or update().")
        horizon = self.config.horizon_s if horizon_s is None else horizon_s
        if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
            raise PredictionError(f"horizon_s must be a positive whole number of seconds, got {horizon!r}")

        rows = self._builder.feature_rows()
        frame = pd.DataFrame(rows, columns=list(ID_COLUMNS) + self._builder.columns)
        bundle = self._models.get(horizon)
        history_ok = bool(len(frame)) and bool(frame["history_ok"].all())

        notes = list(self._notes)
        probabilities: Optional[np.ndarray] = None
        if bundle is None:
            notes.append(
                f"No trained model for a {horizon} s horizon: assuming every junction stays at "
                "its current level. Train one with scripts/train_model.py.")
        elif not history_ok:
            notes.append(
                f"Only {self._builder.history_available_s:.0f} s of history "
                f"({self.config.min_history_s} s needed): assuming every junction stays at its "
                "current level for now.")
        else:
            probabilities = self._predict_proba(bundle, frame)

        predictions = [
            self._junction_prediction(i, row, probabilities, bundle) for i, row in enumerate(rows)
        ]
        warnings = sorted(
            (self._warning(p, horizon) for p in predictions if p.expected_change == "worsening"),
            key=lambda w: (-_RANK[w.predicted_level.value], -w.confidence, w.junction_id),
        )
        return PredictionReport(
            timestamp_s=self._builder.latest_time,
            horizon_s=horizon,
            method="ml" if probabilities is not None else "persistence",
            model=bundle.info if probabilities is not None else None,
            history_available_s=self._builder.history_available_s,
            predictions=predictions,
            early_warnings=warnings,
            notes=notes,
        )

    @staticmethod
    def _predict_proba(bundle: ModelBundle, frame: pd.DataFrame) -> np.ndarray:
        """Class probabilities as an (n_junctions, 3) array in LOW / MEDIUM / HIGH order."""
        try:
            raw = np.asarray(bundle.estimator.predict_proba(frame[bundle.feature_columns]))
        except Exception as exc:  # noqa: BLE001
            raise PredictionError(f"The model could not make a prediction: {exc}") from exc
        out = np.zeros((len(frame), len(LEVELS)))
        for column, label in enumerate(bundle.classes):
            if label in _RANK:
                out[:, _RANK[label]] = raw[:, column]
        total = out.sum(axis=1, keepdims=True)
        if not np.all(total > 0):
            raise PredictionError("The model returned no probabilities for a junction.")
        return out / total

    @staticmethod
    def _junction_prediction(
        index: int, row: Dict[str, Any], probabilities: Optional[np.ndarray],
        bundle: Optional[ModelBundle],
    ) -> JunctionPrediction:
        current = row["current_level"]
        if probabilities is None:
            proba = [1.0 if level == current else 0.0 for level in LEVELS]
        else:
            proba = [round(float(p), 4) for p in probabilities[index]]
            proba[int(np.argmax(proba))] += round(1.0 - sum(proba), 4)   # keep the total at 1
        predicted = LEVELS[int(np.argmax(proba))]
        change = ("worsening" if _RANK[predicted] > _RANK[current]
                  else "improving" if _RANK[predicted] < _RANK[current] else "steady")
        return JunctionPrediction(
            junction_id=row["junction_id"],
            current_level=current,
            current_score=round(float(row["score"]), 4),
            predicted_level=predicted,
            probabilities=dict(zip(LEVELS, proba)),
            confidence=max(proba),
            expected_change=change,
            method="ml" if probabilities is not None else "persistence",
        )

    @staticmethod
    def _warning(prediction: JunctionPrediction, horizon_s: int) -> EarlyWarning:
        return EarlyWarning(
            junction_id=prediction.junction_id,
            current_level=prediction.current_level,
            predicted_level=prediction.predicted_level,
            confidence=prediction.confidence,
            message=(f"Junction {prediction.junction_id}: {prediction.current_level.value} now, "
                     f"{prediction.predicted_level.value} expected in {horizon_s} s "
                     f"({prediction.confidence:.0%} confidence)."),
        )