"""The only module in TrafficMind that talks to SUMO / TraCI.

Everything else (monitor, detector, predictor, optimizer) works on plain data,
so it can be unit-tested without SUMO installed.

Typical use::

    from backend.agents.traffic_mind.simulation_interface import SimulationInterface

    with SimulationInterface() as sim:
        sim.step(60)
        print(sim.vehicle_count())
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

try:  # traci is imported defensively so the package can be imported without it
    import traci
    from traci.exceptions import FatalTraCIError, TraCIException
except ImportError:  # pragma: no cover - exercised only when traci is missing
    traci = None  # type: ignore[assignment]

    class FatalTraCIError(Exception):  # type: ignore[no-redef]
        pass

    class TraCIException(Exception):  # type: ignore[no-redef]
        pass


from .config import SimulationConfig
from .exceptions import (
    ScenarioNotFoundError,
    SimulationConnectionError,
    SimulationNotRunningError,
    SumoNotFoundError,
)
from .schemas import (
    EdgeInfo,
    JunctionInfo,
    NetworkTopology,
    RawEdgeObservation,
    RawObservation,
    RawSignalObservation,
)

logger = logging.getLogger(__name__)


def _pip_sumo_home() -> Optional[Path]:
    """SUMO_HOME of the ``eclipse-sumo`` wheel, if it is installed."""
    try:
        import sumo  # type: ignore

        return Path(sumo.SUMO_HOME)
    except Exception:  # noqa: BLE001 - optional fallback
        return None


def find_sumo_binary(gui: bool = False) -> Optional[str]:
    """Locate ``sumo`` / ``sumo-gui``.

    Search order: ``%SUMO_HOME%\\bin``, ``PATH``, then the eclipse-sumo wheel.
    Returns ``None`` when nothing is found.
    """
    name = "sumo-gui" if gui else "sumo"
    exe = name + (".exe" if os.name == "nt" else "")

    sumo_home = os.environ.get("SUMO_HOME")
    if sumo_home:
        candidate = Path(sumo_home) / "bin" / exe
        if candidate.is_file():
            return str(candidate)

    on_path = shutil.which(name)
    if on_path:
        return on_path

    pip_home = _pip_sumo_home()
    if pip_home is not None:
        candidate = pip_home / "bin" / exe
        if candidate.is_file():
            return str(candidate)
    return None


class SimulationInterface:
    """Controlled access to one SUMO simulation through TraCI.

    The connection uses its own label, so it never clashes with the global
    TraCI connection (useful when an orchestrator runs several components).
    """

    def __init__(self, config: Optional[SimulationConfig] = None) -> None:
        self.config = config or SimulationConfig()
        self._conn: Any = None
        self._ended = False
        self._topology: Optional[NetworkTopology] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    @property
    def is_running(self) -> bool:
        return self._conn is not None

    @property
    def has_ended(self) -> bool:
        """True once the configured end time has been reached."""
        return self._ended

    def start(self) -> None:
        """Start SUMO and connect TraCI.

        Raises:
            ScenarioNotFoundError: the .sumocfg file does not exist.
            SumoNotFoundError: no SUMO executable could be found.
            SimulationConnectionError: SUMO failed to start or connect.
        """
        if self.is_running:
            logger.warning("Simulation already running; start() ignored.")
            return
        if traci is None:
            raise SimulationConnectionError(
                "The 'traci' package is not installed. Run: pip install traci sumolib"
            )

        cfg = self.config.resolved_sumocfg()
        if not cfg.is_file():
            raise ScenarioNotFoundError(
                f"Scenario file not found: {cfg}. Build it with "
                "'python simulation/traffic_mind/build_scenario.py'."
            )

        binary = find_sumo_binary(self.config.gui)
        if binary is None:
            raise SumoNotFoundError(
                "SUMO executable not found. Install SUMO, set SUMO_HOME, and "
                "restart your terminal / VS Code."
            )

        cmd: List[str] = [
            binary,
            "-c", str(cfg),
            "--seed", str(self.config.seed),
            "--scale", str(self.config.demand_scale),
            "--step-length", str(self.config.step_length),
            "--end", str(self.config.end_time),
            "--no-step-log",
            "--no-warnings",
            "--xml-validation", "never",
        ]
        if self.config.gui:
            cmd += ["--start", "--quit-on-end", "--delay", str(self.config.gui_delay_ms)]

        self._release_label()
        logger.info("Starting SUMO: %s", " ".join(cmd))
        try:
            traci.start(
                cmd,
                label=self.config.label,
                numRetries=self.config.connect_retries,
            )
            self._conn = traci.getConnection(self.config.label)
        except (FatalTraCIError, TraCIException, OSError) as exc:
            self._conn = None
            raise SimulationConnectionError(f"Could not start/connect to SUMO: {exc}") from exc

        self._ended = False
        logger.info("Connected to SUMO (scenario: %s)", cfg.name)

    def close(self) -> None:
        """Close the connection. Safe to call more than once."""
        if self._conn is None:
            return
        try:
            self._conn.close()
        except (FatalTraCIError, TraCIException, OSError) as exc:
            logger.warning("Error while closing TraCI connection: %s", exc)
        finally:
            self._conn = None
            self._topology = None
            logger.info("Simulation closed.")

    def _release_label(self) -> None:
        """Close a stale connection that still holds our label."""
        try:
            stale = traci.getConnection(self.config.label)
        except (KeyError, TraCIException, FatalTraCIError):
            return
        try:
            stale.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Stale connection close failed: %s", exc)

    def __enter__(self) -> "SimulationInterface":
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Stepping
    # ------------------------------------------------------------------ #
    def _require_running(self) -> Any:
        if self._conn is None:
            raise SimulationNotRunningError("Simulation is not running. Call start() first.")
        return self._conn

    def step(self, n: int = 1) -> float:
        """Advance the simulation ``n`` steps and return the new time (seconds).

        Raises:
            SimulationNotRunningError: not started, or the end time was reached.
            SimulationConnectionError: SUMO closed the connection unexpectedly.
        """
        conn = self._require_running()
        if self._ended:
            raise SimulationNotRunningError("Simulation has reached its end time.")
        if n < 1:
            raise ValueError("n must be >= 1")

        try:
            for _ in range(n):
                conn.simulationStep()
                if conn.simulation.getTime() >= self.config.end_time:
                    self._ended = True
                    break
            return float(conn.simulation.getTime())
        except (FatalTraCIError, TraCIException) as exc:
            self._conn = None
            raise SimulationConnectionError(f"Lost connection to SUMO: {exc}") from exc

    @property
    def current_time(self) -> float:
        conn = self._require_running()
        return float(conn.simulation.getTime())

    # ------------------------------------------------------------------ #
    # Basic read access (the monitor adds richer metrics in Step 4)
    # ------------------------------------------------------------------ #
    def _edge_ids(self) -> List[str]:
        conn = self._require_running()
        return [e for e in conn.edge.getIDList() if not e.startswith(":")]

    def vehicle_count(self) -> int:
        """Vehicles currently in the network."""
        return int(self._require_running().vehicle.getIDCount())

    def halting_vehicle_count(self) -> int:
        """Vehicles currently stopped (speed < 0.1 m/s) on all roads."""
        conn = self._require_running()
        return int(sum(conn.edge.getLastStepHaltingNumber(e) for e in self._edge_ids()))

    def network_summary(self) -> Dict[str, Any]:
        """JSON-serializable description of the network and its signals."""
        conn = self._require_running()
        lights = []
        for tls_id in sorted(conn.trafficlight.getIDList()):
            logic = conn.trafficlight.getAllProgramLogics(tls_id)[0]
            lights.append({
                "id": tls_id,
                "program": logic.programID,
                "controlled_lanes": len(set(conn.trafficlight.getControlledLanes(tls_id))),
                "phases": [
                    {"index": i, "duration": float(p.duration), "state": p.state}
                    for i, p in enumerate(logic.phases)
                ],
            })
        return {
            "edge_count": len(self._edge_ids()),
            "traffic_light_count": len(lights),
            "traffic_lights": lights,
        }

    # ------------------------------------------------------------------ #
    # Data for the traffic monitor
    # ------------------------------------------------------------------ #
    def read_topology(self) -> NetworkTopology:
        """Static road network: roads (with the signalised junction they lead
        into) and signalised junctions (with their incoming roads). Cached."""
        conn = self._require_running()
        if self._topology is not None:
            return self._topology

        try:
            junctions: List[JunctionInfo] = []
            junction_of_edge: Dict[str, str] = {}
            for tls_id in sorted(conn.trafficlight.getIDList()):
                links = conn.trafficlight.getControlledLinks(tls_id)
                # each link is (incoming lane, outgoing lane, via lane); lane id = "<edge>_<index>"
                incoming = sorted({
                    link[0].rsplit("_", 1)[0] for group in links for link in group
                })
                for edge_id in incoming:
                    junction_of_edge[edge_id] = tls_id
                logic = conn.trafficlight.getAllProgramLogics(tls_id)[0]
                junctions.append(JunctionInfo(
                    junction_id=tls_id,
                    incoming_edges=incoming,
                    phase_count=len(logic.phases),
                ))

            edges: List[EdgeInfo] = []
            for edge_id in self._edge_ids():
                first_lane = f"{edge_id}_0"
                edges.append(EdgeInfo(
                    edge_id=edge_id,
                    length_m=float(conn.lane.getLength(first_lane)),
                    lane_count=int(conn.edge.getLaneNumber(edge_id)),
                    speed_limit_mps=float(conn.lane.getMaxSpeed(first_lane)),
                    junction_id=junction_of_edge.get(edge_id),
                ))
            self._topology = NetworkTopology(edges=edges, junctions=junctions)
        except (FatalTraCIError, TraCIException) as exc:
            self._conn = None
            raise SimulationConnectionError(f"Lost connection to SUMO: {exc}") from exc
        return self._topology

    def read_observation(self) -> RawObservation:
        """Raw per-road and per-signal readings at the current simulation time."""
        conn = self._require_running()
        topology = self.read_topology()
        try:
            edges: List[RawEdgeObservation] = []
            for info in topology.edges:
                edge_id = info.edge_id
                vehicle_ids = list(conn.edge.getLastStepVehicleIDs(edge_id))
                halting = int(conn.edge.getLastStepHaltingNumber(edge_id))
                edges.append(RawEdgeObservation(
                    edge_id=edge_id,
                    vehicle_ids=vehicle_ids,
                    vehicle_count=len(vehicle_ids),
                    halting_count=halting,
                    # TraCI reports occupancy as a fraction (0..1); we expose percent.
                    occupancy_pct=min(
                        100.0, max(0.0, float(conn.edge.getLastStepOccupancy(edge_id)) * 100.0)
                    ),
                    mean_speed_mps=max(0.0, float(conn.edge.getLastStepMeanSpeed(edge_id))),
                    waiting_time_sum_s=max(0.0, float(conn.edge.getWaitingTime(edge_id))),
                    queue_length_m=self._queue_length_m(info) if vehicle_ids else 0.0,
                ))

            now = float(conn.simulation.getTime())
            signals = [
                RawSignalObservation(
                    junction_id=j.junction_id,
                    phase_index=int(conn.trafficlight.getPhase(j.junction_id)),
                    phase_remaining_s=max(
                        0.0, float(conn.trafficlight.getNextSwitch(j.junction_id)) - now
                    ),
                )
                for j in topology.junctions
            ]
            return RawObservation(
                time_s=now,
                total_vehicles=int(conn.vehicle.getIDCount()),
                edges=edges,
                signals=signals,
            )
        except (FatalTraCIError, TraCIException) as exc:
            self._conn = None
            raise SimulationConnectionError(f"Lost connection to SUMO: {exc}") from exc

    def _queue_length_m(self, info: EdgeInfo) -> float:
        """Length (m) of the queue, measured from the stop line back to its last car.

        Slow vehicles (below ``queue_speed_mps``) whose spacing is at most
        ``queue_gap_m`` form a chain. A chain counts as a queue when

        * its first car is within ``queue_head_tolerance_m`` of the stop line, or
        * it has at least ``queue_min_standing_vehicles`` cars.

        The second rule matters while a long queue discharges: the first cars have
        already pulled away (and move faster than ``queue_speed_mps``) while the
        rest still stand, so the standing chain does not start at the stop line.
        The road in between is occupied by the discharging cars, so the queue still
        extends from the stop line to the back of the standing chain.
        The longest queue over the road's lanes is reported.
        """
        conn = self._require_running()
        cfg = self.config
        longest = 0.0
        for lane_index in range(info.lane_count):
            lane_id = f"{info.edge_id}_{lane_index}"
            slow = sorted(
                (
                    info.length_m - conn.vehicle.getLanePosition(vid)   # distance to stop line
                    for vid in conn.lane.getLastStepVehicleIDs(lane_id)
                    if conn.vehicle.getSpeed(vid) < cfg.queue_speed_mps
                ),
            )  # nearest to the stop line first
            if not slow:
                continue
            chains = [[slow[0]]]
            for dist in slow[1:]:
                if dist - chains[-1][-1] <= cfg.queue_gap_m:
                    chains[-1].append(dist)
                else:
                    chains.append([dist])
            for chain in chains:
                near_stop_line = chain[0] <= cfg.queue_head_tolerance_m
                if near_stop_line or len(chain) >= cfg.queue_min_standing_vehicles:
                    length = chain[-1] + cfg.vehicle_length_m
                    longest = max(longest, min(length, info.length_m))
        return longest