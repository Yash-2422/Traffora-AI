"""Independent safety layer for every signal write.

Design rules
------------
1. The orchestrator never changes a phase index and never writes a
   red/yellow/green state string. The only TraCI write is
   ``trafficlight.setPhaseDuration``. Because the signal program's own
   conflict-free phase sequence stays in force, two conflicting greens can
   never be created by the orchestrator.
2. Every write must respect the program's minimum green, the program's maximum
   green, the configured absolute limits and the running clearance interval.
3. Emergency truncation may go down to the program's own ``minDur`` (or a
   documented 4 s floor when ``minDur`` is 0), but never lower, and never
   during yellow/all-red.
4. If the live signal state cannot be read, the action is DEFERRED, not
   applied. Invalid actions are REJECTED and never reach SUMO.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .config import SafetyLimits
from .schemas import ActionType, SignalAction, ValidationResult, ValidationStatus

logger = logging.getLogger(__name__)

_ALLOWED_ACTIONS = {ActionType.SET_PHASE_DURATION, ActionType.RESTORE_SIGNAL}
#: Phase kinds during which the junction is clearing. ``SimulationCoordinator``
#: reports a yellow stage as ``YELLOW`` and an all-red stage as ``RED`` (every
#: link red), so both must be treated as untouchable clearance intervals.
_CLEARANCE_KINDS = {"CLEARANCE", "ALL_RED", "YELLOW", "RED"}


class SafetyValidator:
    """Validates individual actions and whole batches."""

    def __init__(self, limits: Optional[SafetyLimits] = None,
                 known_tls: Optional[Sequence[str]] = None) -> None:
        self.limits = limits or SafetyLimits()
        self._known_tls: Set[str] = set(known_tls or ())

    # ------------------------------------------------------------------ #
    # Network knowledge
    # ------------------------------------------------------------------ #
    def set_known_tls(self, tls_ids: Iterable[str]) -> None:
        self._known_tls = {str(t) for t in tls_ids}

    @property
    def known_tls(self) -> Set[str]:
        return set(self._known_tls)

    # ------------------------------------------------------------------ #
    # Single action
    # ------------------------------------------------------------------ #
    def validate(self, action: SignalAction,
                 signal_state: Optional[Mapping[str, Any]] = None,
                 *, emergency: bool = False,
                 timestamp_s: float = 0.0) -> ValidationResult:
        reasons: List[str] = []

        def reject(reason: str) -> ValidationResult:
            reasons.append(reason)
            return ValidationResult(valid=False, status=ValidationStatus.REJECTED,
                                    reasons=reasons, checked_at_s=timestamp_s)

        def defer(reason: str) -> ValidationResult:
            reasons.append(reason)
            return ValidationResult(valid=False, status=ValidationStatus.DEFERRED,
                                    reasons=reasons, checked_at_s=timestamp_s)

        if not isinstance(action, SignalAction):
            return reject("action is not a SignalAction")
        if not action.tls_id:
            return reject("tls_id is empty")
        if self.limits.require_known_tls and self._known_tls and action.tls_id not in self._known_tls:
            return reject(f"unknown traffic light '{action.tls_id}'")
        if action.action_type not in _ALLOWED_ACTIONS:
            return reject(f"action type {action.action_type.value} is not permitted")

        duration = action.duration_s
        if duration is None or not math.isfinite(float(duration)) or float(duration) <= 0:
            return reject(f"duration_s must be a positive finite number, got {duration!r}")
        duration = float(duration)

        if signal_state is None:
            return defer(f"no live state for '{action.tls_id}'; cannot validate safely")

        state_tls = signal_state.get("tls_id", action.tls_id)
        if state_tls != action.tls_id:
            return reject(f"signal state is for '{state_tls}', not '{action.tls_id}'")

        current_phase = signal_state.get("phase")
        num_phases = signal_state.get("num_phases")
        remaining = signal_state.get("remaining_s")
        phase_kind = str(signal_state.get("phase_kind", "") or "").upper()
        elapsed = signal_state.get("elapsed_s")
        program_min = float(signal_state.get("min_dur", 0.0) or 0.0)
        program_max = float(signal_state.get("max_dur", 0.0) or 0.0)

        # Clearance intervals always run to completion, untouched. Checked
        # before the phase-index rule so the reason names the real problem.
        if phase_kind in _CLEARANCE_KINDS:
            return reject(f"phase {current_phase} is a {phase_kind} clearance interval; "
                          "yellow/all-red may not be shortened or extended")

        # Never switch phases: a phase index that differs from the live one
        # could introduce a conflicting green.
        if action.phase_index is not None:
            if num_phases is not None and not 0 <= int(action.phase_index) < int(num_phases):
                return reject(f"phase index {action.phase_index} does not exist "
                              f"(program has {num_phases} phases)")
            if current_phase is not None and int(action.phase_index) != int(current_phase):
                return reject(
                    f"phase change requested ({current_phase} -> {action.phase_index}); "
                    "the orchestrator may only adjust the duration of the running phase")

        # Minimum green.
        if emergency:
            # Preemption is exactly the case in which a green may legitimately be
            # cut short, so the program's own minDur is deliberately not enforced
            # here. The emergency floor (default 4 s) and min_remaining_green_s
            # still apply, and the program runs its normal yellow/all-red
            # clearance afterwards because the phase index never changes.
            floor = self.limits.min_emergency_green_s
        else:
            floor = max(self.limits.min_green_s, program_min)
        # ``setPhaseDuration`` sets the *remaining* duration of the running
        # phase, and SUMO ends the phase once that many seconds have elapsed in
        # it. So the green actually served by this write is
        # ``max(elapsed, requested)`` -- not ``elapsed + requested``, and not
        # ``requested`` alone. Using the wrong figure here let a sequence of
        # short holds pin a phase green indefinitely.
        served = duration if elapsed is None else max(float(elapsed), duration)
        if served < floor - 1e-6:
            return reject(
                f"total green {served:.1f}s (elapsed {float(elapsed or 0.0):.1f}s, "
                f"requested {duration:.1f}s) is below the minimum green "
                f"({floor:.1f}s, emergency={emergency})")

        # Maximum green, measured over the whole phase, not just this write.
        ceiling = self.limits.max_green_s
        if program_max > 0:
            ceiling = min(ceiling, program_max)
        if served > ceiling + 1e-6:
            return reject(f"total green {served:.1f}s (elapsed {float(elapsed or 0.0):.1f}s, "
                          f"requested {duration:.1f}s) exceeds the maximum green "
                          f"({ceiling:.1f}s)")

        # Never leave a stage with almost no green left.
        if duration < self.limits.min_remaining_green_s - 1e-6:
            return reject(f"duration {duration:.1f}s is below min_remaining_green_s "
                          f"({self.limits.min_remaining_green_s:.1f}s)")

        # A single write may not extend by more than the configured amount.
        if remaining is not None:
            extension = duration - float(remaining)
            if extension > self.limits.max_extension_s + 1e-6:
                return reject(f"extension of {extension:.1f}s exceeds "
                              f"max_extension_s ({self.limits.max_extension_s:.1f}s)")

        # Cycle sanity.
        if duration > self.limits.max_cycle_s + 1e-6:
            return reject(f"duration {duration:.1f}s exceeds max_cycle_s "
                          f"({self.limits.max_cycle_s:.1f}s)")

        # Emergency provenance.
        if action.emergency_vehicle_id is not None and not action.emergency_vehicle_id.strip():
            return reject("emergency_vehicle_id is empty")
        if emergency and not action.emergency_vehicle_id:
            return reject("emergency action has no emergency_vehicle_id")
        if not emergency and action.emergency_vehicle_id:
            return reject("action claims an emergency vehicle but was not flagged as emergency")

        reasons.append("all safety rules passed")
        return ValidationResult(valid=True, status=ValidationStatus.VALID,
                                reasons=reasons, checked_at_s=timestamp_s)

    # ------------------------------------------------------------------ #
    # Batch
    # ------------------------------------------------------------------ #
    def validate_batch(
        self,
        actions: Sequence[SignalAction],
        signal_states: Mapping[str, Mapping[str, Any]],
        *, emergency_tls: Optional[Set[str]] = None,
        timestamp_s: float = 0.0,
    ) -> Tuple[List[SignalAction], List[ValidationResult]]:
        """Validate a batch, rejecting duplicate targets and conflicts.

        Returns ``(accepted_actions, results)`` where ``results`` is aligned
        with the input order.
        """
        emergency_tls = emergency_tls or set()
        accepted: List[SignalAction] = []
        results: List[ValidationResult] = []
        seen_tls: Dict[str, int] = {}

        for index, action in enumerate(actions):
            if action.tls_id in seen_tls:
                results.append(ValidationResult(
                    valid=False, status=ValidationStatus.REJECTED,
                    reasons=[f"conflicting simultaneous actions for '{action.tls_id}' "
                             f"(first at index {seen_tls[action.tls_id]})"],
                    checked_at_s=timestamp_s))
                continue
            result = self.validate(
                action,
                signal_states.get(action.tls_id),
                emergency=(action.tls_id in emergency_tls or
                           bool(action.emergency_vehicle_id)),
                timestamp_s=timestamp_s,
            )
            results.append(result)
            if result.valid:
                seen_tls[action.tls_id] = index
                accepted.append(action)
        return accepted, results

    def is_safe(self, action: SignalAction,
                signal_state: Optional[Mapping[str, Any]] = None, *,
                emergency: bool = False) -> bool:
        return self.validate(action, signal_state, emergency=emergency).valid
