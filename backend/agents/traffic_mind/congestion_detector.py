"""Congestion detection: classifies roads and junctions as LOW / MEDIUM / HIGH.

Pure Python (no SUMO import). It consumes the ``TrafficSnapshot`` produced by
``TrafficMonitor`` - or an equivalent dict - and returns a ``CongestionReport``.

How a level is decided
----------------------
1. **Smoothing**: the four measured signals of each road - occupancy, queue
   length relative to road length, mean waiting time and speed drop - are
   averaged over ``smoothing_window_s`` (one signal cycle), because queues
   build during red and clear during green.
2. **Score per road** (0..1): the weighted sum of the smoothed signals. See
   ``CongestionConfig`` for the exact scaling and reference values.
3. **Level per road**: score >= ``medium_threshold`` -> MEDIUM,
   >= ``high_threshold`` -> HIGH, otherwise LOW.
4. **Junction**: takes the score of its worst approach road.
5. **Persistence**: a level only changes after the new condition has held for
   ``escalate_after_s`` / ``clear_after_s`` seconds. ``raw_level`` is the level
   of the current score, ``level`` is the stable one to act on (it can lag
   behind the score by up to those delays).

The detector keeps a little state (per-entity history), so use one instance per
simulation and call ``update()`` once per snapshot, in time order.

Typical use::

    monitor = TrafficMonitor.from_source(sim)
    detector = CongestionDetector()
    report = detector.update(monitor.sample(sim))
    print(report.to_dict())
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple, Union

from pydantic import ValidationError

from .config import CongestionConfig
from .exceptions import InvalidTrafficDataError
from .schemas import (
    AttentionJunction,
    CongestionAlert,
    CongestionFactors,
    CongestionLevel,
    CongestionReport,
    CongestionSummary,
    EdgeCongestion,
    EdgeMetrics,
    JunctionCongestion,
    JunctionMetrics,
    TrafficSnapshot,
)

logger = logging.getLogger(__name__)

SnapshotInput = Union[TrafficSnapshot, Dict[str, Any]]

_FACTOR_LABELS = {
    "occupancy": "road occupancy",
    "queue": "queue length",
    "waiting": "waiting time",
    "speed_drop": "speed drop",
}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass
class _EntityState:
    """Persistence state for one road or junction."""

    level: CongestionLevel
    since_s: float
    history: Deque[Tuple[float, CongestionLevel]] = field(default_factory=deque)


class CongestionDetector:
    """Turns traffic snapshots into congestion levels, alerts and an attention list."""

    def __init__(self, config: Optional[CongestionConfig] = None) -> None:
        self.config = config or CongestionConfig()
        self._states: Dict[Tuple[str, str], _EntityState] = {}
        self._factor_history: Dict[str, Deque[Tuple[float, CongestionFactors]]] = {}
        self._last_time: Optional[float] = None
        self._last_report: Optional[CongestionReport] = None

    # ------------------------------------------------------------------ #
    # Access
    # ------------------------------------------------------------------ #
    @property
    def latest(self) -> Optional[CongestionReport]:
        return self._last_report

    def reset(self) -> None:
        """Forget all state (e.g. after a simulation restart)."""
        self._states.clear()
        self._factor_history.clear()
        self._last_time = None
        self._last_report = None

    # ------------------------------------------------------------------ #
    # Scoring (stateless, usable on its own)
    # ------------------------------------------------------------------ #
    def classify(self, score: float) -> CongestionLevel:
        """Map a 0..1 score to a level using the configured thresholds."""
        if score >= self.config.high_threshold:
            return CongestionLevel.HIGH
        if score >= self.config.medium_threshold:
            return CongestionLevel.MEDIUM
        return CongestionLevel.LOW

    def edge_factors(self, edge: EdgeMetrics) -> CongestionFactors:
        """The four 0..1 signals for one road."""
        cfg = self.config
        # Roads with few vehicles cannot be congested: damp the two signals that a
        # single stopped car (e.g. at a red light) would otherwise push to the maximum.
        load = min(1.0, edge.vehicle_count / cfg.min_vehicles_full_weight)
        queue_fraction = edge.queue_length_m / edge.length_m if edge.length_m > 0 else 0.0
        return CongestionFactors(
            occupancy=_clamp01(edge.occupancy_pct / cfg.occupancy_ref_pct),
            queue=_clamp01(queue_fraction / cfg.queue_ref_fraction),
            waiting=_clamp01(edge.mean_waiting_time_s / cfg.waiting_ref_s) * load,
            speed_drop=_clamp01(1.0 - edge.speed_ratio) * load,
        )

    def score(self, factors: CongestionFactors) -> float:
        """Weighted sum of the four factors (0..1)."""
        cfg = self.config
        return _clamp01(
            cfg.weight_occupancy * factors.occupancy
            + cfg.weight_queue * factors.queue
            + cfg.weight_waiting * factors.waiting
            + cfg.weight_speed_drop * factors.speed_drop
        )

    def dominant_factor(self, factors: CongestionFactors) -> str:
        """Name of the factor that contributes most to the score."""
        cfg = self.config
        contributions = {
            "occupancy": cfg.weight_occupancy * factors.occupancy,
            "queue": cfg.weight_queue * factors.queue,
            "waiting": cfg.weight_waiting * factors.waiting,
            "speed_drop": cfg.weight_speed_drop * factors.speed_drop,
        }
        return max(contributions, key=contributions.get)

    # ------------------------------------------------------------------ #
    # Processing
    # ------------------------------------------------------------------ #
    def update(self, snapshot: SnapshotInput) -> CongestionReport:
        """Evaluate one snapshot.

        Raises:
            InvalidTrafficDataError: the snapshot is malformed.
        """
        snap = self._validate(snapshot)
        now = snap.timestamp_s

        if self._last_time is not None and now < self._last_time:
            logger.warning(
                "Snapshot time went backwards (%.1f -> %.1f); resetting detector state.",
                self._last_time, now,
            )
            self.reset()
        self._last_time = now

        edges = self._evaluate_edges(snap, now)
        edge_by_id = {e.edge_id: e for e in edges}
        junctions = [self._evaluate_junction(j, edge_by_id, now) for j in snap.junctions]

        report = CongestionReport(
            timestamp_s=now,
            summary=self._summary(junctions, edges),
            junctions=junctions,
            edges=edges,
            alerts=self._alerts(junctions, edges, now),
            attention_junctions=self._attention(junctions),
        )
        self._last_report = report
        return report

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate(snapshot: SnapshotInput) -> TrafficSnapshot:
        try:
            return (
                snapshot if isinstance(snapshot, TrafficSnapshot)
                else TrafficSnapshot.model_validate(snapshot)
            )
        except ValidationError as exc:
            raise InvalidTrafficDataError(f"Invalid traffic snapshot: {exc}") from exc

    def _stabilise(
        self, key: Tuple[str, str], raw: CongestionLevel, now: float
    ) -> Tuple[CongestionLevel, float]:
        """Apply the persistence rule; returns (stable level, time it started)."""
        cfg = self.config
        state = self._states.get(key)
        if state is None:
            state = self._states[key] = _EntityState(level=raw, since_s=now)
            state.history.append((now, raw))
            return state.level, state.since_s

        state.history.append((now, raw))
        # keep only what the longest window needs (plus one older entry so that the
        # window is known to be fully covered)
        longest = max(cfg.escalate_after_s, cfg.clear_after_s)
        while len(state.history) > 1 and state.history[1][0] <= now - longest:
            state.history.popleft()

        def window(span: float) -> Optional[List[CongestionLevel]]:
            if state.history[0][0] > now - span:
                return None  # not enough history yet to judge a change
            return [lvl for ts, lvl in state.history if ts >= now - span]

        worse = window(cfg.escalate_after_s)
        if worse is not None and all(lvl.rank > state.level.rank for lvl in worse):
            state.level = min(worse, key=lambda lvl: lvl.rank)
            state.since_s = now
        else:
            better = window(cfg.clear_after_s)
            if better is not None and all(lvl.rank < state.level.rank for lvl in better):
                state.level = max(better, key=lambda lvl: lvl.rank)
                state.since_s = now
        return state.level, state.since_s

    def _smoothed(self, edge_id: str, factors: CongestionFactors, now: float) -> CongestionFactors:
        """Average a road's four signals over the last ``smoothing_window_s`` seconds.

        Uses the samples received in the window (a plain mean, so it assumes regular
        sampling, which is how the monitor is run).
        """
        window = self.config.smoothing_window_s
        if window <= 0:
            return factors
        history = self._factor_history.setdefault(edge_id, deque())
        history.append((now, factors))
        while history and history[0][0] <= now - window:
            history.popleft()
        n = len(history)
        return CongestionFactors(
            occupancy=sum(f.occupancy for _, f in history) / n,
            queue=sum(f.queue for _, f in history) / n,
            waiting=sum(f.waiting for _, f in history) / n,
            speed_drop=sum(f.speed_drop for _, f in history) / n,
        )

    def _evaluate_edges(self, snap: TrafficSnapshot, now: float) -> List[EdgeCongestion]:
        results = []
        for edge in snap.edges:
            factors = self._smoothed(edge.edge_id, self.edge_factors(edge), now)
            score = self.score(factors)
            raw = self.classify(score)
            level, _ = self._stabilise(("road", edge.edge_id), raw, now)
            results.append(EdgeCongestion(
                edge_id=edge.edge_id,
                junction_id=edge.junction_id,
                score=round(score, 4),
                raw_level=raw,
                level=level,
                factors=self._rounded(factors),
                dominant_factor=self.dominant_factor(factors),
                vehicle_count=edge.vehicle_count,
                queue_length_veh=edge.queue_length_veh,
                queue_length_m=edge.queue_length_m,
            ))
        return results

    def _evaluate_junction(
        self, junction: JunctionMetrics, edge_by_id: Dict[str, EdgeCongestion], now: float
    ) -> JunctionCongestion:
        approaches = [edge_by_id[e] for e in junction.incoming_edges if e in edge_by_id]
        worst = max(approaches, key=lambda e: e.score, default=None)
        score = worst.score if worst else 0.0
        factors = worst.factors if worst else CongestionFactors(
            occupancy=0.0, queue=0.0, waiting=0.0, speed_drop=0.0)

        raw = self.classify(score)
        level, since = self._stabilise(("junction", junction.junction_id), raw, now)
        requires, reason = self._attention_reason(level, since, now)

        return JunctionCongestion(
            junction_id=junction.junction_id,
            score=score,
            raw_level=raw,
            level=level,
            since_s=since,
            worst_edge_id=worst.edge_id if worst else None,
            dominant_factor=self.dominant_factor(factors),
            factors=factors,
            vehicle_count=junction.vehicle_count,
            queue_length_veh=junction.queue_length_veh,
            max_queue_length_m=junction.max_queue_length_m,
            mean_waiting_time_s=junction.mean_waiting_time_s,
            requires_attention=requires,
            attention_reason=reason,
        )

    def _attention_reason(
        self, level: CongestionLevel, since: float, now: float
    ) -> Tuple[bool, Optional[str]]:
        if level == CongestionLevel.HIGH:
            return True, f"HIGH congestion for {now - since:.0f} s"
        if level == CongestionLevel.MEDIUM and now - since >= self.config.attention_medium_after_s:
            return True, f"MEDIUM congestion has lasted {now - since:.0f} s"
        return False, None

    @staticmethod
    def _rounded(factors: CongestionFactors) -> CongestionFactors:
        return CongestionFactors(
            occupancy=round(factors.occupancy, 4), queue=round(factors.queue, 4),
            waiting=round(factors.waiting, 4), speed_drop=round(factors.speed_drop, 4),
        )

    @staticmethod
    def _summary(junctions: List[JunctionCongestion], edges: List[EdgeCongestion]) -> CongestionSummary:
        def count(items, level):
            return sum(1 for i in items if i.level == level)

        return CongestionSummary(
            junctions_low=count(junctions, CongestionLevel.LOW),
            junctions_medium=count(junctions, CongestionLevel.MEDIUM),
            junctions_high=count(junctions, CongestionLevel.HIGH),
            roads_low=count(edges, CongestionLevel.LOW),
            roads_medium=count(edges, CongestionLevel.MEDIUM),
            roads_high=count(edges, CongestionLevel.HIGH),
        )

    def _alerts(
        self, junctions: List[JunctionCongestion], edges: List[EdgeCongestion], now: float
    ) -> List[CongestionAlert]:
        """One alert per junction or road that is currently MEDIUM or HIGH."""
        alerts: List[CongestionAlert] = []
        window = self.config.smoothing_window_s
        averaged = f", averaged over the last {window:.0f} s" if window > 0 else ""
        for j in junctions:
            if j.level == CongestionLevel.LOW:
                continue
            what = _FACTOR_LABELS[j.dominant_factor]
            alerts.append(CongestionAlert(
                alert_id=f"junction:{j.junction_id}:{j.level.value}:{j.since_s:.0f}",
                entity_type="junction",
                entity_id=j.junction_id,
                level=j.level,
                severity="critical" if j.level == CongestionLevel.HIGH else "warning",
                score=j.score,
                since_s=j.since_s,
                is_new=j.since_s == now,
                message=(
                    f"Junction {j.junction_id}: {j.level.value} congestion (score {j.score:.2f}, "
                    f"mainly {what} on {j.worst_edge_id}{averaged}). "
                    f"Right now: {j.queue_length_veh} stopped vehicles, "
                    f"longest queue {j.max_queue_length_m:.0f} m, "
                    f"mean wait {j.mean_waiting_time_s:.0f} s."
                ),
            ))
        for e in edges:
            if e.level == CongestionLevel.LOW:
                continue
            since = self._states[("road", e.edge_id)].since_s
            where = f" (approach to {e.junction_id})" if e.junction_id else ""
            alerts.append(CongestionAlert(
                alert_id=f"road:{e.edge_id}:{e.level.value}:{since:.0f}",
                entity_type="road",
                entity_id=e.edge_id,
                level=e.level,
                severity="critical" if e.level == CongestionLevel.HIGH else "warning",
                score=e.score,
                since_s=since,
                is_new=since == now,
                message=(
                    f"Road {e.edge_id}{where}: {e.level.value} congestion "
                    f"(score {e.score:.2f}{averaged}). Right now: {e.vehicle_count} vehicles, "
                    f"{e.queue_length_veh} stopped, queue {e.queue_length_m:.0f} m."
                ),
            ))
        # critical first, then junctions before roads, then worst score first
        alerts.sort(key=lambda a: (-a.level.rank, a.entity_type != "junction", -a.score, a.entity_id))
        return alerts

    @staticmethod
    def _attention(junctions: List[JunctionCongestion]) -> List[AttentionJunction]:
        flagged = [j for j in junctions if j.requires_attention]
        flagged.sort(key=lambda j: (-j.level.rank, -j.score, j.junction_id))
        return [
            AttentionJunction(
                rank=i,
                junction_id=j.junction_id,
                level=j.level,
                score=j.score,
                reason=j.attention_reason or "",
                worst_edge_id=j.worst_edge_id,
                max_queue_length_m=j.max_queue_length_m,
                mean_waiting_time_s=j.mean_waiting_time_s,
            )
            for i, j in enumerate(flagged, start=1)
        ]