import { useCallback, useState } from "react";
import { ApiError } from "../lib/api";
import { startSimulation, stepSimulation, stopSimulation } from "../lib/services";
import { useAppData } from "../context/AppDataContext";

type Action = "start" | "stop" | "step";

interface UseSimulationResult {
  pending: Action | null;
  error: ApiError | null;
  notice: string | null;
  start: () => Promise<void>;
  stop: () => Promise<void>;
  step: (n?: number) => Promise<void>;
  clearMessages: () => void;
  canStart: boolean;
  canStop: boolean;
  canStep: boolean;
}

/**
 * Simulation mutations with guard rails.
 *
 * * Only one request can be in flight at a time (`pending`).
 * * `start` is disabled while running, `stop`/`step` while stopped, which
 *   prevents the 409 the backend would otherwise return.
 * * Every error is normalised by `toApiError` and shown verbatim.
 * * After a successful mutation the shared data is refetched immediately
 *   instead of waiting for the next poll.
 */
export function useSimulation(): UseSimulationResult {
  const { status, refresh } = useAppData();
  const [pending, setPending] = useState<Action | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const running = Boolean(status?.running);

  const run = useCallback(
    async (action: Action, fn: () => Promise<string>) => {
      if (pending) return;
      setPending(action);
      setError(null);
      setNotice(null);
      try {
        const message = await fn();
        setNotice(message);
        await refresh();
      } catch (caught) {
        setError(caught instanceof ApiError ? caught : new ApiError(String(caught)));
      } finally {
        setPending(null);
      }
    },
    [pending, refresh]
  );

  const start = useCallback(async () => {
    if (running) return;
    await run("start", async () => {
      const result = await startSimulation();
      return `Simulation started${result.traffic_lights != null ? ` · ${result.traffic_lights} traffic lights` : ""}${
        result.parking_areas != null ? ` · ${result.parking_areas} parking areas` : ""
      }.`;
    });
  }, [running, run]);

  const stop = useCallback(async () => {
    if (!running) return;
    await run("stop", async () => {
      await stopSimulation();
      return "Simulation stopped.";
    });
  }, [running, run]);

  const step = useCallback(
    async (n = 1) => {
      if (!running) return;
      await run("step", async () => {
        const result = await stepSimulation(n);
        const count = result.decisions.length;
        return `Advanced ${n} step${n === 1 ? "" : "s"} to t=${result.simulation_time_s.toFixed(0)}s · ${count} decision${
          count === 1 ? "" : "s"
        }.`;
      });
    },
    [running, run]
  );

  const clearMessages = useCallback(() => {
    setError(null);
    setNotice(null);
  }, []);

  return {
    pending,
    error,
    notice,
    start,
    stop,
    step,
    clearMessages,
    canStart: !running && pending === null,
    canStop: running && pending === null,
    canStep: running && pending === null,
  };
}
