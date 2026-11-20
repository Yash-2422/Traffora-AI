"""Generate the prediction training dataset by running SUMO.

Runs a number of *random* demand scenarios (for training and testing) plus the four named
scenarios (offpeak, peak, mixed, surge; a separate check) and writes one CSV.

From the repository root::

    python -m backend.agents.traffic_mind.scripts.generate_dataset --random-runs 40 --workers 2
    python -m backend.agents.traffic_mind.scripts.generate_dataset --random-runs 2 --no-named --until 900 --out datasets/traffic_mind/quick.csv

Each run takes about 15-20 s, so 40 + 4 runs take roughly 10 minutes with 2 workers.
Runs are independent and deterministic: the same seed gives the same data.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import logging
import random
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

from ..config import DATASET_DIR, SCENARIO_DIR, PredictionConfig, configure_logging
from ..exceptions import SimulationConnectionError, TrafficMindError
from ..prediction_training import dataset_columns, simulate_run

logger = logging.getLogger("traffic_mind.generate_dataset")

# (run_id, scenario, kind, seed, sumocfg)
Job = Tuple[str, str, str, int, str]


def _import_builder():
    """Load simulation/traffic_mind/build_scenario.py (it lives outside the package)."""
    import importlib.util

    path = Path(__file__).resolve().parents[4] / "simulation" / "traffic_mind" / "build_scenario.py"
    spec = importlib.util.spec_from_file_location("traffic_mind_build_scenario", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module   # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


def _run_job(args: Tuple[Job, int, Optional[int]]) -> Tuple[str, pd.DataFrame, float]:
    job, stride, until = args
    run_id, scenario, kind, seed, sumocfg = job
    started = time.time()
    # Two SUMO processes starting at the same moment can pick the same TraCI port; the
    # loser fails to connect. Waiting a random moment and trying again fixes that.
    for attempt in range(1, 5):
        try:
            frame = simulate_run(Path(sumocfg), seed, run_id, scenario, kind, PredictionConfig(),
                                 stride_s=stride, until_s=until)
            return run_id, frame, time.time() - started
        except SimulationConnectionError:
            if attempt == 4:
                raise
            time.sleep(random.uniform(1.0, 4.0))
    raise AssertionError("unreachable")


def build_jobs(random_runs: int, named: bool, first_seed: int, work_dir: Path) -> List[Job]:
    builder = _import_builder()
    jobs: List[Job] = []
    work_dir.mkdir(parents=True, exist_ok=True)
    for i in range(random_runs):
        seed = first_seed + i
        name = f"rand_{seed:04d}"
        cfg = builder.build_random_scenario(name, seed, work_dir)
        jobs.append((name, name, "random", seed, str(cfg)))
    if named:
        for scenario in ("offpeak", "peak", "mixed", "surge"):
            cfg = SCENARIO_DIR / f"{scenario}.sumocfg"
            if not cfg.is_file():
                raise FileNotFoundError(f"{cfg} not found; run simulation/traffic_mind/build_scenario.py first.")
            jobs.append((f"named_{scenario}", scenario, "named", 42, str(cfg)))
    return jobs


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate the TrafficMind prediction dataset.")
    parser.add_argument("--random-runs", type=int, default=40, help="Number of random scenarios.")
    parser.add_argument("--no-named", action="store_true", help="Skip the four named scenarios.")
    parser.add_argument("--first-seed", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=2, help="Parallel SUMO processes.")
    parser.add_argument("--stride", type=int, default=15, help="Keep one row per junction every N seconds.")
    parser.add_argument("--until", type=int, default=None, help="Stop each run early (for quick tests).")
    parser.add_argument("--out", type=Path, default=DATASET_DIR / "traffic_dataset.csv")
    args = parser.parse_args(argv)

    configure_logging(logging.WARNING)
    if args.random_runs < 0 or args.workers < 1:
        parser.error("--random-runs must be >= 0 and --workers >= 1")
    work_dir = DATASET_DIR / "_generated_scenarios"
    try:
        jobs = build_jobs(args.random_runs, not args.no_named, args.first_seed, work_dir)
        if not jobs:
            parser.error("Nothing to run.")
        print(f"Running {len(jobs)} simulations with {args.workers} worker(s) ...")
        frames: List[pd.DataFrame] = []
        payload = [(job, args.stride, args.until) for job in jobs]
        with futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
            for done, (run_id, frame, seconds) in enumerate(pool.map(_run_job, payload), start=1):
                frames.append(frame)
                print(f"[{done:>3}/{len(jobs)}] {run_id:<14} {len(frame):>6} rows  ({seconds:.0f} s)")
    except (TrafficMindError, FileNotFoundError, RuntimeError, ImportError) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        logger.error("%s: %s", code, exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        return 130

    data = pd.concat(frames, ignore_index=True)[dataset_columns()]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    data.to_csv(args.out, index=False, float_format="%.4f")
    print(f"\nWrote {len(data)} rows from {data['run_id'].nunique()} runs to {args.out}")
    for level, count in data["current_level"].value_counts().items():
        print(f"  level now {level:<7} {count:>7} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())