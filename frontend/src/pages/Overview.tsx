import {
  Activity,
  Car,
  CheckCircle2,
  Clock,
  Gauge as GaugeIcon,
  Route,
  Siren,
  Timer,
  Truck,
  Users,
} from "lucide-react";
import { Link } from "react-router-dom";
import { useAppData } from "../context/AppDataContext";
import { AsyncSection } from "../components/AsyncState";
import { Panel } from "../components/Panel";
import { StatCard } from "../components/StatCard";
import { StatusPill, healthTone, levelTone } from "../components/StatusPill";
import { AGENT_DESCRIPTIONS, AGENT_LABELS } from "../lib/config";
import { EMPTY, agentLabel, clock, duration, int, isNumber, num, pct, titleCase } from "../lib/format";

export function Overview() {
  const { status, metrics, agents, cityState, loading, error, refresh } = useAppData();

  const running = Boolean(status?.running);
  const traffic = cityState?.traffic ?? null;
  const parking = cityState?.parking ?? null;
  const latest = cityState?.latest_decision ?? null;
  const emergencies = cityState?.emergency_vehicles ?? [];

  const measured = (value: number | null | undefined) => !isNumber(value);

  return (
    <div className="space-y-4">
      <AsyncSection loading={loading && !status} error={error && !status ? error : null} onRetry={refresh}>
        <div className="space-y-4">
          {/* Headline metrics */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
            <StatCard
              label="Vehicles departed"
              value={int(metrics?.vehicles_departed)}
              icon={Truck}
              unavailable={!isNumber(metrics?.vehicles_departed)}
              hint="From SUMO, cumulative"
            />
            <StatCard
              label="Vehicles arrived"
              value={int(metrics?.vehicles_arrived)}
              icon={CheckCircle2}
              tone="good"
              unavailable={!isNumber(metrics?.vehicles_arrived)}
              hint="Completed trips"
            />
            <StatCard
              label="Avg waiting time"
              value={num(metrics?.average_waiting_time_s)}
              unit="s"
              icon={Clock}
              unavailable={measured(metrics?.average_waiting_time_s)}
              hint="Mean over collected samples"
            />
            <StatCard
              label="Avg queue length"
              value={num(metrics?.average_queue_length_m)}
              unit="m"
              icon={Route}
              unavailable={measured(metrics?.average_queue_length_m)}
              hint="Mean over collected samples"
            />
            <StatCard
              label="Avg travel time"
              value={num(metrics?.average_travel_time_s)}
              unit="s"
              icon={Timer}
              unavailable={measured(metrics?.average_travel_time_s)}
              hint="Completed trips only"
            />
            <StatCard
              label="Parking occupancy"
              value={num(metrics?.parking_occupancy_pct)}
              unit="%"
              icon={Car}
              tone={
                !isNumber(metrics?.parking_occupancy_pct)
                  ? "default"
                  : metrics.parking_occupancy_pct >= 90
                    ? "bad"
                    : metrics.parking_occupancy_pct >= 70
                      ? "warn"
                      : "good"
              }
              unavailable={measured(metrics?.parking_occupancy_pct)}
              hint="Across all zones"
            />
          </div>

          <div className="grid gap-4 xl:grid-cols-3">
            {/* Simulation status */}
            <Panel title="Simulation status" subtitle="GET /status">
              <dl className="space-y-2.5 text-sm">
                <Row label="State">
                  <StatusPill tone={running ? "good" : "neutral"} dot>
                    {running ? "Running" : "Stopped"}
                  </StatusPill>
                </Row>
                <Row label="Simulation clock">
                  <span className="font-mono text-slate-200">{clock(status?.simulation_time_s)}</span>
                </Row>
                <Row label="Steps executed">
                  <span className="font-mono text-slate-200">{int(metrics?.steps)}</span>
                </Row>
                <Row label="Decisions recorded">
                  <span className="font-mono text-slate-200">{int(status?.decisions)}</span>
                </Row>
                <Row label="Pending events">
                  <span className="font-mono text-slate-200">{int(status?.pending_events)}</span>
                </Row>
                <Row label="Agent availability">
                  <span className="font-mono text-slate-200">
                    {pct(metrics?.agent_availability_pct)}
                  </span>
                </Row>
                <Row label="Orchestrator latency">
                  <span className="font-mono text-slate-200">
                    {isNumber(metrics?.orchestrator_processing_latency_ms)
                      ? `${metrics.orchestrator_processing_latency_ms.toFixed(1)} ms`
                      : EMPTY}
                  </span>
                </Row>
                <Row label="Accepted / rejected">
                  <span className="font-mono text-slate-200">
                    {int(metrics?.accepted_decisions)} / {int(metrics?.rejected_decisions)}
                  </span>
                </Row>
              </dl>

              {status?.config && (
                <div className="mt-3 rounded-lg border border-white/5 bg-white/[0.02] p-2.5">
                  <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">
                    Active configuration
                  </p>
                  <p className="mt-1 break-all font-mono text-[10px] leading-relaxed text-slate-500">
                    seed {String(status.config.seed ?? EMPTY)} · step{" "}
                    {String(status.config.step_length ?? EMPTY)}s · end{" "}
                    {String(status.config.end_time ?? EMPTY)}s · signal control{" "}
                    {status.config.signal_control_enabled ? "on" : "off"}
                  </p>
                </div>
              )}
            </Panel>

            {/* Agent availability */}
            <Panel
              title="Agent availability"
              subtitle="GET /agents"
              actions={
                <Link to="/orchestrator" className="text-[11px] font-medium text-accent hover:underline">
                  Details
                </Link>
              }
            >
              <ul className="space-y-2.5">
                {agents.length === 0 && (
                  <li className="text-xs text-slate-500">
                    No agents registered yet. Start the simulation to initialise them.
                  </li>
                )}
                {agents.map((agent) => (
                  <li
                    key={agent.agent}
                    className="rounded-lg border border-white/5 bg-white/[0.02] p-2.5"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-medium text-slate-200">
                        {AGENT_LABELS[agent.agent] ?? agentLabel(agent.agent)}
                      </span>
                      <StatusPill tone={healthTone(agent.health)} dot>
                        {titleCase(agent.health)}
                      </StatusPill>
                    </div>
                    <p className="mt-1 text-[11px] leading-snug text-slate-500">
                      {agent.message || AGENT_DESCRIPTIONS[agent.agent] || "No status message"}
                    </p>
                    {isNumber(agent.latency_ms) && (
                      <p className="mt-1 font-mono text-[10px] text-slate-600">
                        latency {agent.latency_ms.toFixed(1)} ms
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            </Panel>

            {/* Live traffic + emergency */}
            <div className="space-y-4">
              <Panel
                title="Traffic snapshot"
                subtitle="GET /city-state"
                actions={
                  traffic ? (
                    <StatusPill tone={levelTone(traffic.congestion_level)}>
                      {titleCase(traffic.congestion_level)}
                    </StatusPill>
                  ) : null
                }
              >
                {traffic ? (
                  <dl className="space-y-2 text-sm">
                    <Row label="Vehicles in network">
                      <span className="font-mono text-slate-200">{int(traffic.vehicle_count)}</span>
                    </Row>
                    <Row label="Mean speed">
                      <span className="font-mono text-slate-200">
                        {num(traffic.average_speed_mps)} m/s
                      </span>
                    </Row>
                    <Row label="Congested junctions">
                      <span className="font-mono text-slate-200">
                        {traffic.congested_junctions.length}
                      </span>
                    </Row>
                    <Row label="Signalised junctions tracked">
                      <span className="font-mono text-slate-200">
                        {Object.keys(traffic.junction_levels).length}
                      </span>
                    </Row>
                  </dl>
                ) : (
                  <p className="text-xs text-slate-500">
                    No traffic snapshot yet. Start the simulation and advance at least one decision
                    interval.
                  </p>
                )}
              </Panel>

              <Panel
                title="Emergency"
                subtitle="GET /city-state · emergency_vehicles"
                actions={
                  <StatusPill tone={emergencies.length ? "bad" : "neutral"} dot>
                    {emergencies.length} active
                  </StatusPill>
                }
              >
                {emergencies.length === 0 ? (
                  <p className="text-xs text-slate-500">No active emergency vehicles.</p>
                ) : (
                  <ul className="space-y-2">
                    {emergencies.slice(0, 3).map((vehicle) => (
                      <li
                        key={`${vehicle.event_id}-${vehicle.vehicle_id}`}
                        className="flex items-center justify-between gap-2 rounded-lg border border-rose-500/20 bg-rose-500/[0.06] px-2.5 py-2"
                      >
                        <span className="flex items-center gap-1.5 text-xs font-medium text-rose-200">
                          <Siren className="h-3.5 w-3.5" aria-hidden />
                          {vehicle.vehicle_id}
                        </span>
                        <span className="font-mono text-[10px] text-rose-300/80">
                          {vehicle.tls_id ? `next ${vehicle.tls_id}` : "no TLS"}
                          {isNumber(vehicle.eta_s) ? ` · ${vehicle.eta_s.toFixed(0)}s` : ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </Panel>
            </div>
          </div>

          {/* Latest decision */}
          <Panel
            title="Latest orchestrator decision"
            subtitle="GET /decisions/latest"
            actions={
              <Link to="/orchestrator" className="text-[11px] font-medium text-accent hover:underline">
                Full log
              </Link>
            }
          >
            {latest ? (
              <div className="grid gap-3 md:grid-cols-4">
                <div>
                  <p className="stat-label">Type</p>
                  <p className="mt-1 text-sm font-medium text-slate-200">
                    {titleCase(latest.decision_type)}
                  </p>
                </div>
                <div>
                  <p className="stat-label">Source</p>
                  <p className="mt-1 text-sm text-slate-200">
                    {latest.source_agent ? agentLabel(latest.source_agent) : EMPTY}
                  </p>
                </div>
                <div>
                  <p className="stat-label">Validation</p>
                  <p className="mt-1">
                    <StatusPill
                      tone={
                        latest.validation
                          ? latest.validation.valid
                            ? "good"
                            : "bad"
                          : latest.applied
                            ? "good"
                            : "neutral"
                      }
                    >
                      {latest.validation
                        ? titleCase(latest.validation.status)
                        : latest.applied
                          ? "Applied"
                          : "Unvalidated"}
                    </StatusPill>
                  </p>
                </div>
                <div>
                  <p className="stat-label">Simulation time</p>
                  <p className="mt-1 font-mono text-sm text-slate-200">
                    {clock(latest.timestamp_s)}
                  </p>
                </div>
                <div className="md:col-span-4">
                  <p className="stat-label">Explanation</p>
                  <p className="mt-1 text-sm leading-relaxed text-slate-300">
                    {latest.explanation || "No explanation recorded for this decision."}
                  </p>
                  {latest.rejected_reason && (
                    <p className="mt-1 text-xs text-rose-300">
                      Rejected: {latest.rejected_reason}
                    </p>
                  )}
                </div>
              </div>
            ) : (
              <p className="text-xs text-slate-500">
                No decision has been produced yet. Decisions appear after the first control cycle
                (every 5 simulation seconds).
              </p>
            )}
          </Panel>

          {/* Quick stats row */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatCard
              label="Parking free spaces"
              value={int(parking?.total_available)}
              icon={Car}
              tone="good"
              unavailable={!isNumber(parking?.total_available)}
              hint={parking ? `of ${parking.total_capacity} spaces` : "No zone data yet"}
            />
            <StatCard
              label="Parking search time"
              value={num(metrics?.parking_search_time_s)}
              unit="s"
              icon={GaugeIcon}
              unavailable={measured(metrics?.parking_search_time_s)}
            />
            <StatCard
              label="Emergency travel time"
              value={duration(metrics?.emergency_vehicle_travel_time_s)}
              icon={Siren}
              tone="warn"
              unavailable={measured(metrics?.emergency_vehicle_travel_time_s)}
            />
            <StatCard
              label="Priority response"
              value={duration(metrics?.emergency_priority_response_time_s)}
              icon={Activity}
              tone="accent"
              unavailable={measured(metrics?.emergency_priority_response_time_s)}
              hint="Request → green"
            />
          </div>

          {metrics?.notes && metrics.notes.length > 0 && (
            <Panel title="Backend notes" subtitle="SystemMetrics.notes">
              <ul className="space-y-1 text-xs text-slate-400">
                {metrics.notes.map((note, index) => (
                  <li key={index} className="flex gap-2">
                    <Users className="mt-0.5 h-3 w-3 shrink-0 text-slate-600" aria-hidden />
                    {note}
                  </li>
                ))}
              </ul>
            </Panel>
          )}
        </div>
      </AsyncSection>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}
