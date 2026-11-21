"""Unit tests for the agent registry and its failure isolation.

The registry is the only place the orchestrator touches the three agents, so
these tests pin the contract: registration, lookup, initialisation failures
that must not cascade, and collection that degrades gracefully.
"""

from __future__ import annotations

import pytest

from backend.orchestrator.agent_registry import (
    AgentAdapter,
    AgentRegistry,
    AgentSnapshot,
    _as_dict,
)
from backend.orchestrator.exceptions import AgentInitializationError, AgentNotFoundError
from backend.orchestrator.schemas import AgentHealth, AgentName, AgentStatus

from .conftest import FakeCoordinator


class StubAdapter(AgentAdapter):
    """A real ``AgentAdapter`` subclass with scripted behaviour."""

    def __init__(self, name: AgentName, *, fail_init: bool = False,
                 fail_collect: bool = False) -> None:
        super().__init__()
        self.name = name
        self._fail_init = fail_init
        self._fail_collect = fail_collect
        self.initialised = False
        self.shutdown_called = False

    def initialize(self, coordinator) -> None:
        if self._fail_init:
            self._mark_failed("scripted init failure")
            raise AgentInitializationError("scripted init failure")
        self.initialised = True
        self._status = AgentStatus(agent=self.name, health=AgentHealth.READY,
                                   available=True, message="ready")

    def collect(self, coordinator, timestamp_s: float) -> AgentSnapshot:
        if self._fail_collect:
            self._mark_degraded("scripted collect failure")
            return AgentSnapshot(agent=self.name, notes=["collect failed"])
        return AgentSnapshot(agent=self.name, notes=[f"t={timestamp_s}"])

    def shutdown(self) -> None:
        self.shutdown_called = True
        super().shutdown()


@pytest.fixture
def registry() -> AgentRegistry:
    reg = AgentRegistry()
    reg.register(StubAdapter(AgentName.TRAFFIC_MIND))
    reg.register(StubAdapter(AgentName.GREEN_CORRIDOR))
    reg.register(StubAdapter(AgentName.PARK_SENSE))
    return reg


def test_registry_holds_all_three_agents(registry):
    assert registry.names() == [AgentName.GREEN_CORRIDOR, AgentName.PARK_SENSE,
                                AgentName.TRAFFIC_MIND]


def test_get_by_enum_and_by_string(registry):
    assert registry.get(AgentName.TRAFFIC_MIND).name is AgentName.TRAFFIC_MIND
    assert registry.get("park_sense").name is AgentName.PARK_SENSE


def test_unknown_agent_raises(registry):
    with pytest.raises(AgentNotFoundError):
        registry.get("does_not_exist")


def test_register_rejects_non_adapter():
    with pytest.raises(AgentInitializationError):
        AgentRegistry().register(object())  # type: ignore[arg-type]


def test_initialize_all_returns_status_per_agent(registry):
    statuses = registry.initialize_all(FakeCoordinator())
    assert set(statuses) == {"traffic_mind", "green_corridor", "park_sense"}
    assert all(s.health is AgentHealth.READY for s in statuses.values())


def test_one_failed_initialisation_does_not_block_the_others():
    reg = AgentRegistry()
    reg.register(StubAdapter(AgentName.TRAFFIC_MIND, fail_init=True))
    reg.register(StubAdapter(AgentName.GREEN_CORRIDOR))
    reg.register(StubAdapter(AgentName.PARK_SENSE))

    statuses = reg.initialize_all(FakeCoordinator())

    assert statuses["traffic_mind"].health is AgentHealth.FAILED
    assert statuses["traffic_mind"].available is False
    assert statuses["green_corridor"].health is AgentHealth.READY
    assert statuses["park_sense"].health is AgentHealth.READY
    assert reg.available() == [AgentName.GREEN_CORRIDOR, AgentName.PARK_SENSE]


def test_failed_agent_is_skipped_when_collecting():
    reg = AgentRegistry()
    reg.register(StubAdapter(AgentName.TRAFFIC_MIND, fail_init=True))
    reg.register(StubAdapter(AgentName.GREEN_CORRIDOR))
    reg.initialize_all(FakeCoordinator())

    snapshots = reg.collect_all(FakeCoordinator(), timestamp_s=12.0)

    assert [s.agent for s in snapshots] == [AgentName.GREEN_CORRIDOR]


def test_collect_failure_degrades_rather_than_raises(registry):
    registry.initialize_all(FakeCoordinator())
    failing = registry.get(AgentName.PARK_SENSE)
    failing._fail_collect = True  # type: ignore[attr-defined]

    snapshots = registry.collect_all(FakeCoordinator(), timestamp_s=3.0)

    assert len(snapshots) == 3
    park = next(s for s in snapshots if s.agent is AgentName.PARK_SENSE)
    assert park.notes == ["collect failed"]
    assert registry.get(AgentName.PARK_SENSE).status().health is AgentHealth.DEGRADED


def test_shutdown_all_marks_every_agent_stopped(registry):
    registry.initialize_all(FakeCoordinator())
    registry.shutdown_all()
    for name in registry.names():
        assert registry.get(name).status().health is AgentHealth.STOPPED
        assert registry.get(name).shutdown_called is True


def test_as_dict_normalises_all_agent_shapes():
    class WithModelDump:
        def model_dump(self, mode=None):
            return {"shape": "pydantic"}

    class WithToDict:
        def to_dict(self):
            return {"shape": "dataclass"}

    assert _as_dict({"shape": "dict"}) == {"shape": "dict"}
    assert _as_dict(WithModelDump()) == {"shape": "pydantic"}
    assert _as_dict(WithToDict()) == {"shape": "dataclass"}
    assert _as_dict(None) == {}
    assert _as_dict(object()) == {}
