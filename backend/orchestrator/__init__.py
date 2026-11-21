"""Traffora-AI central orchestrator.

Public surface
--------------
``TrafforaOrchestrator``
    The composition root. Owns the shared state, the three agent adapters, the
    event manager, the decision engine, the safety validator and the single
    SUMO session.
``OrchestratorConfig``
    Paths and tunables (safety limits, decision thresholds, scenario).
``SharedState``
    Thread-safe store of the latest city state, agent health and decisions.
``AgentRegistry`` / ``build_default_registry``
    Adapters that wrap TrafficMind, GreenCorridor and ParkSense without
    modifying them.
``EventManager``
    Priority-ordered, de-duplicated event queue.
``DecisionEngine``
    Deterministic priority resolution: emergency > traffic > parking.
``SafetyValidator``
    Independent gate that every signal write must pass.
``SimulationCoordinator``
    The one and only SUMO/TraCI session.

Importing this package does **not** import TraCI, so the schema, state, event,
decision and safety modules stay usable (and testable) without SUMO installed.
"""

from __future__ import annotations

from .agent_registry import (
    AgentAdapter,
    AgentRegistry,
    AgentSnapshot,
    GreenCorridorAdapter,
    ParkSenseAdapter,
    TrafficMindAdapter,
    build_default_registry,
)
from .config import DecisionSettings, OrchestratorConfig, SafetyLimits, configure_logging
from .decision_engine import DecisionEngine
from .event_manager import EventManager
from .exceptions import (
    AgentInitializationError,
    AgentNotFoundError,
    ConfigurationError,
    DecisionError,
    DuplicateEventError,
    EventValidationError,
    OrchestratorError,
    SafetyViolationError,
    SharedStateError,
    SimulationError,
    SimulationNotRunningError,
    SimulationStartError,
)
from .orchestrator import TrafforaOrchestrator
from .safety_validator import SafetyValidator
from .schemas import (
    ActionType,
    AgentHealth,
    AgentName,
    AgentRecommendation,
    AgentStatus,
    CityState,
    DecisionType,
    EmergencyEvent,
    Event,
    EventPriority,
    EventStatus,
    EventType,
    OrchestratorDecision,
    ParkingRecommendation,
    ParkingState,
    ParkingZoneState,
    SignalAction,
    SystemMetrics,
    SystemStatus,
    TrafficState,
    ValidationResult,
    ValidationStatus,
)
from .shared_state import SharedState
from .simulation_coordinator import SimulationCoordinator

__all__ = [
    # orchestrator
    "TrafforaOrchestrator",
    # configuration
    "OrchestratorConfig",
    "SafetyLimits",
    "DecisionSettings",
    "configure_logging",
    # state / coordination
    "SharedState",
    "AgentRegistry",
    "AgentAdapter",
    "AgentSnapshot",
    "TrafficMindAdapter",
    "GreenCorridorAdapter",
    "ParkSenseAdapter",
    "build_default_registry",
    "EventManager",
    "DecisionEngine",
    "SafetyValidator",
    "SimulationCoordinator",
    # schemas
    "CityState",
    "TrafficState",
    "EmergencyEvent",
    "ParkingState",
    "ParkingZoneState",
    "AgentStatus",
    "AgentRecommendation",
    "SignalAction",
    "ParkingRecommendation",
    "OrchestratorDecision",
    "SystemMetrics",
    "SystemStatus",
    "ValidationResult",
    "Event",
    "EventPriority",
    "EventType",
    "EventStatus",
    "AgentName",
    "AgentHealth",
    "DecisionType",
    "ActionType",
    "ValidationStatus",
    # exceptions
    "OrchestratorError",
    "ConfigurationError",
    "AgentInitializationError",
    "AgentNotFoundError",
    "SimulationError",
    "SimulationNotRunningError",
    "SimulationStartError",
    "EventValidationError",
    "DuplicateEventError",
    "SafetyViolationError",
    "DecisionError",
    "SharedStateError",
]
