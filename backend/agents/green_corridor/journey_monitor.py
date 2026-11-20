"""Emergency journey monitoring and baseline-vs-priority comparison (Step 8).

JourneyMonitor measures one ambulance journey from the live simulation: journey time, distance, average
speed, intersections crossed, waiting time at intersections and the planner's estimate. It only READS from
the detector and the intersection monitor, so it never changes the simulation.

run_scenario() drives one full SUMO session. compare_journeys() runs the same scenario twice:

    baseline  baseline.sumocfg, no PriorityManager, signals run their normal fixed-time program
    priority  priority.sumocfg, PriorityManager.update() called every step

The two .sumocfg files are identical apart from the tripinfo file name, and both runs use the same seed and
the same traffic files, so the only difference between them is whether PriorityManager acts.

Time saved is baseline minus priority, measured, never clamped (it can be zero or negative). Every run is
cross-checked against SUMO's own tripinfo log.

Public, orchestrator-facing functions (never raise; return the JSON envelope):
    monitor_emergency_journey(journey, vehicle_id)
    compare_baseline_vs_priority(scenario, vehicle_id=None, respect_min_dur=None)

Run:  python -m backend.agents.green_corridor.journey_monitor --help
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from . import priority_manager as pm
from .api_utils import guarded
from .connection import SumoConnection
from .emergency_detector import EmergencyDetector
from .intersection_monitor import IntersectionMonitor
from .route_planner import RoutePlanner
from .scenario_builder import ScenarioError, ScenarioFiles, load_scenario
from .schemas import to_jsonable

logger = logging.getLogger(__name__)

MODES = ("baseline", "priority")
JOURNEY_TIME_TOL_STEPS = 3        # live vs tripinfo duration may differ by this many steps (step rounding)
DISTANCE_TOL_M = 30.0             # live vs tripinfo distance: the live value misses the final partial step


class JourneyError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---- data types ----------------------------------------------------------------------------------
@dataclass
class JourneyMetrics:
    vehicle_id: str
    sim_time_s: float
    finished: bool
    vehicle_lost: bool                    # left the simulation without a recorded arrival
    depart_time_s: float                  # first step the vehicle was seen (1 step resolution)
    arrival_time_s: Optional[float]
    journey_time_s: float                 # so far, or final once finished
    estimated_journey_time_s: Optional[float]   # planner estimate at departure, excludes signal waiting
    delay_vs_estimate_s: Optional[float]        # journey_time - estimate, only once finished
    distance_travelled_m: float
    average_speed_mps: float
    average_speed_kmh: float
    signalised_intersections_crossed: int
    junctions_crossed: int
    intersection_wait_time_s: float       # stopped time attributed to a signal (see config.WAIT_ATTRIBUTION_ZONE_M)
    stopped_time_s: float                 # all time spent stopped, anywhere
    crossings: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(asdict(self))


@dataclass
class RunResult:
    mode: str
    cfg: str
    vehicle_id: str
    completed: bool
    stop_reason: str                      # ARRIVED | TIME_LIMIT | VEHICLE_LOST
    sim_time_end_s: float
    metrics: Optional[JourneyMetrics]
    tripinfo: Optional[Dict[str, Any]]    # SUMO's own record of the trip
    cross_check: Optional[Dict[str, Any]]
    respect_min_dur: Optional[bool]       # priority runs only
    priority: Optional[Dict[str, Any]]    # priority runs only
    priority_errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(asdict(self))


@dataclass
class ComparisonReport:
    vehicle_id: str
    seed: int
    completed_both: bool
    respect_min_dur: bool
    baseline: RunResult
    priority: RunResult
    time_saved_s: Optional[float]         # baseline - priority, not clamped
    time_saved_pct: Optional[float]
    intersection_wait_saved_s: Optional[float]
    stopped_time_saved_s: Optional[float]
    average_speed_change_kmh: Optional[float]
    notes: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(asdict(self))


# ---- per-step journey tracking ---------------------------------------------------------------------
@dataclass
class _Journey:
    depart_time_s: float
    estimate_s: Optional[float] = None
    distance_m: float = 0.0
    stopped_s: float = 0.0
    arrival_time_s: Optional[float] = None
    lost: bool = False


class JourneyMonitor:
    """Call ``update(vehicle_id)`` once per simulation step. Quiet no-op until the vehicle is detected."""

    def __init__(self, detector: EmergencyDetector, monitor: IntersectionMonitor,
                 planner: Optional[RoutePlanner] = None):
        self.detector = detector
        self.monitor = monitor
        self.planner = planner or monitor._planner
        self._journeys: Dict[str, _Journey] = {}

    @staticmethod
    def _check_id(vehicle_id: Any) -> None:
        if not isinstance(vehicle_id, str) or not vehicle_id.strip():
            raise JourneyError("INVALID_INPUT", "vehicle_id must be a non-empty string")

    def has_started(self, vehicle_id: str) -> bool:
        return vehicle_id in self._journeys

    def update(self, vehicle_id: str) -> None:
        self._check_id(vehicle_id)
        tc = self.monitor._tc
        now = tc.simulation.getTime()
        self.detector.update()                      # both calls are idempotent within one step
        self.monitor.update(vehicle_id)
        present = vehicle_id in tc.vehicle.getIDList()

        j = self._journeys.get(vehicle_id)
        if j is None:
            if not present or vehicle_id not in self.detector.known_vehicle_ids():
                return                              # not departed yet, or not an emergency vehicle
            j = _Journey(depart_time_s=now)
            self._journeys[vehicle_id] = j
            try:                                    # read-only: apply=False never touches the vehicle
                j.estimate_s = self.planner.plan_for_vehicle(vehicle_id, apply=False).estimated_travel_time_s
            except Exception as exc:
                logger.warning("No route estimate for %s: %s", vehicle_id, exc)

        if j.arrival_time_s is not None or j.lost:
            return
        if present:
            j.distance_m = tc.vehicle.getDistance(vehicle_id)
            if tc.vehicle.getSpeed(vehicle_id) < config.STOPPED_SPEED_MPS:
                j.stopped_s += tc.simulation.getDeltaT()
        elif vehicle_id in self.detector._arrived:
            j.arrival_time_s = now
            logger.info("Journey finished: %s at t=%.0f", vehicle_id, now)
        else:
            j.lost = True
            logger.warning("%s left the simulation without arriving", vehicle_id)

    def metrics(self, vehicle_id: str) -> JourneyMetrics:
        self._check_id(vehicle_id)
        j = self._journeys.get(vehicle_id)
        if j is None:
            raise JourneyError("JOURNEY_NOT_STARTED",
                               f"'{vehicle_id}' has not been detected yet (not departed, or not an emergency vehicle)")
        now = self.monitor._tc.simulation.getTime()
        finished = j.arrival_time_s is not None
        end = j.arrival_time_s if finished else now
        journey_time = max(0.0, end - j.depart_time_s)
        crossings = self.monitor.crossing_log(vehicle_id)
        track = self.monitor._tracks.get(vehicle_id)
        avg = j.distance_m / journey_time if journey_time > 0 else 0.0
        return JourneyMetrics(
            vehicle_id=vehicle_id, sim_time_s=now, finished=finished, vehicle_lost=j.lost,
            depart_time_s=j.depart_time_s, arrival_time_s=j.arrival_time_s, journey_time_s=journey_time,
            estimated_journey_time_s=j.estimate_s,
            delay_vs_estimate_s=(journey_time - j.estimate_s) if finished and j.estimate_s is not None else None,
            distance_travelled_m=j.distance_m, average_speed_mps=avg, average_speed_kmh=avg * 3.6,
            signalised_intersections_crossed=len(crossings),
            junctions_crossed=track.junctions_crossed if track else 0,
            intersection_wait_time_s=sum(c.wait_time_s for c in crossings), stopped_time_s=j.stopped_s,
            crossings=[asdict(c) for c in crossings])


# ---- SUMO's own record, used as a cross-check -------------------------------------------------------
def read_tripinfo(path: str | Path, vehicle_id: str) -> Optional[Dict[str, Any]]:
    """Read one vehicle's entry from a SUMO tripinfo file; None if the file or the entry is missing."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        root = ET.parse(p).getroot()
    except ET.ParseError:
        return None
    for t in root.iter("tripinfo"):
        if t.get("id") == vehicle_id:
            return {"depart_s": float(t.get("depart", "nan")), "arrival_s": float(t.get("arrival", "nan")),
                    "duration_s": float(t.get("duration", "nan")),
                    "route_length_m": float(t.get("routeLength", "nan")),
                    "waiting_time_s": float(t.get("waitingTime", "nan")),
                    "waiting_count": int(t.get("waitingCount", "0")),
                    "time_loss_s": float(t.get("timeLoss", "nan"))}
    return None


def _cross_check(metrics: Optional[JourneyMetrics], trip: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if metrics is None or trip is None or not metrics.finished:
        return None
    t_diff = metrics.journey_time_s - trip["duration_s"]
    d_diff = metrics.distance_travelled_m - trip["route_length_m"]
    tol_t = JOURNEY_TIME_TOL_STEPS * config.SIM_STEP_LENGTH_S
    return {"journey_time_diff_s": t_diff, "distance_diff_m": d_diff,
            "time_tolerance_s": tol_t, "distance_tolerance_m": DISTANCE_TOL_M,
            "consistent": abs(t_diff) <= tol_t and abs(d_diff) <= DISTANCE_TOL_M}


def _summarise_priority(seen: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    reqs = list(seen.values())
    calls = [a for r in reqs for a in r["actions"] if a["call"].startswith("setPhaseDuration")]
    return {
        "requests": len(reqs),
        "by_state": dict(Counter(r["state"] for r in reqs)),
        "tls_ids": sorted({r["tls_id"] for r in reqs}),
        "signal_interventions": len(calls),
        "extensions": sum(1 for a in calls if a["reason"].startswith("extend")),
        "truncations": sum(1 for a in calls if a["reason"].startswith("truncate")),
        "restores": sum(1 for a in calls if a["reason"].startswith("restore")),
        "actions": [a for r in reqs for a in r["actions"]],
    }


@contextmanager
def _min_dur_override(value: Optional[bool]):
    """Temporarily set priority_manager.PRIORITY_RESPECT_MIN_DUR (None leaves config as is)."""
    if value is None:
        yield
        return
    old = pm.PRIORITY_RESPECT_MIN_DUR
    pm.PRIORITY_RESPECT_MIN_DUR = bool(value)
    try:
        yield
    finally:
        pm.PRIORITY_RESPECT_MIN_DUR = old


# ---- one full run ------------------------------------------------------------------------------------
def run_scenario(scenario: ScenarioFiles, mode: str, vehicle_id: Optional[str] = None,
                 respect_min_dur: Optional[bool] = None, gui: bool = False) -> RunResult:
    """Run one complete SUMO session until the ambulance arrives (or the scenario time limit)."""
    if mode not in MODES:
        raise JourneyError("INVALID_INPUT", f"mode must be one of {MODES}")
    spec = scenario.spec
    vid = vehicle_id or spec["ambulance_id"]
    if not isinstance(vid, str) or not vid.strip():
        raise JourneyError("INVALID_INPUT", "vehicle_id must be a non-empty string")
    is_priority = mode == "priority"
    cfg = scenario.priority_cfg if is_priority else scenario.baseline_cfg
    tripinfo_path = scenario.priority_tripinfo if is_priority else scenario.baseline_tripinfo
    Path(tripinfo_path).unlink(missing_ok=True)     # never read a stale log from an earlier run
    end_s = float(spec["sim_end_s"])
    limit_s = end_s - 2 * config.SIM_STEP_LENGTH_S  # stop before SUMO itself ends the session

    seen: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []
    metrics: Optional[JourneyMetrics] = None
    stop_reason, now, effective = "TIME_LIMIT", 0.0, None

    with _min_dur_override(respect_min_dur if is_priority else None):
        effective = pm.PRIORITY_RESPECT_MIN_DUR if is_priority else None
        with SumoConnection(gui=gui, seed=int(spec["seed"])) as sc:
            sc.start(cfg)
            detector = EmergencyDetector(sc)
            planner = RoutePlanner(sc)
            monitor = IntersectionMonitor(sc, planner)
            journey = JourneyMonitor(detector, monitor, planner)
            manager = pm.PriorityManager(monitor) if is_priority else None
            logger.info("Run '%s' started (%s)", mode, cfg)
            while True:
                now = sc.step()
                journey.update(vid)
                if manager is not None:
                    res = manager.update(vid)
                    if res["success"]:
                        for r in res["data"]["active_requests"]:
                            seen[r["request_id"]] = r
                    else:
                        errors.append(f"t={now:.0f} {res['error']['code']}: {res['error']['message']}")
                j = journey._journeys.get(vid)
                if j is not None and j.arrival_time_s is not None:
                    stop_reason = "ARRIVED"
                    break
                if j is not None and j.lost:
                    stop_reason = "VEHICLE_LOST"
                    break
                if now >= limit_s:
                    break
            if journey.has_started(vid):
                metrics = journey.metrics(vid)

    trip = read_tripinfo(tripinfo_path, vid)        # written when SUMO closes, so read after the session
    return RunResult(
        mode=mode, cfg=str(cfg), vehicle_id=vid, completed=stop_reason == "ARRIVED", stop_reason=stop_reason,
        sim_time_end_s=now, metrics=metrics, tripinfo=trip, cross_check=_cross_check(metrics, trip),
        respect_min_dur=effective, priority=_summarise_priority(seen) if is_priority else None,
        priority_errors=errors)


# ---- comparison ----------------------------------------------------------------------------------------
def compare_journeys(scenario: ScenarioFiles, vehicle_id: Optional[str] = None,
                     respect_min_dur: Optional[bool] = None) -> ComparisonReport:
    """Run baseline then priority on the same scenario and report the measured difference."""
    base = run_scenario(scenario, "baseline", vehicle_id)
    prio = run_scenario(scenario, "priority", vehicle_id, respect_min_dur=respect_min_dur)
    both = base.completed and prio.completed
    notes: List[str] = [
        "Single run per mode with one seed. The difference is measured, not averaged over many scenarios."]
    saved = pct = wait_saved = stop_saved = speed_chg = None
    if both:
        b, p = base.metrics, prio.metrics
        saved = b.journey_time_s - p.journey_time_s
        pct = (saved / b.journey_time_s * 100.0) if b.journey_time_s > 0 else None
        wait_saved = b.intersection_wait_time_s - p.intersection_wait_time_s
        stop_saved = b.stopped_time_s - p.stopped_time_s
        speed_chg = p.average_speed_kmh - b.average_speed_kmh
        if prio.priority and prio.priority["signal_interventions"] == 0:
            notes.append("The priority manager made no signal changes in this run, so both runs should be "
                         "identical. With the fixed-time network and PRIORITY_RESPECT_MIN_DUR=True it can only "
                         "extend the ambulance's own green, and nothing may need extending.")
    else:
        for r in (base, prio):
            if not r.completed:
                notes.append(f"The {r.mode} run did not finish ({r.stop_reason}), so no time saving is reported.")
    for r in (base, prio):
        if r.cross_check is not None and not r.cross_check["consistent"]:
            notes.append(f"The {r.mode} run disagrees with SUMO's tripinfo log beyond tolerance.")
        if r.completed and r.tripinfo is None:
            notes.append(f"No tripinfo entry was found for the {r.mode} run, so it was not cross-checked.")
    if prio.priority_errors:
        notes.append(f"The priority manager reported {len(prio.priority_errors)} error(s) during the run.")
    return ComparisonReport(
        vehicle_id=base.vehicle_id, seed=int(scenario.spec["seed"]),
        completed_both=both, respect_min_dur=bool(prio.respect_min_dur), baseline=base, priority=prio,
        time_saved_s=saved, time_saved_pct=pct, intersection_wait_saved_s=wait_saved,
        stopped_time_saved_s=stop_saved, average_speed_change_kmh=speed_chg, notes=notes)


# ---- public, orchestrator-facing functions ------------------------------------------------------------
def monitor_emergency_journey(journey: JourneyMonitor, vehicle_id: str) -> Dict[str, Any]:
    """Live journey metrics for one emergency vehicle (call every simulation step).

    Args:
        journey: JourneyMonitor bound to a live SUMO/TraCI connection.
        vehicle_id: SUMO vehicle id, e.g. "amb_1".
    Returns (envelope): data = JourneyMetrics dict {finished, journey_time_s, estimated_journey_time_s,
        distance_travelled_m, average_speed_kmh, signalised_intersections_crossed, junctions_crossed,
        intersection_wait_time_s, stopped_time_s, crossings[]}.
    Errors: INVALID_INPUT, JOURNEY_NOT_STARTED, CONNECTION_LOST, NOT_CONNECTED, TRACI_ERROR.
    """
    def run():
        journey.update(vehicle_id)
        return journey.metrics(vehicle_id).to_dict()
    return guarded(run)


def compare_baseline_vs_priority(scenario: ScenarioFiles, vehicle_id: Optional[str] = None,
                                 respect_min_dur: Optional[bool] = None) -> Dict[str, Any]:
    """Run the scenario without and with signal priority and return the measured comparison.

    Args:
        scenario: ScenarioFiles from build_scenario() or load_scenario().
        vehicle_id: defaults to the scenario's ambulance.
        respect_min_dur: override priority_manager.PRIORITY_RESPECT_MIN_DUR for the priority run only.
    Returns (envelope): data = ComparisonReport dict {time_saved_s, time_saved_pct, intersection_wait_saved_s,
        stopped_time_saved_s, average_speed_change_kmh, baseline, priority, notes}. time_saved_s is baseline
        minus priority and may be zero or negative.
    Errors: INVALID_INPUT, SUMO_NOT_FOUND, START_FAILED, CONNECTION_LOST, TRACI_ERROR.
    """
    return guarded(lambda: compare_journeys(scenario, vehicle_id, respect_min_dur).to_dict())


# ---- command line ----------------------------------------------------------------------------------------
def _fmt(v: Optional[float], unit: str = "") -> str:
    return "n/a" if v is None else f"{v:.1f}{unit}"


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Compare the ambulance journey with and without signal priority")
    p.add_argument("--respect-min-dur", action=argparse.BooleanOptionalAction, default=None,
                   help="override PRIORITY_RESPECT_MIN_DUR for the priority run")
    p.add_argument("--vehicle-id", default=None)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        report = compare_journeys(load_scenario(), a.vehicle_id, a.respect_min_dur)
    except (ScenarioError, JourneyError) as exc:
        print(f"[FAIL] {exc.code}: {exc.message}")
        return 1
    rows = [("journey time (s)", "journey_time_s"), ("distance (m)", "distance_travelled_m"),
            ("average speed (km/h)", "average_speed_kmh"), ("signalised intersections", "signalised_intersections_crossed"),
            ("waiting at intersections (s)", "intersection_wait_time_s"), ("stopped time (s)", "stopped_time_s")]
    print(f"\nvehicle {report.vehicle_id}, seed {report.seed}, respect_min_dur={report.respect_min_dur}")
    print(f"{'':32}{'baseline':>12}{'priority':>12}")
    for label, key in rows:
        vals = [getattr(r.metrics, key) if r.metrics else None for r in (report.baseline, report.priority)]
        print(f"{label:32}" + "".join(f"{_fmt(v):>12}" for v in vals))
    print(f"\ntime saved: {_fmt(report.time_saved_s, ' s')} ({_fmt(report.time_saved_pct, ' %')})")
    if report.priority.priority:
        s = report.priority.priority
        print(f"signal interventions: {s['signal_interventions']} "
              f"(extensions {s['extensions']}, truncations {s['truncations']}, restores {s['restores']})")
    for n in report.notes:
        print(f"note: {n}")
    out = config.RESULTS_DIR / "comparison.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.to_dict(), indent=2) + "\n")
    print(f"\nfull report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())