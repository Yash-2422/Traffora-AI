"""TrafficMind - intelligent traffic management agent for VELORA AI.

Phase 1 (standalone) package. Nothing here depends on GreenCorridor,
ParkSense, or the Central Orchestrator.

The orchestrator calls the four functions in ``agent.py``:

    get_traffic_status, detect_congestion,
    predict_congestion, generate_signal_recommendation
"""

__version__ = "0.1.0"

from .agent import (
    attach_simulation,
    detect_congestion,
    generate_signal_recommendation,
    get_traffic_status,
    load_signal_programs,
    predict_congestion,
    reset_agent,
)

__all__ = [
    "attach_simulation",
    "detect_congestion",
    "generate_signal_recommendation",
    "get_traffic_status",
    "load_signal_programs",
    "predict_congestion",
    "reset_agent",
]