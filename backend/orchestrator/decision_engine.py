"""Deterministic, explainable decision engine.

Priority order is fixed and non-negotiable:

1. **Emergency safety** — a GreenCorridor priority request always wins. Any
   TrafficMind action for the same traffic light is dropped for that step.
2. **Traffic congestion** — applied only where no emergency owns the light, and
   only when it changes something (no unnecessary switching).
3. **Parking optimisation** — advisory only. It can never produce a signal
   action, so it cannot interfere with 1 or 2.

There is no LLM in this path and no randomness: given the same inputs the
engine returns the same decisions in the same order.
"""

from __future__ import annotations

import logging
import uuid
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .config import DecisionSettings
from .schemas import (
    AgentName,
    AgentRecommendation,
    CityState,
    DecisionType,
    EventPriority,
    OrchestratorDecision,
    SignalAction,
)

logger = logging.getLogger(__name__)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class DecisionEngine:
    """Turns recommendations + city state into ordered decisions."""

    def __init__(self, settings: Optional[DecisionSettings] = None) -> None:
        self.settings = settings or DecisionSettings()
        self._last_action_s: Dict[str, float] = {}
        self._last_emergency_s: Dict[str, float] = {}

    def reset(self) -> None:
        self._last_action_s.clear()
        self._last_emergency_s.clear()

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    def decide(self, city_state: CityState,
               recommendations: Sequence[AgentRecommendation],
               timestamp_s: Optional[float] = None) -> List[OrchestratorDecision]:
        now = float(timestamp_s if timestamp_s is not None else city_state.timestamp_s)
        ordered = sorted(recommendations,
                         key=lambda r: (int(r.priority), r.agent.value, r.recommendation_id))

        decisions: List[OrchestratorDecision] = []

        emergency = [r for r in ordered if r.decision_type is DecisionType.EMERGENCY_PRIORITY]
        traffic = [r for r in ordered if r.decision_type is DecisionType.TRAFFIC_OPTIMIZATION]
        parking = [r for r in ordered if r.decision_type is DecisionType.PARKING_RECOMMENDATION]

        emergency_decisions, emergency_tls = self._emergency_decisions(
            emergency, city_state, now)
        decisions.extend(emergency_decisions)

        decisions.extend(self._traffic_decisions(traffic, city_state, emergency_tls, now))
        decisions.extend(self._parking_decisions(parking, city_state, now))

        if not decisions:
            decisions.append(OrchestratorDecision(
                decision_id=_new_id("dec"),
                timestamp_s=now,
                decision_type=DecisionType.NO_ACTION,
                priority=EventPriority.PARKING,
                source_agent=None,
                explanation="No agent requested an action at this step.",
                applied=True,
                metadata={"reason": "no_recommendations"},
            ))
        return decisions

    # ------------------------------------------------------------------ #
    # Priority 1: emergency
    # ------------------------------------------------------------------ #
    def _emergency_decisions(
        self,
        recommendations: Sequence[AgentRecommendation],
        city_state: CityState,
        now: float,
    ) -> Tuple[List[OrchestratorDecision], Set[str]]:
        actions: List[SignalAction] = []
        vehicles: Set[str] = set()
        for rec in recommendations:
            for action in rec.signal_actions:
                actions.append(action)
                if action.emergency_vehicle_id:
                    vehicles.add(action.emergency_vehicle_id)

        emergency_tls = {a.tls_id for a in actions}
        if not actions:
            return [], emergency_tls

        # One action per traffic light per cycle: two emergency vehicles heading
        # for the same junction must not produce two competing writes. The longer
        # hold wins because it satisfies both ETAs; the tie-break is deterministic.
        by_tls: Dict[str, SignalAction] = {}
        for action in actions:
            current = by_tls.get(action.tls_id)
            if current is None or (action.duration_s, action.action_id) > (current.duration_s, current.action_id):
                by_tls[action.tls_id] = action
        actions = sorted(by_tls.values(), key=lambda a: (a.tls_id, a.action_id))

        # A preemption is a one-off adjustment of the running phase, not a
        # permanent hold. Re-issuing it every step would pin the light green
        # indefinitely (and starve the cross traffic), so each light is written
        # at most once per cooldown window. The lights stay reserved for the
        # emergency vehicle either way: ``emergency_tls`` still suppresses any
        # competing traffic-optimisation action.
        cooldown = self.settings.signal_action_cooldown_s
        issued: List[SignalAction] = []
        for action in actions:
            last = self._last_emergency_s.get(action.tls_id)
            if last is not None and now - last < cooldown:
                continue
            issued.append(action)
        for action in issued:
            self._last_emergency_s[action.tls_id] = now
        actions = issued
        if not actions:
            return [], emergency_tls

        detail = []
        for event in city_state.emergency_vehicles:
            if event.tls_id:
                detail.append(f"{event.vehicle_id} -> {event.tls_id} "
                              f"(ETA {event.eta_s:.1f}s)" if event.eta_s is not None
                              else f"{event.vehicle_id} -> {event.tls_id}")
        explanation = (
            "Emergency priority (P1): GreenCorridor requested preemption at "
            + ", ".join(sorted(emergency_tls))
            + (". Active vehicles: " + "; ".join(detail) if detail else ".")
            + " TrafficMind actions for these lights are suppressed this step."
        )
        decision = OrchestratorDecision(
            decision_id=_new_id("dec"),
            timestamp_s=now,
            decision_type=DecisionType.EMERGENCY_PRIORITY,
            priority=EventPriority.EMERGENCY,
            source_agent=AgentName.GREEN_CORRIDOR,
            signal_actions=actions,
            explanation=explanation,
            metadata={
                "emergency_vehicles": sorted(vehicles),
                "suppressed_tls": sorted(emergency_tls),
                "preempts_traffic": True,
            },
        )
        return [decision], emergency_tls

    # ------------------------------------------------------------------ #
    # Priority 2: traffic
    # ------------------------------------------------------------------ #
    def _traffic_decisions(
        self,
        recommendations: Sequence[AgentRecommendation],
        city_state: CityState,
        emergency_tls: Set[str],
        now: float,
    ) -> List[OrchestratorDecision]:
        decisions: List[OrchestratorDecision] = []
        suppressed: List[str] = []
        kept: List[SignalAction] = []

        for rec in recommendations:
            for action in rec.signal_actions:
                if action.tls_id in emergency_tls:
                    suppressed.append(action.tls_id)
                    continue
                level = (city_state.traffic.junction_levels.get(action.tls_id, "")
                         if city_state.traffic else "")
                if level and level.upper() not in {l.upper() for l in self.settings.traffic_action_levels}:
                    # Low/medium congestion does not justify touching the signal.
                    continue
                last = self._last_action_s.get(action.tls_id)
                if last is not None and now - last < self.settings.traffic_cooldown_s:
                    continue
                kept.append(action)

        if not kept:
            return decisions

        kept.sort(key=lambda a: (a.tls_id, a.action_id))
        for action in kept:
            self._last_action_s[action.tls_id] = now
        explanation = (
            f"Traffic optimisation (P2): {len(kept)} green extension(s) at "
            + ", ".join(sorted({a.tls_id for a in kept}))
            + ". Only lights with HIGH congestion and no active emergency request "
              "were adjusted."
        )
        if suppressed:
            explanation += " Suppressed by emergency priority: " + ", ".join(sorted(set(suppressed))) + "."
        decisions.append(OrchestratorDecision(
            decision_id=_new_id("dec"),
            timestamp_s=now,
            decision_type=DecisionType.TRAFFIC_OPTIMIZATION,
            priority=EventPriority.TRAFFIC,
            source_agent=AgentName.TRAFFIC_MIND,
            signal_actions=kept,
            explanation=explanation,
            metadata={"suppressed_tls": sorted(set(suppressed))},
        ))
        return decisions

    # ------------------------------------------------------------------ #
    # Priority 3: parking (advisory)
    # ------------------------------------------------------------------ #
    def _parking_decisions(
        self,
        recommendations: Sequence[AgentRecommendation],
        city_state: CityState,
        now: float,
    ) -> List[OrchestratorDecision]:
        decisions: List[OrchestratorDecision] = []
        for rec in recommendations:
            if not rec.parking_recommendations:
                continue
            ranked = sorted(rec.parking_recommendations, key=lambda p: (-p.score, p.zone_id))
            best = ranked[0]
            decisions.append(OrchestratorDecision(
                decision_id=_new_id("dec"),
                timestamp_s=now,
                decision_type=DecisionType.PARKING_RECOMMENDATION,
                priority=EventPriority.PARKING,
                source_agent=AgentName.PARK_SENSE,
                signal_actions=[],  # advisory only: never a signal write
                parking_recommendations=ranked,
                explanation=(
                    f"Parking optimisation (P3, advisory): recommend {best.zone_id} "
                    f"({best.available} free spaces, {best.occupancy_pct:.1f}% occupied). "
                    "This decision cannot change a traffic signal."
                ),
                applied=True,
                metadata={"advisory": True,
                          "overall_occupancy_pct": (city_state.parking.overall_occupancy_pct
                                                    if city_state.parking else None)},
            ))
        return decisions

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    def last_action_times(self) -> Dict[str, float]:
        return dict(self._last_action_s)
