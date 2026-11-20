"""Adaptive signal recommendations (advice only; nothing is executed here).

For every signalised junction the advisor looks at how busy each *stage* is (a stage is one
green phase plus the yellow / all-red that follow it) and suggests:

* **a green-time plan** - move green seconds from the quieter stage to the busier one, a few
  seconds at a time, keeping the cycle length the same;
* **an action for the phase running now** - extend a green that is about to end while its
  traffic is still heavy, or end an idle green early when another stage is waiting.

Safety first. Every recommendation passes ``validate_plan`` before it is returned (and the
orchestrator can call the same function on any plan). The rules:

* every stage is served in every cycle (no stage is skipped or starved),
* green stays within ``min_green_s`` .. ``max_green_s``,
* yellow and all-red are never shortened below the program's own values (or the configured
  floors) and the phase order is never changed,
* the cycle never exceeds ``max_cycle_s``,
* a green changes by at most ``max_change_s`` per recommendation.

If a recommendation would break a rule, the advisor returns "keep" instead. During yellow and
all-red the only possible action is "in_clearance" (nothing may change).

Pure Python on the Pydantic models; no SUMO import. ``SignalAdvisor.from_source`` reads the
programs from a running ``SimulationInterface``.

Typical use::

    advisor = SignalAdvisor.from_source(sim)
    report = advisor.recommend(snapshot, congestion_report, prediction_report)
    print(report.to_dict())
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from pydantic import ValidationError

from .config import CongestionConfig, SignalConfig
from .congestion_detector import CongestionDetector
from .exceptions import InvalidTrafficDataError, UnsafeSignalPlanError
from .schemas import (
    CongestionLevel,
    CongestionReport,
    ImmediateAction,
    JunctionCongestion,
    JunctionMetrics,
    JunctionPrediction,
    JunctionSignalRecommendation,
    PredictionReport,
    RawSignalProgram,
    SafetyCheck,
    SignalPhase,
    SignalProgram,
    SignalRecommendationReport,
    SignalStage,
    SignalSummary,
    StagePlan,
    TrafficSnapshot,
)

logger = logging.getLogger(__name__)

_GREEN_CHARS = "Gg"
_YELLOW_CHARS = "yY"
_PRIORITY = {CongestionLevel.LOW: "low", CongestionLevel.MEDIUM: "medium", CongestionLevel.HIGH: "high"}


# --------------------------------------------------------------------------- #
# Signal program -> stages
# --------------------------------------------------------------------------- #
def _phase_kind(state: str) -> str:
    if any(c in _GREEN_CHARS for c in state):
        return "green"
    if any(c in _YELLOW_CHARS for c in state):
        return "yellow"
    return "all_red"


def build_signal_program(raw: Union[RawSignalProgram, Dict[str, Any]]) -> SignalProgram:
    """Split a fixed-time program into stages (one green phase + its clearance phases).

    Raises:
        InvalidTrafficDataError: no green phase, a phase that does not match the links, or a
            stage that gives green to no road.
    """
    try:
        program = raw if isinstance(raw, RawSignalProgram) else RawSignalProgram.model_validate(raw)
    except ValidationError as exc:
        raise InvalidTrafficDataError(f"Invalid signal program: {exc}") from exc

    jid = program.junction_id
    phases = [SignalPhase(index=i, state=p.state, duration_s=p.duration_s, kind=_phase_kind(p.state))
              for i, p in enumerate(program.phases)]
    for phase in phases:
        if len(phase.state) != len(program.link_edges):
            raise InvalidTrafficDataError(
                f"Junction {jid}: phase {phase.index} has {len(phase.state)} signals but the junction "
                f"has {len(program.link_edges)} links.")
    greens = [p.index for p in phases if p.kind == "green"]
    if not greens:
        raise InvalidTrafficDataError(f"Junction {jid}: the signal program has no green phase.")

    stages: List[SignalStage] = []
    for number, green_index in enumerate(greens, start=1):
        clearance: List[int] = []
        j = (green_index + 1) % len(phases)
        while j != green_index and phases[j].kind != "green":
            clearance.append(j)
            j = (j + 1) % len(phases)
        edges = sorted({program.link_edges[k] for k, c in enumerate(phases[green_index].state)
                        if c in _GREEN_CHARS and program.link_edges[k]})
        if not edges:
            raise InvalidTrafficDataError(f"Junction {jid}: green phase {green_index} serves no road.")
        stages.append(SignalStage(
            stage_id=number, green_phase=green_index, clearance_phases=clearance, edges=edges,
            green_s=phases[green_index].duration_s,
            yellow_s=sum(phases[c].duration_s for c in clearance if phases[c].kind == "yellow"),
            all_red_s=sum(phases[c].duration_s for c in clearance if phases[c].kind == "all_red"),
        ))
    return SignalProgram(junction_id=jid, phases=phases, stages=stages,
                         cycle_s=sum(p.duration_s for p in phases))


# --------------------------------------------------------------------------- #
# Safety validator
# --------------------------------------------------------------------------- #
def validate_plan(
    program: SignalProgram,
    greens: Mapping[int, float],
    config: Optional[SignalConfig] = None,
    previous_greens: Optional[Mapping[int, float]] = None,
    yellow_s: Optional[Mapping[int, float]] = None,
    all_red_s: Optional[Mapping[int, float]] = None,
) -> List[str]:
    """Check a green-time plan against the safety rules.

    Args:
        program: the junction's signal program (stages and their clearance times).
        greens: proposed green seconds by ``stage_id``.
        previous_greens: the plan currently running; if given, the change limit is checked.
        yellow_s / all_red_s: proposed clearance times by ``stage_id``; default to the program's.

    Returns:
        The names of the rules that were checked (all passed).

    Raises:
        UnsafeSignalPlanError: lists every rule that was broken.
    """
    cfg = config or SignalConfig()
    problems: List[str] = []
    rules = ["every_stage_served", "min_green", "max_green", "yellow_not_shortened",
             "all_red_not_shortened", "max_cycle"]
    stage_ids = [s.stage_id for s in program.stages]

    unknown = sorted(set(greens) - set(stage_ids))
    if unknown:
        problems.append(f"unknown stage(s) {unknown}")
    cycle = 0.0
    for stage in program.stages:
        green = greens.get(stage.stage_id)
        if green is None or not _is_number(green) or green <= 0:
            problems.append(f"stage {stage.stage_id} is not served (no green time): every stage needs green every cycle")
            continue
        if green < cfg.min_green_s:
            problems.append(f"stage {stage.stage_id}: green {green:g} s is below the minimum of {cfg.min_green_s} s")
        if green > cfg.max_green_s:
            problems.append(f"stage {stage.stage_id}: green {green:g} s is above the maximum of {cfg.max_green_s} s")
        yellow = stage.yellow_s if yellow_s is None else yellow_s.get(stage.stage_id, stage.yellow_s)
        red = stage.all_red_s if all_red_s is None else all_red_s.get(stage.stage_id, stage.all_red_s)
        if yellow < max(stage.yellow_s, cfg.min_yellow_s):
            problems.append(f"stage {stage.stage_id}: yellow {yellow:g} s is shorter than the required "
                            f"{max(stage.yellow_s, cfg.min_yellow_s):g} s")
        if red < max(stage.all_red_s, cfg.min_all_red_s):
            problems.append(f"stage {stage.stage_id}: all-red {red:g} s is shorter than the required "
                            f"{max(stage.all_red_s, cfg.min_all_red_s):g} s")
        cycle += green + yellow + red
        if previous_greens is not None and stage.stage_id in previous_greens:
            change = abs(green - previous_greens[stage.stage_id])
            if change > cfg.max_change_s + 1e-9:
                problems.append(f"stage {stage.stage_id}: green changes by {change:g} s, more than the "
                                f"{cfg.max_change_s} s allowed per step")
    if previous_greens is not None:
        rules.append("max_change_per_step")
    if cycle > cfg.max_cycle_s + 1e-9:
        problems.append(f"cycle of {cycle:g} s exceeds the maximum of {cfg.max_cycle_s} s")
    if problems:
        raise UnsafeSignalPlanError(f"Junction {program.junction_id}: " + "; ".join(problems))
    return rules


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# --------------------------------------------------------------------------- #
# Green-time allocation helpers
# --------------------------------------------------------------------------- #
def _split_total(weights: Mapping[int, float], total: float, low: float, high: float) -> Dict[int, float]:
    """Share ``total`` seconds in proportion to ``weights``, keeping each share in [low, high]."""
    values: Dict[int, float] = {}
    free = set(weights)
    for _ in range(len(weights) + 1):
        if not free:
            break
        remaining = total - sum(values.values())
        weight_sum = sum(weights[k] for k in free)
        trial = {k: remaining * weights[k] / weight_sum for k in free}
        out_of_range = {k: min(max(v, low), high) for k, v in trial.items() if v < low or v > high}
        if not out_of_range:
            values.update(trial)
            break
        values.update(out_of_range)
        free -= set(out_of_range)
    return values


def _step_toward(current: Mapping[int, float], target: Mapping[int, float], max_change: float) -> Dict[int, float]:
    """Move each green toward its target by at most ``max_change``, keeping the total unchanged."""
    delta = {k: max(-max_change, min(max_change, target[k] - current[k])) for k in current}
    up = sum(d for d in delta.values() if d > 0)
    down = -sum(d for d in delta.values() if d < 0)
    if up > down > 0:
        delta = {k: (d * down / up if d > 0 else d) for k, d in delta.items()}
    elif down > up > 0:
        delta = {k: (d * up / down if d < 0 else d) for k, d in delta.items()}
    elif up == 0 or down == 0:
        delta = {k: 0.0 for k in delta}
    return {k: current[k] + delta[k] for k in current}


def _round_keeping_total(
    values: Mapping[int, float], total: int, demand: Mapping[int, float],
    low: float, high: float, current: Mapping[int, float], max_change: float,
) -> Dict[int, float]:
    """Whole seconds with the same total as before; any leftover second goes to the busiest stage."""
    rounded = {k: float(round(v)) for k, v in values.items()}
    for _ in range(len(rounded) * 2):
        diff = total - sum(rounded.values())
        if diff == 0:
            break
        order = sorted(rounded, key=lambda k: demand[k], reverse=diff > 0)
        step = 1.0 if diff > 0 else -1.0
        for k in order:
            candidate = rounded[k] + step
            if low <= candidate <= high and abs(candidate - current[k]) <= max_change:
                rounded[k] = candidate
                break
        else:
            break
    return rounded


# --------------------------------------------------------------------------- #
# The advisor
# --------------------------------------------------------------------------- #
class SignalAdvisor:
    """Turns congestion (and optionally predictions) into safe signal recommendations."""

    def __init__(
        self,
        programs: Mapping[str, Union[SignalProgram, RawSignalProgram]],
        config: Optional[SignalConfig] = None,
        congestion_config: Optional[CongestionConfig] = None,
        detector: Optional[CongestionDetector] = None,
    ) -> None:
        self.config = config or SignalConfig()
        self.congestion_config = congestion_config or CongestionConfig()
        self._detector = detector or CongestionDetector(self.congestion_config)
        self._programs: Dict[str, SignalProgram] = {}
        for junction_id, program in programs.items():
            built = program if isinstance(program, SignalProgram) else build_signal_program(program)
            if built.junction_id != junction_id:
                raise InvalidTrafficDataError(
                    f"Program for {built.junction_id} was given under the name {junction_id}.")
            self._programs[junction_id] = built
        self._plans: Dict[str, Dict[int, float]] = {
            jid: {s.stage_id: s.green_s for s in p.stages} for jid, p in self._programs.items()
        }
        # junction -> (phase index, estimated start time, time last seen): how long a phase has run
        self._phase_track: Dict[str, Tuple[int, float, float]] = {}

    @classmethod
    def from_source(cls, source: Any, **kwargs: Any) -> "SignalAdvisor":
        """Read the programs from anything with ``read_signal_programs()`` (a SimulationInterface)."""
        return cls(source.read_signal_programs(), **kwargs)

    # ------------------------------------------------------------------ #
    @property
    def junction_ids(self) -> List[str]:
        return sorted(self._programs)

    def program(self, junction_id: str) -> SignalProgram:
        try:
            return self._programs[junction_id]
        except KeyError:
            raise InvalidTrafficDataError(f"No signal program for junction {junction_id}.") from None

    def current_plan(self, junction_id: str) -> Dict[int, float]:
        """Green seconds by stage that recommendations are measured against."""
        self.program(junction_id)
        return dict(self._plans[junction_id])

    def set_current_plan(self, junction_id: str, greens: Mapping[int, float]) -> None:
        """Tell the advisor which plan is now running (call after a recommendation was applied).

        Raises:
            InvalidTrafficDataError: unknown junction.
            UnsafeSignalPlanError: the plan breaks a safety rule.
        """
        program = self.program(junction_id)
        validate_plan(program, greens, self.config)
        self._plans[junction_id] = {int(k): float(v) for k, v in greens.items()}

    def reset(self) -> None:
        """Forget applied plans and congestion history (e.g. after a simulation restart)."""
        self._plans = {jid: {s.stage_id: s.green_s for s in p.stages} for jid, p in self._programs.items()}
        self._phase_track.clear()
        self._detector.reset()

    # ------------------------------------------------------------------ #
    def recommend(
        self,
        snapshot: Union[TrafficSnapshot, Dict[str, Any]],
        congestion: Optional[Union[CongestionReport, Dict[str, Any]]] = None,
        prediction: Optional[Union[PredictionReport, Dict[str, Any]]] = None,
    ) -> SignalRecommendationReport:
        """Recommendations for every junction in the snapshot that has a signal program.

        Pass the matching congestion report (same time) if you already have one; otherwise one
        is computed (call once per snapshot, in time order). A prediction report is optional: when
        it comes from a trained model, a forecast of worsening raises the priority.

        Raises:
            InvalidTrafficDataError: malformed input, or reports from a different time.
        """
        snap = _validated(snapshot, TrafficSnapshot, "snapshot")
        report = (self._detector.update(snap) if congestion is None
                  else _validated(congestion, CongestionReport, "congestion report"))
        if abs(report.timestamp_s - snap.timestamp_s) > 1e-6:
            raise InvalidTrafficDataError(
                f"Snapshot (t={snap.timestamp_s}) and congestion report (t={report.timestamp_s}) "
                "are from different times.")
        notes: List[str] = []
        predictions: Dict[str, JunctionPrediction] = {}
        if prediction is not None:
            pred = _validated(prediction, PredictionReport, "prediction report")
            if pred.method == "ml":
                predictions = {p.junction_id: p for p in pred.predictions}
            else:
                notes.append("The prediction report is a fallback (no trained model): not used.")
        else:
            notes.append("No prediction supplied: recommendations use current congestion only.")

        edge_scores = {e.edge_id: e.score for e in report.edges}
        congestion_by_junction = {j.junction_id: j for j in report.junctions}
        recommendations: List[JunctionSignalRecommendation] = []
        for jm in sorted(snap.junctions, key=lambda j: j.junction_id):
            program = self._programs.get(jm.junction_id)
            cj = congestion_by_junction.get(jm.junction_id)
            if program is None or cj is None:
                notes.append(f"Junction {jm.junction_id} skipped: "
                             + ("no signal program." if program is None else "not in the congestion report."))
                continue
            recommendations.append(self._recommend_junction(
                program, jm, cj, edge_scores, predictions.get(jm.junction_id), snap.timestamp_s))

        return SignalRecommendationReport(
            timestamp_s=snap.timestamp_s,
            summary=SignalSummary(
                junctions_rebalance=sum(r.action == "rebalance" for r in recommendations),
                junctions_keep=sum(r.action == "keep" for r in recommendations),
                immediate_actions=sum(r.immediate.action in ("extend_green", "end_green_early")
                                      for r in recommendations)),
            recommendations=recommendations, notes=notes)

    # ------------------------------------------------------------------ #
    def _recommend_junction(
        self, program: SignalProgram, jm: JunctionMetrics, cj: JunctionCongestion,
        edge_scores: Mapping[str, float], predicted: Optional[JunctionPrediction], now: float,
    ) -> JunctionSignalRecommendation:
        cfg = self.config
        jid = program.junction_id
        current = dict(self._plans[jid])
        demand = {s.stage_id: max((edge_scores.get(e, 0.0) for e in s.edges), default=0.0)
                  for s in program.stages}
        predicted_level = predicted.predicted_level if predicted is not None else None
        yellow = max((s.yellow_s for s in program.stages), default=0.0)
        all_red = max((s.all_red_s for s in program.stages), default=0.0)

        def build(plan: Mapping[int, float], action: str, immediate: ImmediateAction, reason: str,
                  rules: List[str], validated: bool = True,
                  elapsed: Optional[float] = None) -> JunctionSignalRecommendation:
            clearance = sum(s.yellow_s + s.all_red_s for s in program.stages)
            return JunctionSignalRecommendation(
                junction_id=jid, level=cj.level, predicted_level=predicted_level,
                priority=self._priority(cj.level, predicted_level, action, immediate),
                action=action,  # type: ignore[arg-type]
                stages=[StagePlan(stage_id=s.stage_id, edges=s.edges, demand=round(demand[s.stage_id], 4),
                                  current_green_s=current[s.stage_id], recommended_green_s=plan[s.stage_id],
                                  change_s=plan[s.stage_id] - current[s.stage_id]) for s in program.stages],
                yellow_s=yellow, all_red_s=all_red, cycle_s=sum(plan.values()) + clearance,
                current_phase=jm.current_phase, phase_elapsed_s=elapsed,
                immediate=immediate, reason=reason, safety=SafetyCheck(validated=validated, rules_checked=rules))

        # the plan that is running must itself be within the limits
        try:
            validate_plan(program, current, cfg)
        except UnsafeSignalPlanError as exc:
            return build(current, "keep", ImmediateAction(action="none"),
                         f"The running plan is outside the safety limits, so no changes are recommended "
                         f"until it is fixed. ({exc})", [], validated=False)

        immediate, elapsed = self._immediate_action(program, jm, current, demand, now)

        # green-time rebalancing
        gap = max(demand.values()) - min(demand.values())
        plan, reason = dict(current), ""
        if max(demand.values()) < cfg.min_demand_to_act:
            reason = "Traffic is light on every approach; keep the current plan."
        elif gap < cfg.imbalance_deadband:
            reason = self._balanced_reason(cj.level, demand)
        else:
            weights = {k: v + cfg.demand_epsilon for k, v in demand.items()}
            total = sum(current.values())
            target = _split_total(weights, total, cfg.min_green_s, cfg.max_green_s)
            stepped = _step_toward(current, target, cfg.max_change_s)
            plan = _round_keeping_total(stepped, int(round(total)), demand, cfg.min_green_s, cfg.max_green_s,
                                        current, cfg.max_change_s)
            if all(abs(plan[k] - current[k]) < 1 for k in plan):
                plan, reason = dict(current), "Demand is uneven but the greens are already close to the best split."
            else:
                reason = self._rebalance_reason(program, current, plan, demand)

        try:
            rules = validate_plan(program, plan, cfg, previous_greens=current)
        except UnsafeSignalPlanError as exc:   # defence in depth: never return an unsafe plan
            logger.error("Discarded an unsafe recommendation: %s", exc)
            return build(current, "keep", immediate, f"A change was considered but rejected by the safety check ({exc}).",
                         [], validated=False, elapsed=elapsed)
        action = "rebalance" if any(abs(plan[k] - current[k]) >= 1 for k in plan) else "keep"
        if immediate.reason and immediate.action in ("extend_green", "end_green_early"):
            reason = (reason + " " + immediate.reason).strip()
        return build(plan, action, immediate, reason, rules, elapsed=elapsed)

    # ------------------------------------------------------------------ #
    def _immediate_action(
        self, program: SignalProgram, jm: JunctionMetrics, current: Mapping[int, float],
        demand: Mapping[int, float], now: float,
    ) -> Tuple[ImmediateAction, Optional[float]]:
        cfg = self.config
        index, remaining = jm.current_phase, jm.phase_remaining_s
        if index is None or remaining is None or index >= len(program.phases):
            return ImmediateAction(action="none", reason="The signal phase is not known."), None
        phase = program.phases[index]
        stage = next((s for s in program.stages if s.green_phase == index), None)
        planned = current[stage.stage_id] if stage is not None else phase.duration_s
        elapsed = self._phase_elapsed(program, index, planned, remaining, now)
        if phase.kind != "green":
            return ImmediateAction(
                action="in_clearance",
                reason="Yellow / all-red is running; it is never shortened or changed."), elapsed
        if stage is None:
            return ImmediateAction(action="none"), elapsed
        mine = demand[stage.stage_id]
        others = [d for k, d in demand.items() if k != stage.stage_id]
        others_max = max(others, default=0.0)
        medium = self.congestion_config.medium_threshold

        if elapsed >= cfg.min_green_s and mine < cfg.end_early_demand and others_max >= medium:
            return ImmediateAction(
                action="end_green_early",
                reason=(f"Stage {stage.stage_id} has almost no traffic (score {mine:.2f}) while another stage "
                        f"is waiting (score {others_max:.2f}): its green can end now (it already ran "
                        f"{elapsed:.0f} s; yellow and all-red still follow in full).")), elapsed

        if remaining <= cfg.extend_when_remaining_s and mine >= medium and mine - others_max >= cfg.imbalance_deadband:
            headroom = min(cfg.max_extension_s,
                           cfg.max_green_s - (elapsed + remaining),
                           cfg.max_cycle_s - self._cycle_s(program, current))
            extension = math.floor(max(0.0, headroom) / cfg.extend_step_s) * cfg.extend_step_s
            if extension >= cfg.extend_step_s:
                return ImmediateAction(
                    action="extend_green", extend_s=float(extension),
                    reason=(f"Stage {stage.stage_id} still has heavy traffic (score {mine:.2f} vs "
                            f"{others_max:.2f}): extend its current green by {extension:.0f} s (once; "
                            f"stays within the {cfg.max_green_s} s maximum).")), elapsed
        return ImmediateAction(action="none"), elapsed

    def _phase_elapsed(self, program: SignalProgram, index: int, planned_s: float,
                       remaining_s: float, now: float) -> float:
        """Seconds the running phase has lasted so far, including any earlier extension.

        The advisor notes when it first sees a phase: a phase starts with its planned duration,
        so its start is ``now - (planned - remaining)``. Later snapshots reuse that start, so the
        figure stays right after an extension (when "planned - remaining" no longer is).
        """
        track = self._phase_track.get(program.junction_id)
        same = (track is not None and track[0] == index and track[2] <= now
                and now - track[2] <= program.cycle_s)
        start = track[1] if same else now - max(0.0, planned_s - remaining_s)
        self._phase_track[program.junction_id] = (index, start, now)
        return max(0.0, now - start)

    @staticmethod
    def _cycle_s(program: SignalProgram, greens: Mapping[int, float]) -> float:
        return sum(greens[s.stage_id] + s.yellow_s + s.all_red_s for s in program.stages)

    @staticmethod
    def _priority(level: CongestionLevel, predicted: Optional[CongestionLevel], action: str,
                  immediate: ImmediateAction) -> str:
        worst = max(level, predicted or level, key=lambda lv: lv.rank)
        if worst is CongestionLevel.LOW:
            acting = action == "rebalance" or immediate.action in ("extend_green", "end_green_early")
            return "low" if acting else "none"
        return _PRIORITY[worst]

    @staticmethod
    def _stage_label(program: SignalProgram, stage_id: int) -> str:
        stage = next(s for s in program.stages if s.stage_id == stage_id)
        return f"stage {stage_id} ({', '.join(stage.edges)})"

    def _rebalance_reason(self, program: SignalProgram, current: Mapping[int, float],
                          plan: Mapping[int, float], demand: Mapping[int, float]) -> str:
        busiest = max(demand, key=lambda k: demand[k])
        parts = [f"{self._stage_label(program, k)}: {current[k]:.0f} s -> {plan[k]:.0f} s"
                 for k in sorted(plan) if abs(plan[k] - current[k]) >= 1]
        quiet = min(demand, key=lambda k: demand[k])
        label = self._stage_label(program, busiest)
        return (f"{label[0].upper()}{label[1:]} is the busiest "
                f"(score {demand[busiest]:.2f} vs {demand[quiet]:.2f}); green time moves toward it: "
                + "; ".join(parts) + ". The cycle length stays the same.")

    def _balanced_reason(self, level: CongestionLevel, demand: Mapping[int, float]) -> str:
        if level is not CongestionLevel.LOW:
            return ("Every approach is busy to a similar degree, so moving green time would not help; "
                    "keep the current plan (relief would need to come from upstream junctions).")
        return "Traffic is balanced across the stages; keep the current plan."


def _validated(value: Any, model: Any, what: str) -> Any:
    try:
        return value if isinstance(value, model) else model.model_validate(value)
    except ValidationError as exc:
        raise InvalidTrafficDataError(f"Invalid {what}: {exc}") from exc