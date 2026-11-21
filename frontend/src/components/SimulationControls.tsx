import { Play, Square, StepForward, Loader2, CheckCircle2, XCircle } from "lucide-react";
import { useSimulation } from "../hooks/useSimulation";
import { clock } from "../lib/format";
import { useAppData } from "../context/AppDataContext";

/**
 * Simulation control bar.
 *
 * Start / Stop / Step map 1:1 onto POST /simulation/start, /simulation/stop and
 * /simulation/step. Buttons are disabled in states the backend would reject
 * (start while running, step while stopped) so no invalid request is sent.
 */
export function SimulationControls({ compact = false }: { compact?: boolean }) {
  const { status } = useAppData();
  const { start, stop, step, pending, error, notice, canStart, canStop, canStep } = useSimulation();

  const running = Boolean(status?.running);
  const simTime = status?.simulation_time_s;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="chip border-white/10 bg-white/5 text-slate-300">
          <span
            className={`h-1.5 w-1.5 rounded-full ${
              running ? "animate-pulse bg-emerald-400" : "bg-slate-500"
            }`}
            aria-hidden
          />
          {running ? "Running" : "Stopped"}
        </span>

        <span className="chip border-white/10 bg-white/5 font-mono text-slate-300">
          t = {clock(simTime)}
        </span>

        {!compact && (
          <span className="chip border-white/10 bg-white/5 text-slate-400">
            {status?.pending_events ?? 0} pending · {status?.decisions ?? 0} decisions
          </span>
        )}

        <div className="ml-auto flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => void start()}
            disabled={!canStart}
            className="btn-primary px-3 py-1.5 text-xs"
            title={running ? "The simulation is already running" : "Start SUMO and the agents"}
          >
            {pending === "start" ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            ) : (
              <Play className="h-3.5 w-3.5" aria-hidden />
            )}
            Start
          </button>

          <button
            type="button"
            onClick={() => void step(1)}
            disabled={!canStep}
            className="btn-ghost px-3 py-1.5 text-xs"
            title={running ? "Advance one simulation step" : "Start the simulation first"}
          >
            {pending === "step" ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            ) : (
              <StepForward className="h-3.5 w-3.5" aria-hidden />
            )}
            Step
          </button>

          <button
            type="button"
            onClick={() => void stop()}
            disabled={!canStop}
            className="btn-danger px-3 py-1.5 text-xs"
            title={running ? "Stop the simulation" : "The simulation is not running"}
          >
            {pending === "stop" ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            ) : (
              <Square className="h-3.5 w-3.5" aria-hidden />
            )}
            Stop
          </button>
        </div>
      </div>

      {error && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-lg border border-rose-500/25 bg-rose-500/[0.07] px-3 py-2"
        >
          <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-rose-400" aria-hidden />
          <p className="text-xs text-rose-200">
            {error.message}
            {error.code ? <span className="ml-1 text-rose-300/60">({error.code})</span> : null}
          </p>
        </div>
      )}

      {notice && !error && (
        <div className="flex items-start gap-2 rounded-lg border border-emerald-500/20 bg-emerald-500/[0.06] px-3 py-2">
          <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-400" aria-hidden />
          <p className="text-xs text-emerald-200">{notice}</p>
        </div>
      )}
    </div>
  );
}
