import { Activity, Clock, Gauge, Route, Truck } from "lucide-react";
import { useAppData } from "../context/AppDataContext";
import { Panel } from "../components/Panel";
import { StatCard } from "../components/StatCard";
import { StatusPill, levelTone } from "../components/StatusPill";
import { AsyncSection, EmptyBlock } from "../components/AsyncState";
import { TrendChart } from "../components/charts/TrendChart";
import { useMetricHistory } from "../hooks/useMetricHistory";
import { EMPTY, clock, int, isNumber, num, titleCase } from "../lib/format";

export function TrafficMind() {
  const { traffic, cityState, decisions, loading, error, refresh, metrics } = useAppData();
  const history = useMetricHistory();

  const state = traffic?.traffic ?? cityState?.traffic ?? null;
  const trafficDecisions = decisions.filter(
    (decision) =>
      decision.decision_type === "TRAFFIC_OPTIMIZATION" || decision.source_agent === "traffic_mind"
  );

  const junctionLevels = state?.junction_levels ?? {};
  const levelCounts = Object.values(junctionLevels).reduce<Record<string, number>>((acc, level) => {
    const key = level.toUpperCase();
    acc[key] = (acc[key] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="space-y-4">
      <AsyncSection loading={loading && !traffic} error={error && !traffic ? error : null} onRetry={refresh}>
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
            <StatCard
              label="Congestion level"
              value={state ? titleCase(state.congestion_level) : EMPTY}
              icon={Activity}
              tone={
                state?.congestion_level === "HIGH"
                  ? "bad"
                  : state?.congestion_level === "MEDIUM"
                    ? "warn"
                    : "good"
              }
              unavailable={!state}
              hint="Overall, from TrafficMind"
            />
            <StatCard
              label="Vehicles in network"
              value={int(state?.vehicle_count)}
              icon={Truck}
              unavailable={!isNumber(state?.vehicle_count)}
            />
            <StatCard
              label="Mean speed"
              value={num(state?.average_speed_mps)}
              unit="m/s"
              icon={Gauge}
              unavailable={!isNumber(state?.average_speed_mps)}
            />
            <StatCard
              label="Avg waiting time"
              value={num(traffic?.average_waiting_time_s ?? state?.average_waiting_time_s)}
              unit="s"
              icon={Clock}
              unavailable={!isNumber(traffic?.average_waiting_time_s ?? state?.average_waiting_time_s)}
            />
            <StatCard
              label="Avg queue length"
              value={num(traffic?.average_queue_length ?? state?.average_queue_length_m)}
              unit="m"
              icon={Route}
              unavailable={!isNumber(traffic?.average_queue_length ?? state?.average_queue_length_m)}
            />
            <StatCard
              label="Congested junctions"
              value={int(state?.congested_junctions.length)}
              icon={Activity}
              tone={state && state.congested_junctions.length > 0 ? "warn" : "good"}
              unavailable={!state}
              hint="MEDIUM or HIGH"
            />
          </div>

          <div className="grid gap-4 xl:grid-cols-3">
            <Panel
              className="xl:col-span-2"
              title="Traffic trend (this session)"
              subtitle="Accumulated from measured /metrics samples"
            >
              <TrendChart
                data={history}
                height={260}
                series={[
                  { key: "waiting", label: "Avg waiting", color: "#22d3ee", unit: " s" },
                  { key: "queue", label: "Avg queue", color: "#f59e0b", unit: " m" },
                  { key: "vehicles", label: "Vehicles", color: "#a78bfa", unit: "" },
                ]}
              />
              <p className="mt-2 text-[10px] leading-relaxed text-slate-500">
                The API returns current values only, so this chart is built from the samples this
                browser actually received ({history.length} points). Nothing is interpolated.
              </p>
            </Panel>

            <div className="space-y-4">
              <Panel title="Junction level distribution" subtitle="city-state.traffic.junction_levels">
                {Object.keys(levelCounts).length === 0 ? (
                  <EmptyBlock
                    title="No junction data"
                    description="TrafficMind publishes junction levels during each control cycle."
                  />
                ) : (
                  <ul className="space-y-2">
                    {["HIGH", "MEDIUM", "LOW"].map((level) => (
                      <li key={level} className="flex items-center justify-between gap-2">
                        <StatusPill tone={levelTone(level)}>{titleCase(level)}</StatusPill>
                        <div className="flex flex-1 items-center gap-2">
                          <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-white/5">
                            <div
                              className={`h-full rounded-full ${
                                level === "HIGH"
                                  ? "bg-rose-500"
                                  : level === "MEDIUM"
                                    ? "bg-amber-500"
                                    : "bg-emerald-500"
                              }`}
                              style={{
                                width: `${
                                  (levelCounts[level] / Math.max(1, Object.keys(junctionLevels).length)) * 100
                                }%`,
                              }}
                            />
                          </div>
                          <span className="w-6 text-right font-mono text-xs text-slate-300">
                            {levelCounts[level] ?? 0}
                          </span>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </Panel>

              <Panel title="Traffic metrics" subtitle="GET /analytics/traffic">
                <dl className="space-y-2 text-xs">
                  <Row label="Simulation time" value={clock(traffic?.simulation_time_s)} mono />
                  <Row label="Vehicles departed" value={int(traffic?.vehicles_departed)} mono />
                  <Row label="Vehicles arrived" value={int(traffic?.vehicles_arrived)} mono />
                  <Row label="Avg travel time" value={`${num(traffic?.average_travel_time_s)} s`} mono />
                  <Row label="Processed events" value={int(traffic?.congestion_events)} mono />
                  <Row label="Signal decisions" value={int(trafficDecisions.length)} mono />
                  <Row
                    label="Accepted / rejected"
                    value={`${int(metrics?.accepted_decisions)} / ${int(metrics?.rejected_decisions)}`}
                    mono
                  />
                </dl>
              </Panel>
            </div>
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <Panel title="Junction status" subtitle="Live congestion level per junction">
              {Object.keys(junctionLevels).length === 0 ? (
                <EmptyBlock title="No junctions reported" />
              ) : (
                <div className="max-h-80 overflow-y-auto">
                  <table className="w-full border-collapse text-left">
                    <thead className="table-head">
                      <tr>
                        <th className="px-3 py-2">Junction</th>
                        <th className="px-3 py-2">Level</th>
                        <th className="px-3 py-2 text-right">In congested list</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(junctionLevels)
                        .sort(([a], [b]) => a.localeCompare(b))
                        .map(([junction, level]) => (
                          <tr key={junction}>
                            <td className="table-cell font-mono">{junction}</td>
                            <td className="table-cell">
                              <StatusPill tone={levelTone(level)}>{titleCase(level)}</StatusPill>
                            </td>
                            <td className="table-cell text-right text-xs text-slate-500">
                              {state?.congested_junctions.includes(junction) ? "yes" : "no"}
                            </td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Panel>

            <Panel
              title="Signal decisions"
              subtitle="TrafficMind-originated decisions"
              actions={<StatusPill tone="info">{trafficDecisions.length}</StatusPill>}
            >
              {trafficDecisions.length === 0 ? (
                <EmptyBlock
                  title="No traffic decisions yet"
                  description="The decision engine only acts on HIGH congestion, or on an explicit green extension recommendation."
                />
              ) : (
                <div className="max-h-80 space-y-2 overflow-y-auto pr-1">
                  {trafficDecisions.slice(0, 30).map((decision) => (
                    <div
                      key={decision.decision_id}
                      className="rounded-lg border border-white/5 bg-white/[0.02] p-2.5"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <StatusPill tone={decision.applied ? "good" : "neutral"}>
                          {decision.applied ? "Applied" : "Not applied"}
                        </StatusPill>
                        <span className="font-mono text-[10px] text-slate-500">
                          t={clock(decision.timestamp_s)}
                        </span>
                        {decision.validation && (
                          <StatusPill tone={decision.validation.valid ? "good" : "bad"}>
                            {titleCase(decision.validation.status)}
                          </StatusPill>
                        )}
                      </div>
                      <p className="mt-1 text-xs leading-snug text-slate-300">
                        {decision.explanation || "No explanation recorded."}
                      </p>
                      {decision.signal_actions.length > 0 && (
                        <ul className="mt-1.5 space-y-0.5">
                          {decision.signal_actions.map((action) => (
                            <li
                              key={action.action_id}
                              className="font-mono text-[10px] text-slate-400"
                            >
                              {action.tls_id} · {action.action_type} · {action.duration_s.toFixed(0)}s
                              {action.phase_index != null ? ` · phase ${action.phase_index}` : ""}
                            </li>
                          ))}
                        </ul>
                      )}
                      {decision.rejected_reason && (
                        <p className="mt-1 text-[10px] text-rose-300">
                          {decision.rejected_reason}
                        </p>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </Panel>
          </div>
        </div>
      </AsyncSection>
    </div>
  );
}

function Row({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <dt className="text-slate-500">{label}</dt>
      <dd className={`text-slate-300 ${mono ? "font-mono" : ""}`}>{value}</dd>
    </div>
  );
}
