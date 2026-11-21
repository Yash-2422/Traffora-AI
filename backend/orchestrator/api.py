"""HTTP API for the Traffora-AI orchestrator.

This is a thin transport layer. Every endpoint delegates to a service method
that already exists on :class:`~backend.orchestrator.orchestrator.TrafforaOrchestrator`
and is covered by the unit tests, so there is no control logic here: no
decision is made, no signal action is validated and no SUMO command is issued
in this module.

Design notes
------------
* Importing this module does **not** start SUMO and does **not** import the
  three agents. The orchestrator is built lazily on the first request that
  needs it, behind a lock, so ``uvicorn`` can import the app cheaply and the
  process stays idle until ``POST /simulation/start``.
* The simulation is never advanced implicitly. ``POST /simulation/step`` is
  the only way to move simulated time forward, which keeps the HTTP surface
  deterministic and easy to drive from a dashboard or a test.
* ``OrchestratorError`` (and its subclasses) map to a JSON body with the
  error ``code``, so clients can branch on a stable identifier instead of
  parsing a message.

Run it with::

    python -m backend.orchestrator.api --port 8000

or::

    uvicorn backend.orchestrator.api:app --port 8000
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import OrchestratorConfig, configure_logging
from .exceptions import OrchestratorError, SimulationNotRunningError
from .orchestrator import TrafforaOrchestrator

logger = logging.getLogger(__name__)

_BUILD_LOCK = threading.Lock()


def _serialise(value: Any) -> Any:
    """Convert Pydantic models, dataclasses and containers to plain JSON.

    The service methods return a mix of Pydantic models (``CityState``,
    ``SystemStatus``), plain dicts and lists of either. Rather than declare a
    response model per endpoint - which would duplicate the schema layer - we
    normalise once here using ``model_dump(mode="json")``.
    """
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_serialise(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialise(item) for key, item in value.items()}
    return value


def get_orchestrator(request: Request) -> TrafforaOrchestrator:
    """FastAPI dependency returning the process-wide orchestrator.

    Built lazily so that importing the app costs nothing and so that tests can
    inject a fake by setting ``app.state.orchestrator`` before the first call.
    """
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator is not None:
        return orchestrator

    with _BUILD_LOCK:
        orchestrator = getattr(request.app.state, "orchestrator", None)
        if orchestrator is None:
            config = getattr(request.app.state, "config", None) or OrchestratorConfig()
            logger.info("building orchestrator on first use")
            orchestrator = TrafforaOrchestrator(config)
            request.app.state.orchestrator = orchestrator
    return orchestrator


def _require_running(orchestrator: TrafforaOrchestrator) -> None:
    """Reject step calls that need a live simulation."""
    if not orchestrator.is_running:
        raise SimulationNotRunningError(
            "the simulation is not running; call POST /simulation/start first"
        )


_DEFAULT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


def _cors_origins() -> List[str]:
    """Resolve the allowed browser origins for the dashboard.

    ``TRAFFORA_CORS_ORIGINS`` is a comma-separated list. It defaults to the
    Vite dev server only, so a production deployment must opt in explicitly
    rather than inheriting a wildcard. ``*`` is rejected on purpose: the API
    is unauthenticated, and a wildcard would let any page on the internet
    drive the simulation.
    """
    raw = os.getenv("TRAFFORA_CORS_ORIGINS", "")
    candidates = [item.strip() for item in raw.split(",") if item.strip()]
    origins = candidates or list(_DEFAULT_CORS_ORIGINS)
    return [origin for origin in origins if origin != "*"]


def create_app(
    orchestrator: Optional[TrafforaOrchestrator] = None,
    config: Optional[OrchestratorConfig] = None,
) -> FastAPI:
    """Build the FastAPI application.

    Parameters
    ----------
    orchestrator:
        Pre-built orchestrator, used by the tests. When omitted the app builds
        one lazily from ``config`` on first use.
    config:
        Settings used for that lazy build.
    """
    app = FastAPI(
        title="Traffora-AI Orchestrator",
        version="1.0.0",
        description=(
            "Multi-agent urban mobility management. Emergency preemption is "
            "priority 1, traffic optimisation priority 2, parking advice "
            "priority 3. Every signal action passes an independent safety "
            "validator before it reaches SUMO."
        ),
    )
    app.state.orchestrator = orchestrator
    app.state.config = config

    # Browser access is opt-in per origin. No wildcard, no credentials: the
    # dashboard is a static SPA that talks to this API with plain XHR.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Accept"],
        max_age=600,
    )

    @app.exception_handler(OrchestratorError)
    async def _orchestrator_error_handler(_: Request, exc: OrchestratorError) -> JSONResponse:
        # 409 for "not running", 400 for bad input, 500 otherwise.
        code = 409 if isinstance(exc, SimulationNotRunningError) else 400
        return JSONResponse(status_code=code, content={"error": exc.to_dict()})

    # ------------------------------------------------------------------ #
    # Meta
    # ------------------------------------------------------------------ #
    @app.get("/health", tags=["meta"])
    def health() -> Dict[str, Any]:
        """Liveness probe. Never builds the orchestrator."""
        return {"status": "ok"}

    @app.get("/status", tags=["meta"])
    def get_status(
        orch: TrafforaOrchestrator = Depends(get_orchestrator),
    ) -> Dict[str, Any]:
        """Overall system status: config, agents, queue depth, metrics."""
        return _serialise(orch.get_system_status())

    # ------------------------------------------------------------------ #
    # Simulation control
    # ------------------------------------------------------------------ #
    @app.post("/simulation/start", tags=["simulation"])
    def start_simulation(
        orch: TrafforaOrchestrator = Depends(get_orchestrator),
    ) -> Dict[str, Any]:
        """Initialise SUMO and the agents, then mark the system running."""
        return _serialise(orch.start_simulation())

    @app.post("/simulation/stop", tags=["simulation"])
    def stop_simulation(
        orch: TrafforaOrchestrator = Depends(get_orchestrator),
    ) -> Dict[str, Any]:
        """Close the shared SUMO session and shut the agents down."""
        return _serialise(orch.stop_simulation())

    @app.post("/simulation/step", tags=["simulation"])
    def step_simulation(
        n: int = Query(default=1, ge=1, le=600, description="SUMO steps to advance"),
        orch: TrafforaOrchestrator = Depends(get_orchestrator),
    ) -> Dict[str, Any]:
        """Advance the simulation by ``n`` steps and return the new decisions.

        This is the only endpoint that moves simulated time, so a dashboard
        controls the clock explicitly instead of racing a background loop.
        """
        _require_running(orch)
        decisions = orch.run_step(n)
        return {
            "simulation_time_s": orch.state.simulation_time_s,
            "decisions": _serialise(decisions),
        }

    # ------------------------------------------------------------------ #
    # City state and agents
    # ------------------------------------------------------------------ #
    @app.get("/city-state", tags=["state"])
    def get_city_state(
        orch: TrafforaOrchestrator = Depends(get_orchestrator),
    ) -> Dict[str, Any]:
        """Latest traffic, emergency, parking, agent and decision snapshot."""
        return _serialise(orch.get_city_state())

    @app.get("/agents", tags=["state"])
    def get_agents(
        orch: TrafforaOrchestrator = Depends(get_orchestrator),
    ) -> List[Dict[str, Any]]:
        """One health entry per registered agent."""
        return _serialise(orch.get_agent_statuses())

    # ------------------------------------------------------------------ #
    # Decisions
    # ------------------------------------------------------------------ #
    @app.get("/decisions/latest", tags=["decisions"])
    def get_latest_decision(
        orch: TrafforaOrchestrator = Depends(get_orchestrator),
    ) -> Optional[Dict[str, Any]]:
        """The most recent decision, or ``null`` before the first cycle.

        The return type must be ``Optional``: before the first control cycle
        there is genuinely no decision, and a ``Dict`` annotation would make
        FastAPI reject that ``None`` as a validation error.
        """
        return _serialise(orch.get_latest_decision())

    @app.get("/decisions", tags=["decisions"])
    def get_decisions(
        limit: int = Query(default=50, ge=1, le=500),
        orch: TrafforaOrchestrator = Depends(get_orchestrator),
    ) -> List[Dict[str, Any]]:
        """Decision history, newest first."""
        return _serialise(orch.get_decisions(limit=limit))

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    @app.get("/events", tags=["events"])
    def get_events(
        limit: int = Query(default=100, ge=1, le=1000),
        orch: TrafforaOrchestrator = Depends(get_orchestrator),
    ) -> List[Dict[str, Any]]:
        """Recent events, oldest first."""
        return _serialise(orch.get_events(limit=limit))

    @app.get("/emergency", tags=["events"])
    def get_emergency(
        orch: TrafforaOrchestrator = Depends(get_orchestrator),
    ) -> List[Dict[str, Any]]:
        """Active emergency vehicles plus their emergency events."""
        return _serialise(orch.get_emergency_events())

    # ------------------------------------------------------------------ #
    # Analytics and metrics
    # ------------------------------------------------------------------ #
    @app.get("/analytics/traffic", tags=["analytics"])
    def get_traffic_analytics(
        orch: TrafforaOrchestrator = Depends(get_orchestrator),
    ) -> Dict[str, Any]:
        """Measured traffic figures (waiting time, queue length, travel time)."""
        return _serialise(orch.get_traffic_analytics())

    @app.get("/analytics/parking", tags=["analytics"])
    def get_parking_analytics(
        orch: TrafforaOrchestrator = Depends(get_orchestrator),
    ) -> Dict[str, Any]:
        """Measured parking figures (occupancy, search time)."""
        return _serialise(orch.get_parking_analytics())

    @app.get("/metrics", tags=["analytics"])
    def get_metrics(
        orch: TrafforaOrchestrator = Depends(get_orchestrator),
    ) -> Dict[str, Any]:
        """Every measured metric, nothing estimated."""
        return _serialise(orch.get_simulation_metrics())

    return app


app = create_app()


def main(argv: Optional[List[str]] = None) -> int:
    """``python -m backend.orchestrator.api`` entry point.

    Cloud hosts (Render, Railway, Fly, Heroku) inject the port through the
    ``PORT`` environment variable and require binding ``0.0.0.0`` rather than
    loopback. Both are honoured here so the same entry point works locally and
    in a container without a second command line.
    """
    env_port = os.environ.get("PORT")
    parser = argparse.ArgumentParser(description="Serve the Traffora-AI orchestrator API.")
    # Bind all interfaces when PORT is injected (i.e. we are in a container);
    # otherwise stay on loopback so a local run is not exposed by accident.
    parser.add_argument("--host", default="0.0.0.0" if env_port else "127.0.0.1")
    parser.add_argument("--port", type=int, default=int(env_port) if env_port else 8000)
    parser.add_argument("--sumocfg", default=None, help="Override the SUMO config path.")
    parser.add_argument("--end", type=int, default=None, help="Override the simulation end time.")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--gui", action="store_true", help="Run SUMO with the GUI.")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    configure_logging("WARNING" if args.quiet else "INFO")

    overrides: Dict[str, Any] = {}
    if args.sumocfg:
        overrides["sumocfg"] = args.sumocfg
    if args.end is not None:
        overrides["end_time"] = args.end
    if args.seed is not None:
        overrides["seed"] = args.seed
    if args.gui:
        overrides["gui"] = True

    config = dataclasses.replace(OrchestratorConfig(), **overrides)

    import uvicorn

    uvicorn.run(create_app(config=config), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
