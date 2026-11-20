"""Train and evaluate the congestion prediction model(s).

For each horizon: compares logistic regression, random forest and gradient boosting by
cross-validation over runs, trains the best one, and scores it against the rule-based
baselines on runs it has never seen. Saves the model and a metrics file in
``backend/agents/traffic_mind/models``.

From the repository root::

    python -m backend.agents.traffic_mind.scripts.train_model
    python -m backend.agents.traffic_mind.scripts.train_model --horizons 60 --models random_forest
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from ..config import DATASET_DIR, MODEL_DIR, PredictionConfig, configure_logging
from ..exceptions import TrafficMindError
from ..prediction_training import format_comparison, load_dataset, train_for_horizon


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Train the TrafficMind prediction model.")
    parser.add_argument("--dataset", type=Path, default=DATASET_DIR / "traffic_dataset.csv")
    parser.add_argument("--horizons", type=int, nargs="+", default=[PredictionConfig().horizon_s],
                        help="Prediction horizons in seconds (dataset has 30, 60, 120).")
    parser.add_argument("--models", nargs="+", default=None,
                        help="Candidates: logistic_regression random_forest gradient_boosting (default: all).")
    parser.add_argument("--out-dir", type=Path, default=MODEL_DIR)
    args = parser.parse_args(argv)

    configure_logging(logging.WARNING)
    try:
        data = load_dataset(args.dataset)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for horizon in args.horizons:
            result = train_for_horizon(data, horizon, models=args.models, progress=print)
            print("\nHeld-out random runs:")
            print(format_comparison(result.metrics, "test"))
            print("\nThe four named scenarios (never used for training):")
            print(format_comparison(result.metrics, "named_scenarios"))
            model_path = result.bundle.save(args.out_dir / f"traffic_model_h{horizon}.joblib")
            metrics_path = args.out_dir / f"metrics_h{horizon}.json"
            metrics_path.write_text(json.dumps(result.metrics, indent=2), encoding="utf-8")
            print(f"\nSaved {model_path}\nSaved {metrics_path}\n")
    except (TrafficMindError, ValueError) as exc:
        logging.getLogger("traffic_mind.train_model").error("%s: %s", getattr(exc, "code", "ERROR"), exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())