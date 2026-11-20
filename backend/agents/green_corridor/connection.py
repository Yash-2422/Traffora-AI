"""SUMO/TraCI connection management.

All TraCI start/stop/error handling lives here so the rest of the agent never
touches process management directly. Errors are raised as SumoConnectionError
with a stable machine-readable ``code`` that the agent maps into its JSON envelope.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import List, Optional

from . import config

logger = logging.getLogger(__name__)


class SumoConnectionError(Exception):
    """Raised for any SUMO/TraCI setup or runtime failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


def _add_sumo_tools_to_path(sumo_home: str) -> None:
    """Make SUMO's bundled `traci`/`sumolib` importable if pip versions are absent."""
    tools = os.path.join(sumo_home, "tools")
    if os.path.isdir(tools) and tools not in sys.path:
        sys.path.append(tools)


def find_sumo_home() -> Optional[str]:
    """Return SUMO_HOME from the environment, or from the pip `eclipse-sumo` package."""
    env = os.environ.get("SUMO_HOME")
    if env and os.path.isdir(env):
        return env
    try:
        import sumo  # provided by the eclipse-sumo wheel
        return sumo.SUMO_HOME
    except Exception:
        return None


def find_binary(name: str) -> Optional[str]:
    """Locate a SUMO executable (sumo, sumo-gui, netgenerate, ...)."""
    home = find_sumo_home()
    if home:
        for candidate in (Path(home) / "bin" / name, Path(home) / "bin" / f"{name}.exe"):
            if candidate.is_file():
                return str(candidate)
    return shutil.which(name)


class SumoConnection:
    """Starts a SUMO process, exposes its TraCI connection, and cleans up reliably.

    Usage:
        with SumoConnection() as sc:
            sc.start("scenario.sumocfg")
            sc.step()
            print(sc.traci.simulation.getTime())
    """

    def __init__(self, gui: bool = False, seed: int = config.DEFAULT_SEED):
        self.gui = gui
        self.seed = seed
        self._label = f"gc_{uuid.uuid4().hex[:8]}"
        self._conn = None

    # -- lifecycle ----------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._conn is not None

    @property
    def traci(self):
        """The live TraCI connection object (raises if not started)."""
        if self._conn is None:
            raise SumoConnectionError("NOT_CONNECTED", "SUMO is not running; call start() first.")
        return self._conn

    def start(self, sumocfg: str | Path, extra_args: Optional[List[str]] = None) -> None:
        if self._conn is not None:
            raise SumoConnectionError("ALREADY_CONNECTED", "A SUMO session is already active.")

        cfg = Path(sumocfg)
        if not cfg.is_file():
            raise SumoConnectionError("CONFIG_NOT_FOUND", f"SUMO config not found: {cfg}")

        home = find_sumo_home()
        if home:
            _add_sumo_tools_to_path(home)
        try:
            import traci  # noqa: WPS433 (deferred so a missing install gives a clean error)
        except ImportError as exc:
            raise SumoConnectionError(
                "TRACI_NOT_INSTALLED", "Python package 'traci' not found. Run: pip install traci sumolib"
            ) from exc

        binary = find_binary(config.SUMO_GUI_BINARY if self.gui else config.SUMO_BINARY)
        if binary is None:
            raise SumoConnectionError(
                "SUMO_NOT_FOUND",
                "SUMO executable not found. Install SUMO and set SUMO_HOME, or run: pip install eclipse-sumo",
            )

        cmd = [
            binary, "-c", str(cfg),
            "--step-length", str(config.SIM_STEP_LENGTH_S),
            "--seed", str(self.seed),
            "--no-warnings", "true",
            "--no-step-log", "true",
        ]
        if self.gui:
            cmd += ["--start", "true", "--quit-on-end", "true"]
        if extra_args:
            cmd += extra_args

        logger.info("Starting SUMO: %s", " ".join(cmd))
        try:
            traci.start(cmd, label=self._label)
            self._conn = traci.getConnection(self._label)
        except Exception as exc:  # traci raises several types (FatalTraCIError, OSError, ...)
            self._conn = None
            raise SumoConnectionError("START_FAILED", f"Could not start SUMO/TraCI: {exc}") from exc

    def step(self) -> float:
        """Advance one simulation step; returns the new simulation time (s)."""
        conn = self.traci
        try:
            conn.simulationStep()
            return conn.simulation.getTime()
        except Exception as exc:
            self.close()
            raise SumoConnectionError("CONNECTION_LOST", f"SUMO connection lost during step: {exc}") from exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception as exc:  # already dead is fine
                logger.debug("Ignoring error on close: %s", exc)
            finally:
                self._conn = None

    def __enter__(self) -> "SumoConnection":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()