"""End-to-end test of the HTTP API against the *real* SUMO scenario.

``test_api.py`` proves the transport layer with fakes. This module proves the
same endpoints work when the orchestrator is driving live TraCI and the real
agents, which is the only way to know the JSON actually serialises the shapes
the agents produce.

Skipped (not failed) when SUMO or TraCI is unavailable.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import shutil
from pathlib import Path

import pytest

from backend.orchestrator.config import OrchestratorConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
SUMOCFG = REPO_ROOT / "simulation" / "orchestrator" / "unified.sumocfg"

#: Enough simulated time for vehicles to depart and be measured, while keeping
#: the suite fast. The full demonstration uses ``--end 900``.
API_E2E_END_S = 120


def _sumo_available() -> bool:
    if shutil.which("sumo") is None:
        return False
    return importlib.util.find_spec("traci") is not None


pytestmark = pytest.mark.skipif(
    not _sumo_available() or not SUMOCFG.is_file(),
    reason="SUMO / TraCI or the unified scenario is not available",
)

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from backend.orchestrator.api import create_app  # noqa: E402


@pytest.fixture(scope="module")
def client() -> TestClient:
    """App wired to a real orchestrator on the unified scenario."""
    base = OrchestratorConfig()
    config = dataclasses.replace(
        base,
        sumocfg=SUMOCFG,
        end_time=API_E2E_END_S,
        label="orchestrator-api-e2e",
        seed=42,
        decision_interval_s=5.0,
        parking_interval_s=10.0,
    )
    app = create_app(config=config)
    with TestClient(app) as test_client:
        yield test_client
        # Tear the SUMO session down even if an assertion failed.
        orchestrator = app.state.orchestrator
        if orchestrator is not None and orchestrator.is_running:
            orchestrator.shutdown()


def test_api_starts_a_real_simulation(client: TestClient):
    response = client.post("/simulation/start")
    assert response.status_code == 200
    body = response.json()
    assert body["started"] is True
    # Measured from the running network, not a constant.
    assert body["traffic_lights"] == 21
    assert body["parking_areas"] == 5


def test_stepping_drives_the_real_simulation(client: TestClient):
    body = client.post("/simulation/step", params={"n": 40}).json()
    assert body["simulation_time_s"] == 40.0
    assert isinstance(body["decisions"], list)


def test_city_state_is_dashboard_ready_from_real_data(client: TestClient):
    client.post("/simulation/step", params={"n": 20})
    state = client.get("/city-state").json()

    assert state["timestamp_s"] == 60.0
    # The agents reported real observations.
    assert state["traffic"] is not None
    assert state["parking"] is not None and state["parking"]["zones"]
    assert state["agents"], "the registry should report the agents"

    # Every agent that is registered should have reached a usable state.
    healths = {entry["agent"]: entry["health"] for entry in state["agents"]}
    assert healths, healths
    assert all(health in {"READY", "DEGRADED"} for health in healths.values()), healths


def test_metrics_are_measured_not_invented(client: TestClient):
    metrics = client.get("/metrics").json()
    assert metrics["simulation_time_s"] == 60.0
    # Vehicles must actually have been simulated by this point.
    assert metrics["vehicles_departed"] > 0, metrics
    assert metrics["average_waiting_time_s"] is not None
    assert metrics["orchestrator_processing_latency_ms"] is not None


def test_analytics_endpoints_return_real_figures(client: TestClient):
    traffic = client.get("/analytics/traffic").json()
    assert traffic["vehicles_departed"] > 0
    assert traffic["average_queue_length"] is not None

    parking = client.get("/analytics/parking").json()
    assert parking["parking"]["zones"], parking


def test_decision_history_is_serialisable(client: TestClient):
    decisions = client.get("/decisions", params={"limit": 20}).json()
    assert isinstance(decisions, list)
    for decision in decisions:
        # These keys must survive serialisation for a dashboard to use them.
        assert "decision_type" in decision
        assert "priority" in decision
        assert "validation" in decision
        assert "explanation" in decision


def test_no_unvalidated_action_reached_sumo_via_the_api(client: TestClient):
    """The API must not become a way to bypass the safety validator."""
    app = client.app
    orchestrator = app.state.orchestrator
    applied = [
        decision
        for decision in orchestrator.state.get_decisions()
        if decision.applied and decision.signal_actions
    ]
    for decision in applied:
        assert decision.validation is not None
        assert decision.validation.valid, decision.explanation

    # And the coordinator's own write count agrees with the accepted decisions.
    assert orchestrator.coordinator.applied_action_count == sum(
        len(decision.signal_actions) for decision in applied
    )


def test_api_stops_the_real_simulation(client: TestClient):
    response = client.post("/simulation/stop")
    assert response.status_code == 200
    assert response.json() == {"started": False, "running": False}
    assert client.get("/status").json()["running"] is False
