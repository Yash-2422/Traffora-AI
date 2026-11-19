"""Shared helper that turns any failure inside a public GreenCorridor function into the JSON envelope."""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict

from .schemas import fail, ok

logger = logging.getLogger(__name__)


def guarded(fn: Callable[[], Any]) -> Dict[str, Any]:
    """Run fn() and return ok(result) or fail(code, message). Never raises.

    Any exception carrying ``code`` and ``message`` attributes (DetectorError, RouteError,
    SumoConnectionError, ...) keeps its own code. A dead SUMO socket becomes CONNECTION_LOST.
    """
    try:
        return ok(fn())
    except Exception as exc:
        code, message = getattr(exc, "code", None), getattr(exc, "message", None)
        if isinstance(code, str) and isinstance(message, str):
            return fail(code, message)
        name = type(exc).__name__
        if name == "FatalTraCIError" or isinstance(exc, (OSError, EOFError)):  # incl. broken socket
            return fail("CONNECTION_LOST", f"SUMO connection lost: {exc}")
        if name == "TraCIException":
            return fail("TRACI_ERROR", str(exc))
        logger.exception("Unexpected error")
        return fail("UNEXPECTED_ERROR", f"{name}: {exc}")