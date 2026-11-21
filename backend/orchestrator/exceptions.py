"""Typed exceptions for the Traffora-AI Central Orchestrator.

Every exception carries a stable ``code`` so the FastAPI layer (or a CLI) can
map failures to responses without string matching. ``to_dict()`` returns a
JSON-ready payload, mirroring the convention already used by the three agents.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class OrchestratorError(Exception):
    """Base class for every orchestrator failure."""

    code: str = "ORCHESTRATOR_ERROR"

    def __init__(self, message: str, *, code: Optional[str] = None,
                 details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"status": "error", "code": self.code,
                                   "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class ConfigurationError(OrchestratorError):
    """Invalid or missing orchestrator configuration."""

    code = "CONFIGURATION_ERROR"


class AgentInitializationError(OrchestratorError):
    """An agent adapter could not be initialised."""

    code = "AGENT_INIT_ERROR"


class AgentNotFoundError(OrchestratorError):
    """A requested agent is not registered."""

    code = "AGENT_NOT_FOUND"


class SimulationError(OrchestratorError):
    """Base class for simulation problems."""

    code = "SIMULATION_ERROR"


class SimulationNotRunningError(SimulationError):
    """An operation needs a running simulation."""

    code = "SIMULATION_NOT_RUNNING"


class SimulationStartError(SimulationError):
    """SUMO could not be started."""

    code = "SIMULATION_START_ERROR"


class EventValidationError(OrchestratorError):
    """An event payload is malformed or inconsistent."""

    code = "EVENT_VALIDATION_ERROR"


class DuplicateEventError(OrchestratorError):
    """The same event was already published inside the dedupe window."""

    code = "DUPLICATE_EVENT"


class SafetyViolationError(OrchestratorError):
    """A signal action violates a hard safety rule."""

    code = "SAFETY_VIOLATION"


class DecisionError(OrchestratorError):
    """The decision engine could not produce a decision."""

    code = "DECISION_ERROR"


class SharedStateError(OrchestratorError):
    """Shared state was accessed in an invalid way."""

    code = "SHARED_STATE_ERROR"
