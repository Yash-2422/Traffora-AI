"""Tests for the HTTP API layer.

The API is a transport layer only, so these tests assert two things:

1. every endpoint delegates to the orchestrator service method and returns
   JSON-serialisable data (no Pydantic objects leaking into the response),
2. the error paths behave: stepping a stopped simulation returns 409, and
   importing the app does not start SUMO.

No SUMO binary is required: the app is built around ``FakeCoordinator``.
"""

from __future__ import annotations

import dataclasses

import pytest

from backend.orchestrator.agent_registry import AgentRegistry
from backend.orchestrator.api import _serialise, create_app
from backend.orchestrator.config import OrchestratorConfig
from backend.orchestrator.decision_engine import DecisionEngine
from backend.orchestrator.event_manager import EventManager
from backend.orchestrator.orchestrator import TrafforaOrchestrator
from backend.orchestrator.safety_validator import SafetyValidator
from backend.orchestrator.schemas import (
    AgentName,
    DecisionType,
    EventPriority,
    ParkingRecommendation,
)
from backend.orchestrator.shared_state import SharedState

from .conftest import (
    FakeAdapter,
    FakeCoordinator,
    make_emergency,
    make_parking,
    make_recommendation,
    make_signal_action,
    make_traffic,
)

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def _config() -> OrchestratorConfig:
    base = OrchestratorConfig()
    return dataclasses.replace(
        base,
        decision_interval_s=1.0,
        parking_interval_s=1.0,
        decisions=dataclasses.replace(
            base.decisions,
            signal_action_cooldown_s=0.0,
            traffic_cooldown_s=0.0,
        ),
    )


def _build() -> TrafforaOrchestrator:
    """Orchestrator backed by fakes, with one scripted recommendation per agent."""
    config = _config()

    def traffic_factory(timestamp_s: float):
        from backend.orchestrator.agent_registry import AgentSnapshot

        return AgentSnapshot(
            agent=AgentName.TRAFFIC_MIND,
            traffic=make_traffic({"A1": "HIGH"}),
            recommendations=[
                make_recommendation(
                    AgentName.TRAFFIC_MIND,
                    DecisionType.TRAFFIC_OPTIMIZATION,
                    EventPriority.TRAFFIC,
                    signal_actions=[make_signal_action("A1", 25.0)],
                    explanation="queue at A1 is high",
                )
            ],
        )

    def parking_factory(timestamp_s: float):
        from backend.orchestrator.agent_registry import AgentSnapshot

        return AgentSnapshot(
            agent=AgentName.PARK_SENSE,
            parking=make_parking(85.0),
            recommendations=[
                make_recommendation(
                    AgentName.PARK_SENSE,
                    DecisionType.PARKING_RECOMMENDATION,
                    EventPriority.PARKING,
                    parking_recommendations=[
                        ParkingRecommendation(
                            zone_id="PA_A",
                            name="Zone A",
                            score=0.9,
                            available=2,
                            occupancy_pct=85.0,
                            reason="closest to destination",
                        )
                    ],
                    explanation="occupancy above threshold",
                )
            ],
        )

    registry = AgentRegistry()
    registry.register(FakeAdapter(AgentName.TRAFFIC_MIND, snapshot_factory=traffic_factory))
    registry.register(FakeAdapter(AgentName.PARK_SENSE, snapshot_factory=parking_factory))

    coordinator = FakeCoordinator()
    return TrafforaOrchestrator(
        config,
        coordinator=coordinator,
        registry=registry,
        event_manager=EventManager(dedupe_window_s=0.0, max_pending=100),
        decision_engine=DecisionEngine(config.decisions),
        safety_validator=SafetyValidator(config.safety, known_tls=coordinator.known_tls_ids),
        state=SharedState(),
    )


@pytest.fixture()
def orchestrator() -> TrafforaOrchestrator:
    return _build()


@pytest.fixture()
def client(orchestrator: TrafforaOrchestrator) -> TestClient:
    return TestClient(create_app(orchestrator=orchestrator))


def test_importing_the_app_does_not_build_an_orchestrator():
    app = create_app()
    assert app.state.orchestrator is None


def test_health_never_builds_the_orchestrator():
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
    assert app.state.orchestrator is None


def test_serialise_handles_models_lists_and_dicts():
    assert _serialise(None) is None
    assert _serialise([make_traffic()])[0]["vehicle_count"] == 100
    assert _serialise({"a": make_parking(50.0)})["a"]["timestamp_s"] == 0.0


def test_start_and_stop_simulation(client: TestClient):
    started = client.post("/simulation/start")
    assert started.status_code == 200
    assert started.json()["started"] is True

    stopped = client.post("/simulation/stop")
    assert stopped.status_code == 200
    assert stopped.json() == {"started": False, "running": False}


def test_stepping_a_stopped_simulation_returns_409(client: TestClient):
    response = client.post("/simulation/step")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SIMULATION_NOT_RUNNING"


def test_step_advances_time_and_returns_decisions(client: TestClient):
    client.post("/simulation/start")
    body = client.post("/simulation/step", params={"n": 3}).json()
    assert body["simulation_time_s"] == 3.0
    assert isinstance(body["decisions"], list)
    assert body["decisions"], "the scripted agents should produce decisions"


def test_step_rejects_out_of_range_n(client: TestClient):
    client.post("/simulation/start")
    assert client.post("/simulation/step", params={"n": 0}).status_code == 422
    assert client.post("/simulation/step", params={"n": 10_000}).status_code == 422


def test_city_state_is_json_and_carries_agents(client: TestClient):
    client.post("/simulation/start")
    client.post("/simulation/step", params={"n": 2})
    body = client.get("/city-state").json()
    assert body["timestamp_s"] == 2.0
    assert body["traffic"]["vehicle_count"] == 100
    assert body["parking"]["zones"][0]["zone_id"] == "PA_A"
    assert {entry["agent"] for entry in body["agents"]} == {"traffic_mind", "park_sense"}


def test_agents_endpoint_reports_health(client: TestClient):
    client.post("/simulation/start")
    agents = client.get("/agents").json()
    assert len(agents) == 2
    assert all(entry["health"] == "READY" for entry in agents)
    assert all(entry["available"] for entry in agents)


def test_decisions_endpoints(client: TestClient):
    client.post("/simulation/start")
    client.post("/simulation/step", params={"n": 1})

    latest = client.get("/decisions/latest").json()
    assert latest["decision_type"] in {"TRAFFIC_OPTIMIZATION", "PARKING_RECOMMENDATION"}

    history = client.get("/decisions", params={"limit": 10}).json()
    assert isinstance(history, list) and history

    assert client.get("/decisions", params={"limit": 0}).status_code == 422


def test_latest_decision_is_null_before_any_cycle(client: TestClient):
    assert client.get("/decisions/latest").json() is None


def test_events_and_emergency_endpoints(client: TestClient):
    client.post("/simulation/start")
    client.post("/simulation/step", params={"n": 2})
    assert isinstance(client.get("/events").json(), list)
    assert isinstance(client.get("/emergency").json(), list)


def test_emergency_endpoint_surfaces_active_vehicle(client: TestClient, orchestrator):
    orchestrator.initialize()
    orchestrator.start()
    orchestrator.state.upsert_emergency(make_emergency("amb_1", "A1", 5.0))
    body = client.get("/emergency").json()
    assert any(item.get("vehicle_id") == "amb_1" for item in body)


def test_analytics_and_metrics_endpoints(client: TestClient):
    client.post("/simulation/start")
    client.post("/simulation/step", params={"n": 4})

    traffic = client.get("/analytics/traffic").json()
    assert "average_waiting_time_s" in traffic
    assert traffic["vehicles_departed"] >= 0

    parking = client.get("/analytics/parking").json()
    assert "occupancy_pct" in parking

    metrics = client.get("/metrics").json()
    assert metrics["simulation_time_s"] == 4.0
    assert "orchestrator_processing_latency_ms" in metrics


def test_status_endpoint_reports_config_and_agents(client: TestClient):
    client.post("/simulation/start")
    body = client.get("/status").json()
    assert body["running"] is True
    assert body["config"]["seed"] == 42
    assert len(body["agents"]) == 2
    assert "metrics" in body


def test_orchestrator_errors_are_mapped_to_json():
    """A domain error surfaces as ``{"error": {...}}`` with a stable code."""
    from backend.orchestrator.exceptions import AgentNotFoundError

    app = create_app(orchestrator=_build())
    orch = app.state.orchestrator

    def boom(*_args, **_kwargs):
        raise AgentNotFoundError("no such agent")

    orch.get_agent_statuses = boom  # type: ignore[assignment]

    with TestClient(app) as local_client:
        response = local_client.get("/agents")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "AGENT_NOT_FOUND"


def test_openapi_schema_is_generated(client: TestClient):
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "Traffora-AI Orchestrator"
    assert "/city-state" in schema["paths"]
    assert "/simulation/step" in schema["paths"]
