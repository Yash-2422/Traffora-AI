"""Training data and model training for traffic prediction.

Two jobs:

1. **Dataset** - run SUMO scenarios, and for every junction and sample time record the
   features (``FeatureBuilder``) plus the level the detector reports ``h`` seconds later
   (``label_h<h>`` columns). ``simulate_run`` does one run.
2. **Training** - pick a model by cross-validation over *runs*, evaluate it on runs it
   never saw, and compare it with the rule-based baselines. ``train_for_horizon`` does
   this and returns a ``ModelBundle`` plus a metrics dictionary.

Rows from one run are strongly correlated (a queue lasts minutes), so data is always
split by run, never by row: a random row split would put near-identical rows in both the
training and test sets and make every model look better than it is.

Needs scikit-learn and pandas; ``simulate_run`` also needs SUMO.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .config import CongestionConfig, PredictionConfig, SimulationConfig
from .congestion_detector import CongestionDetector
from .exceptions import PredictionError
from .simulation_interface import SimulationInterface
from .traffic_monitor import TrafficMonitor
from .traffic_predictor import (
    ID_COLUMNS,
    LEVELS,
    FeatureBuilder,
    ModelBundle,
    feature_columns,
    make_model_info,
    persistence_levels,
    trend_levels,
)

logger = logging.getLogger(__name__)

RUN_COLUMNS: Tuple[str, ...] = ("run_id", "scenario", "kind")   # kind: "random" | "named"
_RANK = {level: i for i, level in enumerate(LEVELS)}


def label_column(horizon_s: int) -> str:
    return f"label_h{horizon_s}"


def dataset_columns(config: Optional[PredictionConfig] = None) -> List[str]:
    cfg = config or PredictionConfig()
    return (list(RUN_COLUMNS) + list(ID_COLUMNS) + feature_columns(cfg)
            + [label_column(h) for h in cfg.horizons_s])


# --------------------------------------------------------------------------- #
# Dataset generation
# --------------------------------------------------------------------------- #
def simulate_run(
    sumocfg: Path,
    seed: int,
    run_id: str,
    scenario: str,
    kind: str = "random",
    config: Optional[PredictionConfig] = None,
    congestion_config: Optional[CongestionConfig] = None,
    stride_s: int = 15,
    until_s: Optional[int] = None,
) -> pd.DataFrame:
    """Run one scenario and return its rows (one per junction every ``stride_s`` seconds).

    Rows without enough history are dropped. A label is empty (NaN) when the run ends
    before ``time + horizon``.

    Raises:
        PredictionError: ``stride_s`` is not a multiple of the sample interval.
        TrafficMindError: SUMO could not be started (see the error's ``code``).
    """
    cfg = config or PredictionConfig()
    cong = congestion_config or CongestionConfig()
    if stride_s <= 0 or stride_s % cfg.sample_interval_s:
        raise PredictionError(
            f"stride_s ({stride_s}) must be a multiple of the sample interval ({cfg.sample_interval_s} s).")

    sim_config = SimulationConfig(sumocfg=Path(sumocfg), seed=seed, label=f"gen_{run_id}")
    builder = FeatureBuilder(cfg, cong)
    rows: List[Dict[str, Any]] = []
    with SimulationInterface(sim_config) as sim:
        monitor = TrafficMonitor.from_source(sim)
        detector = CongestionDetector(cong)
        while not sim.has_ended and (until_s is None or sim.current_time < until_s):
            sim.step(cfg.sample_interval_s)
            snapshot = monitor.sample(sim)
            report = detector.update(snapshot)
            builder.update(snapshot, report)
            rows.extend(builder.feature_rows())

    frame = pd.DataFrame(rows, columns=list(ID_COLUMNS) + builder.columns)
    if frame.empty:
        return pd.DataFrame(columns=dataset_columns(cfg))
    frame["time_s"] = frame["time_s"].round().astype(int)
    frame = add_labels(frame, cfg.horizons_s)
    frame = frame[frame["history_ok"] & (frame["time_s"] % stride_s == 0)].copy()
    frame.insert(0, "kind", kind)
    frame.insert(0, "scenario", scenario)
    frame.insert(0, "run_id", run_id)
    return frame[dataset_columns(cfg)].reset_index(drop=True)


def add_labels(frame: pd.DataFrame, horizons_s: Sequence[int]) -> pd.DataFrame:
    """Add ``label_h<h>``: the junction's level ``h`` seconds after each row (NaN if unknown).

    ``frame`` must hold every sample time of the run (not a subsample), with integer
    ``time_s`` and the level in ``current_level``.
    """
    out = frame.copy()
    levels = out.set_index(["junction_id", "time_s"])["current_level"]
    for horizon in horizons_s:
        keys = pd.MultiIndex.from_arrays([out["junction_id"], out["time_s"] + int(horizon)])
        out[label_column(horizon)] = levels.reindex(keys).to_numpy()
    return out


# --------------------------------------------------------------------------- #
# Loading and splitting
# --------------------------------------------------------------------------- #
def load_dataset(path: Path) -> pd.DataFrame:
    """Read a dataset CSV written by ``scripts/generate_dataset.py``."""
    source = Path(path)
    if not source.is_file():
        raise PredictionError(f"Dataset not found: {source}. Create it with scripts/generate_dataset.py.")
    frame = pd.read_csv(source)
    missing = [c for c in dataset_columns() if c not in frame.columns]
    if missing:
        raise PredictionError(
            f"Dataset {source} lacks columns {missing[:5]}; re-generate it with this version of the code.")
    return frame


def split_runs(
    frame: pd.DataFrame, test_fraction: float = 0.25, random_state: int = 42
) -> Tuple[List[str], List[str]]:
    """Split the *random* runs into train / test run ids (the "named" runs are kept apart)."""
    run_ids = sorted(frame.loc[frame["kind"] == "random", "run_id"].unique())
    if len(run_ids) < 4:
        raise PredictionError(f"Need at least 4 random runs to train and test; found {len(run_ids)}.")
    rng = np.random.RandomState(random_state)
    shuffled = list(rng.permutation(run_ids))
    n_test = min(len(run_ids) - 2, max(1, round(len(run_ids) * test_fraction)))
    return sorted(shuffled[n_test:]), sorted(shuffled[:n_test])


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def evaluate_predictions(
    y_true: Sequence[str], y_pred: Sequence[str], current: Sequence[str]
) -> Dict[str, Any]:
    """Accuracy, macro-F1, per-class recall, confusion matrix and early-warning scores.

    *Onset recall*: of the rows where congestion was about to get worse than the current
    level, the share predicted to get worse. *False-alarm rate*: of the rows where it did
    not get worse, the share predicted to get worse. Persistence has onset recall 0.
    """
    from sklearn.metrics import confusion_matrix, f1_score, recall_score

    true, pred, now = (np.asarray(a) for a in (y_true, y_pred, current))
    labels = list(LEVELS)
    matrix = confusion_matrix(true, pred, labels=labels)
    recalls = recall_score(true, pred, labels=labels, average=None, zero_division=0)
    t_rank = np.array([_RANK[v] for v in true])
    p_rank = np.array([_RANK[v] for v in pred])
    c_rank = np.array([_RANK[v] for v in now])
    worsening = t_rank > c_rank
    predicted_worse = p_rank > c_rank
    return {
        "rows": int(len(true)),
        "accuracy": round(float((true == pred).mean()), 4),
        "macro_f1": round(float(f1_score(true, pred, labels=labels, average="macro", zero_division=0)), 4),
        "recall": {lvl: round(float(r), 4) for lvl, r in zip(labels, recalls)},
        "confusion_matrix": {"labels": labels, "rows_true_cols_predicted": matrix.tolist()},
        "worsening_events": int(worsening.sum()),
        "onset_recall": round(float(predicted_worse[worsening].mean()), 4) if worsening.any() else None,
        "false_alarm_rate": round(float(predicted_worse[~worsening].mean()), 4) if (~worsening).any() else None,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
def candidate_models(random_state: int = 42) -> Dict[str, Callable[[], Any]]:
    """Factories for the models that are compared (each returns an unfitted pipeline)."""
    from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    return {
        "logistic_regression": lambda: make_pipeline(
            SimpleImputer(strategy="median"), StandardScaler(),
            LogisticRegression(max_iter=2000, class_weight="balanced", C=1.0)),
        "random_forest": lambda: make_pipeline(
            SimpleImputer(strategy="median"),
            RandomForestClassifier(n_estimators=100, min_samples_leaf=30, class_weight="balanced_subsample",
                                   n_jobs=2, random_state=random_state)),
        "gradient_boosting": lambda: make_pipeline(
            SimpleImputer(strategy="median"),
            HistGradientBoostingClassifier(max_iter=150, learning_rate=0.08, max_depth=4,
                                           class_weight="balanced", random_state=random_state)),
    }


def _cross_validate(factory: Callable[[], Any], x: pd.DataFrame, y: np.ndarray,
                    groups: np.ndarray, n_splits: int) -> Dict[str, float]:
    from sklearn.metrics import f1_score
    from sklearn.model_selection import GroupKFold

    scores: List[float] = []
    for train_idx, valid_idx in GroupKFold(n_splits=n_splits).split(x, y, groups):
        model = factory()
        model.fit(x.iloc[train_idx], y[train_idx])
        scores.append(f1_score(y[valid_idx], model.predict(x.iloc[valid_idx]),
                               labels=list(LEVELS), average="macro", zero_division=0))
    return {"mean_macro_f1": round(float(np.mean(scores)), 4), "std": round(float(np.std(scores)), 4)}


def tune_trend_gain(train: pd.DataFrame, horizon_s: int, config: PredictionConfig,
                    congestion_config: CongestionConfig, target: str) -> float:
    """Pick the trend baseline's gain on the training rows (so the baseline is fair, not a strawman)."""
    from sklearn.metrics import f1_score

    best_gain, best_f1 = 1.0, -1.0
    for gain in (0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0):
        pred = trend_levels(train, horizon_s, gain, config, congestion_config)
        score = f1_score(train[target], pred, labels=list(LEVELS), average="macro", zero_division=0)
        if score > best_f1:
            best_gain, best_f1 = gain, score
    return best_gain


@dataclass
class TrainingResult:
    bundle: ModelBundle
    metrics: Dict[str, Any]


def train_for_horizon(
    frame: pd.DataFrame,
    horizon_s: int,
    config: Optional[PredictionConfig] = None,
    congestion_config: Optional[CongestionConfig] = None,
    models: Optional[Sequence[str]] = None,
    test_fraction: float = 0.25,
    n_splits: int = 4,
    progress: Callable[[str], None] = lambda message: None,
) -> TrainingResult:
    """Select, train and evaluate a model for one horizon.

    Steps: split runs into train / test; choose the best candidate by cross-validated
    macro-F1 on the training runs; refit it on all training runs; score it and the
    baselines on the held-out test runs and on the named scenarios (``kind == "named"``).
    """
    import sklearn

    cfg = config or PredictionConfig()
    cong = congestion_config or CongestionConfig()
    target = label_column(horizon_s)
    if target not in frame.columns:
        raise PredictionError(f"The dataset has no labels for a {horizon_s} s horizon.")
    columns = feature_columns(cfg)

    data = frame.dropna(subset=[target]).copy()
    train_ids, test_ids = split_runs(data, test_fraction, cfg.random_state)
    train = data[data["run_id"].isin(train_ids)]
    test = data[data["run_id"].isin(test_ids)]
    named = data[data["kind"] == "named"]
    progress(f"{horizon_s} s horizon: {len(train_ids)} training runs ({len(train)} rows), "
             f"{len(test_ids)} test runs ({len(test)} rows), {named['run_id'].nunique()} named scenarios")
    if train[target].nunique() < 2:
        raise PredictionError("The training runs contain a single congestion level; cannot train.")

    factories = candidate_models(cfg.random_state)
    names = list(models) if models else list(factories)
    unknown = [n for n in names if n not in factories]
    if unknown:
        raise PredictionError(f"Unknown model(s) {unknown}; choose from {list(factories)}.")

    x_train, y_train = train[columns], train[target].to_numpy()
    groups = train["run_id"].to_numpy()
    folds = max(2, min(n_splits, len(train_ids)))
    cv: Dict[str, Dict[str, float]] = {}
    for name in names:
        cv[name] = _cross_validate(factories[name], x_train, y_train, groups, folds)
        progress(f"  cross-validation {name}: macro-F1 {cv[name]['mean_macro_f1']:.3f} (+/- {cv[name]['std']:.3f})")
    best = max(names, key=lambda n: cv[n]["mean_macro_f1"])
    progress(f"  chosen: {best}")

    estimator = factories[best]()
    estimator.fit(x_train, y_train)

    gain = tune_trend_gain(train, horizon_s, cfg, cong, target)

    def score_sets(subset: pd.DataFrame) -> Dict[str, Any]:
        if subset.empty:
            return {}
        truth, now = subset[target].to_numpy(), subset["current_level"].to_numpy()
        predictions = {
            "model": estimator.predict(subset[columns]),
            "persistence": persistence_levels(subset),
            "trend": trend_levels(subset, horizon_s, gain, cfg, cong),
            "always_low": np.full(len(subset), "LOW"),
        }
        return {name: evaluate_predictions(truth, pred, now) for name, pred in predictions.items()}

    metrics = {
        "horizon_s": horizon_s,
        "chosen_model": best,
        "cross_validation": cv,
        "trend_gain": gain,
        "train_runs": train_ids,
        "test_runs": test_ids,
        "class_balance_test": test[target].value_counts().to_dict(),
        "test": score_sets(test),
        "named_scenarios": score_sets(named),
        "named_scenarios_by_run": {
            run: score_sets(named[named["run_id"] == run]) for run in sorted(named["run_id"].unique())
        },
    }
    test_f1 = metrics["test"].get("model", {}).get("macro_f1")
    bundle = ModelBundle(
        estimator=estimator,
        classes=[str(c) for c in estimator.classes_],
        feature_columns=list(columns),
        horizon_s=horizon_s,
        info=make_model_info(best, horizon_s, len(columns), training_rows=int(len(train)),
                             training_runs=len(train_ids), test_macro_f1=test_f1),
        congestion_config=asdict(cong),
        sklearn_version=sklearn.__version__,
    )
    return TrainingResult(bundle=bundle, metrics=metrics)


def format_comparison(metrics: Dict[str, Any], section: str = "test") -> str:
    """A small text table comparing the model with the baselines."""
    rows = metrics.get(section) or {}
    lines = [f"{'method':<13}{'macro-F1':>9}{'accuracy':>9}{'recall L/M/H':>20}{'onset recall':>14}{'false alarms':>14}"]
    for name in ("model", "trend", "persistence", "always_low"):
        if name not in rows:
            continue
        r = rows[name]
        rec = "/".join(f"{r['recall'][lvl]:.2f}" for lvl in LEVELS)
        onset = "-" if r["onset_recall"] is None else f"{r['onset_recall']:.2f}"
        alarm = "-" if r["false_alarm_rate"] is None else f"{r['false_alarm_rate']:.2f}"
        lines.append(f"{name:<13}{r['macro_f1']:>9.3f}{r['accuracy']:>9.3f}{rec:>20}{onset:>14}{alarm:>14}")
    return "\n".join(lines)