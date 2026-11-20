"""
TrafficMind public interface (Phase 1 -> Phase 2 integration surface).

The four functions the Central Orchestrator will call. Each is a one-line wrapper
around :class:`TrafficMindAgent`, returns a JSON-ready dict, and does not raise:

* success: the documented result (``status`` is ``"ok"``)
* failure: ``{"status": "error", "code": "...", "message": "..."}``

Pass a traffic-status dict to run without SUMO. Omit it and the agent reads the
simulation passed to :func:`attach_simulation`. Call the four functions once per
sample, in order. A second call at the same simulation time reuses that sample.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Mapping, Optional

from .congestion_detector import CongestionDetector
from .exceptions import InvalidTrafficDataError, PredictionError, SimulationNotRunningError, TrafficMindError
from .schemas import CongestionReport, PredictionReport, TrafficSnapshot
from .signal_controller import SignalAdvisor
from .simulation_interface import SimulationInterface
from .traffic_monitor import TrafficMonitor
from .traffic_predictor import ModelBundle, TrafficPredictor

logger = logging.getLogger(__name__)

__all__ = [
    "TrafficMindAgent",
    "attach_simulation",
    "load_signal_programs",
    "get_traffic_status",
    "detect_congestion",
    "predict_congestion",
    "generate_signal_recommendation",
    "get_agent",
    "reset_agent",
]

_BUNDLES: Optional[List[ModelBundle]] = None


def _shared_bundles() -> List[ModelBundle]:
    """Load the trained models once per process (an empty list if none are installed)."""
    global _BUNDLES
    if _BUNDLES is None:
        loader = TrafficPredictor()
        loader.load_models()
        _BUNDLES = [loader._models[horizon] for horizon in loader.horizons]
    return _BUNDLES


class TrafficMindAgent:
    """One TrafficMind instance: monitor, detector, predictor and signal advisor.

    The detector is shared, so a sample is scored once even though prediction and
    signal advice both need the congestion report.
    """

    def __init__(self) -> None:
        self.detector = CongestionDetector()
        self.predictor = TrafficPredictor(detector=self.detector)
        self._models_ready = False
        self._sim: Optional[SimulationInterface] = None
        self.monitor: Optional[TrafficMonitor] = None
        self.advisor: Optional[SignalAdvisor] = None
        self._status: Optional[TrafficSnapshot] = None
        self._congestion: Optional[CongestionReport] = None
        self._prediction: Optional[PredictionReport] = None

    # ------------------------------------------------------------------ #
    # Setup (the orchestrator calls these; the four functions do not)
    # ------------------------------------------------------------------ #
    def attach(self, simulation: SimulationInterface) -> None:
        """Bind a running simulation. TrafficMind does not start or step SUMO."""
        if simulation is None or not getattr(simulation, "is_running", False):
            raise SimulationNotRunningError(
                "attach_simulation() needs a running SimulationInterface."
            )
        self.detector.reset()
        self.predictor.reset()
        self._sim = simulation
        self.monitor = TrafficMonitor.from_source(simulation)
        self.advisor = SignalAdvisor.from_source(simulation, detector=self.detector)
        self._clear_sample()
        logger.info("TrafficMind attached to simulation %s.", simulation.config.label)

    def load_programs(self, programs: Mapping[str, Any]) -> None:
        """Install signal programs when no simulation is attached (tests, replay)."""
        if not programs:
            raise InvalidTrafficDataError(
                "load_signal_programs() needs at least one signal program."
            )
        self.advisor = SignalAdvisor(programs, detector=self.detector)
        logger.info("TrafficMind loaded signal programs for %s.", ", ".join(sorted(programs)))

    def reset(self) -> None:
        """Forget history (call after a simulation restart, before the next sample)."""
        if self.monitor is not None:
            self.monitor.reset()
        self.detector.reset()
        self.predictor.reset()
        if self.advisor is not None:
            self.advisor.reset()
        self._clear_sample()

    # ------------------------------------------------------------------ #
    # The four operations
    # ------------------------------------------------------------------ #
    def get_traffic_status(self, snapshot: Any = None) -> TrafficSnapshot:
        """Current metrics. ``snapshot`` is a traffic-status dict, or omitted to read SUMO."""
        return self._snapshot(snapshot)

    def detect_congestion(self, snapshot: Any = None) -> CongestionReport:
        """Congestion levels, alerts and the attention list for the current sample."""
        snap = self._snapshot(snapshot)
        if self._congestion is not None and _same_time(self._congestion.timestamp_s, snap.timestamp_s):
            return self._congestion
        latest = self.detector.latest
        if latest is not None and _same_time(latest.timestamp_s, snap.timestamp_s):
            self._congestion = latest
            return latest
        self._congestion = self.detector.update(snap)
        self._prediction = None
        return self._congestion

    def predict_congestion(self, snapshot: Any = None, horizon_s: Optional[int] = None) -> PredictionReport:
        """Congestion forecast. ``horizon_s`` is 30, 60 or 120 (default 60)."""
        if horizon_s is not None and (isinstance(horizon_s, bool) or not isinstance(horizon_s, int) or horizon_s <= 0):
            raise PredictionError(
                f"horizon_s must be a positive whole number of seconds, got {horizon_s!r}"
            )
        self._ensure_models()
        snap = self._snapshot(snapshot)
        if (
            self._prediction is not None
            and _same_time(self._prediction.timestamp_s, snap.timestamp_s)
            and (horizon_s is None or horizon_s == self._prediction.horizon_s)
        ):
            return self._prediction
        report = self._congestion_for(snap)
        already = (
            self.predictor._builder.latest_time is not None
            and _same_time(self.predictor._builder.latest_time, snap.timestamp_s)
        )
        if already:
            self._prediction = self.predictor.predict(horizon_s=horizon_s)
        else:
            self._prediction = self.predictor.predict(snap, report, horizon_s=horizon_s)
        return self._prediction

    def generate_signal_recommendation(self, snapshot: Any = None):
        """Green-time advice. Nothing is sent to a signal. A forecast is used only if one was just made."""
        if self.advisor is None:
            raise SimulationNotRunningError(
                "No signal programs loaded. Call attach_simulation(sim) or "
                "load_signal_programs(programs) first."
            )
        snap = self._snapshot(snapshot)
        congestion = self._congestion_for(snap)
        prediction = (
            self._prediction
            if self._prediction is not None and _same_time(self._prediction.timestamp_s, snap.timestamp_s)
            else None
        )
        return self.advisor.recommend(snap, congestion, prediction)

    # ------------------------------------------------------------------ #
    def _ensure_models(self) -> None:
        if self._models_ready:
            return
        for bundle in _shared_bundles():
            self.predictor.add_model(bundle)
        self._models_ready = True

    def _congestion_for(self, snap: TrafficSnapshot) -> CongestionReport:
        if self._congestion is not None and _same_time(self._congestion.timestamp_s, snap.timestamp_s):
            return self._congestion
        return self.detect_congestion(snap)

    def _snapshot(self, snapshot: Any) -> TrafficSnapshot:
        if snapshot is not None:
            snap = _as_snapshot(snapshot)
            self._remember(snap)
            return snap
        if self._status is not None and self._sim is not None and self._sim.is_running:
            if _same_time(self._sim.current_time, self._status.timestamp_s):
                return self._status
        if self._status is not None and (self._sim is None or not self._sim.is_running):
            return self._status
        if self.monitor is None or self._sim is None or not self._sim.is_running:
            raise SimulationNotRunningError(
                "No running simulation. Call attach_simulation(sim), or pass a traffic snapshot."
            )
        snap = self.monitor.sample(self._sim)
        self._remember(snap)
        return snap

    def _remember(self, snap: TrafficSnapshot) -> None:
        if self._status is None or not _same_time(self._status.timestamp_s, snap.timestamp_s):
            self._congestion = None
            self._prediction = None
        self._status = snap

    def _clear_sample(self) -> None:
        self._status = None
        self._congestion = None
        self._prediction = None


def _same_time(left: float, right: float) -> bool:
    return abs(left - right) < 1e-6


def _as_snapshot(snapshot: Any) -> TrafficSnapshot:
    if isinstance(snapshot, TrafficSnapshot):
        return snapshot
    try:
        return TrafficSnapshot.model_validate(snapshot)
    except Exception as exc:
        raise InvalidTrafficDataError(f"Invalid traffic snapshot: {exc}") from exc


_agent: Optional[TrafficMindAgent] = None


def get_agent() -> TrafficMindAgent:
    """The process-wide agent. The orchestrator should use this single instance."""
    global _agent
    if _agent is None:
        _agent = TrafficMindAgent()
    return _agent


def reset_agent() -> None:
    """Drop the cached agent (tests, and after the orchestrator closes a simulation)."""
    global _agent
    _agent = None


def _run(fn: Callable[[], Any]) -> Dict[str, Any]:
    try:
        result = fn()
        return result.to_dict() if hasattr(result, "to_dict") else dict(result)
    except TrafficMindError as exc:
        logger.warning("%s: %s", exc.code, exc)
        return exc.to_dict()
    except Exception as exc:  # the orchestrator always gets a dict
        logger.exception("TrafficMind call failed")
        return {"status": "error", "code": "TRAFFICMIND_ERROR", "message": str(exc)}


def attach_simulation(simulation: SimulationInterface) -> None:
    """Bind the shared agent to a simulation the orchestrator already started."""
    get_agent().attach(simulation)


def load_signal_programs(programs: Mapping[str, Any]) -> None:
    """Bind signal programs for replay or tests, when no simulation is attached."""
    get_agent().load_programs(programs)


def get_traffic_status(snapshot: Optional[dict] = None) -> dict:
    """Vehicle counts, density, queues, waiting time, occupancy and flow."""
    return _run(lambda: get_agent().get_traffic_status(snapshot))


def detect_congestion(snapshot: Optional[dict] = None) -> dict:
    """LOW / MEDIUM / HIGH per road and junction, alerts, and junctions that need attention."""
    return _run(lambda: get_agent().detect_congestion(snapshot))


def predict_congestion(snapshot: Optional[dict] = None, horizon_s: Optional[int] = None) -> dict:
    """Predicted congestion. Uses the trained model, or 'no change' when it cannot."""
    return _run(lambda: get_agent().predict_congestion(snapshot, horizon_s))


def generate_signal_recommendation(snapshot: Optional[dict] = None) -> dict:
    """Recommended green durations. Advice only; the orchestrator decides whether to apply it."""
    return _run(lambda: get_agent().generate_signal_recommendation(snapshot))
