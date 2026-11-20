"""GreenCorridor - Step 7: Safe Signal Priority Request Generation.

Core safety rule (see project spec section 4.D):

    GreenCorridor NEVER sets a traffic light directly to a new phase index
    and NEVER writes a fabricated phase-state string. Either could put two
    conflicting movements on green at once. The only call this module makes
    to a signal is:

        tc.trafficlight.setPhaseDuration(tls_id, seconds)

    which only shortens-or-lengthens the *remaining time of the phase the
    signal is already in*. The signal always continues through its own
    programmed sequence -- including every yellow/all-red phase, which this
    module never shortens -- so the inter-green clearance interval the
    traffic engineer designed into the program is always respected,
    regardless of how PRIORITY_RESPECT_MIN_DUR below is configured.

    PRIORITY_RESPECT_MIN_DUR (config.py) governs a *service*, not a safety,
    tradeoff: whether a conflicting green may be shortened below its own
    programmed minDur. On a fixed-time program minDur == duration, so
    respecting it (the default) means this module will not shorten that
    phase at all -- it only ever waits. Setting it False allows shortening
    down to PRIORITY_MIN_GREEN_FLOOR_S instead; this still only affects how
    much green that approach gets, because the next phase is always the
    program's own yellow/all-red either way.

Public API (project spec section 7):
    generate_signal_priority_request(manager, vehicle_id) -> envelope
    restore_normal_operation(manager, tls_id)             -> envelope
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

import traci  # only used for the shared TraCIException/FatalTraCIError classes

from . import config
from .api_utils import guarded
from .intersection_monitor import IntersectionMonitor
from .schemas import IntersectionStatus, SignalSnapshot, UpcomingIntersection

logger = logging.getLogger(__name__)

# --- Signal priority (Step 7) -----------------------------------------------
PRIORITY_REQUEST_HORIZON_M = getattr(config, "PRIORITY_REQUEST_HORIZON_M", 250.0)
PRIORITY_RESPECT_MIN_DUR = getattr(config, "PRIORITY_RESPECT_MIN_DUR", True)
PRIORITY_MIN_GREEN_FLOOR_S = getattr(config, "PRIORITY_MIN_GREEN_FLOOR_S", 4.0)
PRIORITY_MAX_GREEN_EXTENSION_S = getattr(config, "PRIORITY_MAX_GREEN_EXTENSION_S", 20.0)
PRIORITY_ETA_BUFFER_S = getattr(config, "PRIORITY_ETA_BUFFER_S", 2.0)
PRIORITY_COOLDOWN_S = getattr(config, "PRIORITY_COOLDOWN_S", 5.0)
# Append the five lines above to config.py's own "Signal priority (Step 7)"
# section; the getattr() defaults here just mean this module still runs if
# you haven't copied them over yet.


class PriorityError(Exception):
    """Same shape as SumoConnectionError / IntersectionError: a stable code
    plus a human message, so guarded() can turn it into the JSON envelope."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class PriorityState(str, Enum):
    REQUESTED = "REQUESTED"                    # in horizon, decision being made
    DENIED = "DENIED"                          # could not safely act this step (not fatal)
    GRANTED_EXTENDING = "GRANTED_EXTENDING"     # ambulance already green, holding it open
    GRANTED_TRUNCATING = "GRANTED_TRUNCATING"   # a conflicting green is being shortened
    ACTIVE_GREEN = "ACTIVE_GREEN"               # ambulance confirmed green, no action needed
    RESTORED = "RESTORED"                       # ambulance has passed; cooldown running


@dataclass
class PriorityAction:
    """One audited TraCI call, so every decision this module makes is traceable."""
    time_s: float
    call: str
    reason: str


@dataclass
class PriorityRequest:
    request_id: str
    vehicle_id: str
    tls_id: str
    created_time_s: float
    eta_s: float
    current_phase_index: int
    current_phase_kind: str
    current_phase_state: str
    target_phase_index: Optional[int]
    state: PriorityState
    actions: List[PriorityAction] = field(default_factory=list)
    original_remaining_s: Optional[float] = None
    granted_time_s: Optional[float] = None
    restored_time_s: Optional[float] = None
    denial_reason: Optional[str] = None
    cooldown_until_s: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d


class PriorityManager:
    """One instance per simulation. Holds at most one active PriorityRequest
    per traffic light, so the same signal is never double-granted.

    Constructed as PriorityManager(monitor) -- it reuses the monitor's own
    live TraCI connection and its cached program-logic lookup rather than
    opening a second path to SUMO.
    """

    def __init__(self, monitor: IntersectionMonitor):
        self.monitor = monitor
        self._requests: Dict[str, PriorityRequest] = {}  # key: tls_id

    @property
    def _tc(self):
        return self.monitor._tc  # same connection object the monitor itself uses

    # -- public, called once per sim step for the ambulance ------------------
    def update(self, vehicle_id: str) -> Dict[str, Any]:
        return guarded(lambda: self._update(vehicle_id))

    def _update(self, vehicle_id: str) -> Dict[str, Any]:
        try:
            status: IntersectionStatus = self.monitor.status(vehicle_id)
        except Exception as exc:
            # Before the ambulance's configured depart time, SUMO has not
            # inserted it yet, and IntersectionMonitor (correctly, by its
            # own design) raises VEHICLE_NOT_FOUND for "never seen at all"
            # rather than distinguishing "not yet departed" from "unknown
            # id." For this per-step orchestration call that distinction
            # does not matter: there is nothing to request yet either way,
            # so this is a normal state, not a failure of this function.
            # Any other error (CONNECTION_LOST, NOT_CONNECTED, ...) still
            # propagates to guarded() as a real failure.
            if getattr(exc, "code", None) == "VEHICLE_NOT_FOUND":
                return {
                    "vehicle_id": vehicle_id,
                    "sim_time_s": self._tc.simulation.getTime(),
                    "active_requests": [r.to_dict() for r in self._requests.values()],
                }
            raise
        touched_tls = set()

        if status.next_intersection is not None:
            ui = status.next_intersection
            if ui.signal is not None and ui.distance_m <= PRIORITY_REQUEST_HORIZON_M:
                touched_tls.add(ui.tls_id)
                self._handle_intersection(vehicle_id, ui)

        # Restore any TLS we were managing that the ambulance has now left
        # behind (no longer current, no longer next, but still held).
        still_relevant = touched_tls | (
            {status.current_intersection} if status.current_intersection else set()
        )
        for tls_id, req in list(self._requests.items()):
            if tls_id in still_relevant:
                continue
            if req.state in (
                PriorityState.GRANTED_EXTENDING,
                PriorityState.GRANTED_TRUNCATING,
                PriorityState.ACTIVE_GREEN,
            ):
                self._restore(req, reason="ambulance has passed this intersection")

        return {
            "vehicle_id": vehicle_id,
            "sim_time_s": status.sim_time_s,
            "active_requests": [r.to_dict() for r in self._requests.values()],
        }

    # -- items 1-2: identify next intersection + its current phase -----------
    def _handle_intersection(self, vehicle_id: str, ui: UpcomingIntersection) -> None:
        sig: SignalSnapshot = ui.signal
        req = self._requests.get(ui.tls_id)
        now = self._tc.simulation.getTime()

        if req is not None and req.state == PriorityState.RESTORED:
            if req.cooldown_until_s and now < req.cooldown_until_s:
                return  # still cooling down, do nothing
            del self._requests[ui.tls_id]
            req = None

        if req is None:
            req = PriorityRequest(
                request_id=str(uuid.uuid4()),
                vehicle_id=vehicle_id,
                tls_id=ui.tls_id,
                created_time_s=now,
                eta_s=ui.eta_s,
                current_phase_index=sig.phase_index,
                current_phase_kind=sig.phase_kind,
                current_phase_state=sig.phase_state,
                target_phase_index=None,
                state=PriorityState.REQUESTED,
            )
            self._requests[ui.tls_id] = req
        else:
            req.eta_s = ui.eta_s
            req.current_phase_index = sig.phase_index
            req.current_phase_kind = sig.phase_kind
            req.current_phase_state = sig.phase_state

        # items 3-4 (required phase + ETA) come straight from the monitor's
        # SignalSnapshot/UpcomingIntersection. items 5-8: decide + safely act.
        self._decide_and_act(req, sig)

    # -- items 5-8: safe decision + action -------------------------------------
    def _decide_and_act(self, req: PriorityRequest, sig: SignalSnapshot) -> None:
        try:
            logic = self.monitor._logic(req.tls_id)
        except traci.exceptions.TraCIException as exc:
            req.state = PriorityState.DENIED
            req.denial_reason = f"NO_PROGRAM_LOGIC: {exc}"
            return

        if sig.ambulance_signal == "GREEN":
            # Already safe and moving. Only act if this green might end
            # before the ambulance clears the junction.
            phase = logic.phases[sig.phase_index]
            margin = sig.phase_remaining_s - (req.eta_s + PRIORITY_ETA_BUFFER_S)
            if margin < 0 and phase.maxDur > sig.phase_remaining_s:
                new_remaining = min(
                    phase.maxDur,
                    req.eta_s + PRIORITY_ETA_BUFFER_S,
                    sig.phase_remaining_s + PRIORITY_MAX_GREEN_EXTENSION_S,
                )
                self._tc.trafficlight.setPhaseDuration(req.tls_id, new_remaining)
                req.state = PriorityState.GRANTED_EXTENDING
                req.granted_time_s = self._tc.simulation.getTime()
                req.actions.append(PriorityAction(
                    time_s=req.granted_time_s,
                    call=f"setPhaseDuration({req.tls_id}, {new_remaining:.1f})",
                    reason="extend ambulance's own green so it does not turn before ETA",
                ))
            else:
                req.state = PriorityState.ACTIVE_GREEN
            return

        # Ambulance signal is RED/RED_YELLOW: only act on a phase that is
        # *currently a conflicting GREEN*. Never touch CLEARANCE (yellow
        # present) or ALL_RED -- those always run their full programmed
        # duration, untouched, whatever PRIORITY_RESPECT_MIN_DUR is set to.
        if sig.phase_kind != "GREEN":
            req.state = PriorityState.REQUESTED
            return

        target_idx = self._pick_target_phase(sig)
        if target_idx is None:
            req.state = PriorityState.DENIED
            req.denial_reason = "NO_GREEN_PHASE_SERVES_AMBULANCE_LINK"
            return
        req.target_phase_index = target_idx

        phase = logic.phases[sig.phase_index]
        if PRIORITY_RESPECT_MIN_DUR:
            floor = phase.minDur if phase.minDur > 0 else PRIORITY_MIN_GREEN_FLOOR_S
        else:
            floor = PRIORITY_MIN_GREEN_FLOOR_S
        new_remaining = min(sig.phase_remaining_s, floor)
        if new_remaining >= sig.phase_remaining_s:
            # Nothing safe/allowed to shorten (e.g. a fixed-time program
            # with PRIORITY_RESPECT_MIN_DUR=True and minDur == duration).
            req.state = PriorityState.REQUESTED
            req.denial_reason = "AT_OR_BELOW_ALLOWED_FLOOR"
            return

        req.original_remaining_s = sig.phase_remaining_s
        self._tc.trafficlight.setPhaseDuration(req.tls_id, new_remaining)
        req.state = PriorityState.GRANTED_TRUNCATING
        req.granted_time_s = self._tc.simulation.getTime()
        req.actions.append(PriorityAction(
            time_s=req.granted_time_s,
            call=f"setPhaseDuration({req.tls_id}, {new_remaining:.1f})",
            reason=(
                f"truncate conflicting green to {new_remaining:.1f}s "
                f"(floor={floor:.1f}s) so the program can advance through "
                f"its own yellow/all-red into phase {target_idx} for the ambulance"
            ),
        ))

    # -- item 9: ambulance movement through the intersection -------------------
    # Already tracked by IntersectionMonitor (status.current_intersection /
    # status.crossed); _update() reads status.current_intersection every step
    # to know when to restore.

    # -- item 10: restore normal operation ---------------------------------------
    def _restore(self, req: PriorityRequest, reason: str) -> None:
        now = self._tc.simulation.getTime()
        try:
            logic = self.monitor._logic(req.tls_id)
            cur_idx = self._tc.trafficlight.getPhase(req.tls_id)
            if cur_idx == req.current_phase_index:
                default_dur = logic.phases[cur_idx].duration
                remaining = self._tc.trafficlight.getNextSwitch(req.tls_id) - now
                if default_dur > remaining:
                    self._tc.trafficlight.setPhaseDuration(req.tls_id, default_dur)
                    req.actions.append(PriorityAction(
                        time_s=now,
                        call=f"setPhaseDuration({req.tls_id}, {default_dur:.1f})",
                        reason="restore this phase's normal programmed duration",
                    ))
        except traci.exceptions.TraCIException:
            pass  # phase has already moved on naturally; nothing to restore

        req.state = PriorityState.RESTORED
        req.restored_time_s = now
        req.cooldown_until_s = now + PRIORITY_COOLDOWN_S
        req.actions.append(PriorityAction(
            time_s=now, call="restore_normal_operation", reason=reason
        ))

    # -- helpers -------------------------------------------------------------------
    def _pick_target_phase(self, sig: SignalSnapshot) -> Optional[int]:
        if not sig.green_phase_indices:
            return None
        n = sig.num_phases
        for step in range(1, n + 1):
            idx = (sig.phase_index + step) % n
            if idx in sig.green_phase_indices:
                return idx
        return None


# ---------------------------------------------------------------------------
# Public functions (project spec section 7)
# ---------------------------------------------------------------------------
def generate_signal_priority_request(manager: PriorityManager, vehicle_id: str) -> Dict[str, Any]:
    """Advance the priority state machine one step for this vehicle.

    Returns the standard {success, data, error, timestamp} envelope.
    data = {vehicle_id, sim_time_s, active_requests: [PriorityRequest.to_dict(), ...]}.
    Call every simulation step -- the same call both creates new requests as
    the ambulance enters a horizon and restores signals it has passed.
    """
    return manager.update(vehicle_id)


def restore_normal_operation(manager: PriorityManager, tls_id: str) -> Dict[str, Any]:
    """Force-restore a single TLS (manual testing / shutdown path)."""
    def _do():
        req = manager._requests.get(tls_id)
        if req is None:
            raise PriorityError("TLS_NOT_MANAGED", f"No active priority request for {tls_id}")
        manager._restore(req, reason="manual restore_normal_operation call")
        return req.to_dict()
    return guarded(_do)