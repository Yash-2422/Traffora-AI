"""Custom exceptions for the TrafficMind agent.

Every exception carries a machine-readable ``code`` and can be converted to
the structured error object returned by the agent's public functions:

    {"status": "error", "code": "SIMULATION_CONNECTION", "message": "..."}
"""

from __future__ import annotations

from typing import Any, Dict


class TrafficMindError(Exception):
    """Base class for all TrafficMind errors."""

    code = "TRAFFICMIND_ERROR"

    def to_dict(self) -> Dict[str, Any]:
        return {"status": "error", "code": self.code, "message": str(self)}


class SimulationConnectionError(TrafficMindError):
    """SUMO could not be started, or the TraCI connection was lost."""

    code = "SIMULATION_CONNECTION"


class SumoNotFoundError(SimulationConnectionError):
    """The SUMO executable could not be located."""

    code = "SUMO_NOT_FOUND"


class ScenarioNotFoundError(SimulationConnectionError):
    """The requested scenario / .sumocfg file does not exist."""

    code = "SCENARIO_NOT_FOUND"


class SimulationNotRunningError(TrafficMindError):
    """An operation needs a running simulation but none is active."""

    code = "SIMULATION_NOT_RUNNING"


class InvalidTrafficDataError(TrafficMindError):
    """Traffic data is missing, malformed, or out of range."""

    code = "INVALID_TRAFFIC_DATA"


class UnsafeSignalPlanError(TrafficMindError):
    """A signal plan violates a safety rule (min/max green, clearance, phases)."""

    code = "UNSAFE_SIGNAL_PLAN"


class PredictionError(TrafficMindError):
    """Traffic prediction could not be carried out."""

    code = "PREDICTION_ERROR"


class ModelLoadError(PredictionError):
    """A trained prediction model is missing, unreadable, or does not match the code."""

    code = "MODEL_LOAD_ERROR"