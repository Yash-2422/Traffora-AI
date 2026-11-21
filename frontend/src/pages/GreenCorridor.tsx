import { Ambulance, Clock, Route, ShieldCheck, Siren, Timer } from "lucide-react";
import { useAppData } from "../context/AppDataContext";
import { Panel } from "../components/Panel";
import { StatCard } from "../components/StatCard";
import { StatusPill, eventStatusTone } from "../components/StatusPill";
import { AsyncSection, EmptyBlock } from "../components/AsyncState";
import { EMPTY, clock, duration, int, isNumber, metres, priorityLabel, timeOfDay, titleCase } from "../lib/format";

export function GreenCorridor() {
  const { emergency, cityState, decisions, metrics, loading, error, refresh } = useAppData();

  // `/emergency` merges live vehicles and recorded events into one array; the
  // service already split them. Fall back to city-state for the vehicle list.
  const vehicles = emergency.vehicles.length > 0 ? emergency.vehicles : cityState?.emergency_vehicles ?? [];
  const events = emergency.events;

  const priorityDecisions = decisions.filter(
    (decision) =>
      decision.decision_type === "EMERGENCY_PRIORITY" || decision.source_agent === "green_corridor"
  );

  const approvals = priorityDecisions.filter((decision) => decision.applied);
  const rejections = priorityDecisions.filter((decision) => !decision.applied);
  const activeVehicles = vehicles.filter((vehicle) => vehicle.status.toUpperCase() !== "ARRIVED");

  return (
    <div className="space-y-4">
      <AsyncSection loading={loading && !emergency && !cityState} error={error && !cityState ? error : null} onRetry={refresh}>
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
            <StatCard
              label="Active emergency vehicles"
              value={int(activeVehicles.length)}
              icon={Ambulance}
              tone={activeVehicles.length > 0 ? "bad" : "good"}
              hint="Reported by GreenCorridor"
            />
            <StatCard
              label="Priority requests"
              value={int(priorityDecisions.length)}
              icon={Siren}
              hint="P1 decisions in the decision log"
            />
            <StatCard
              label="Approved corridors"
              value={int(approvals.length)}
              icon={ShieldCheck}
              tone={approvals.length > 0 ? "good" : "default"}
              hint="Applied by the orchestrator"
            />
            <StatCard
              label="Rejected requests"
              value={int(rejections.length)}
              icon={ShieldCheck}
              tone={rejections.length > 0 ? "warn" : "default"}
              hint="Failed validation"
            />
            <StatCard
              label="Emergency travel time"
              value={duration(metrics?.emergency_vehicle_travel_time_s)}
              icon={Timer}
              unavailable={!isNumber(metrics?.emergency_vehicle_travel_time_s)}
              hint="GET /metrics"
            />
            <StatCard
              label="Priority response time"
              value={duration(metrics?.emergency_priority_response_time_s)}
              icon={Clock}
              unavailable={!isNumber(metrics?.emergency_priority_response_time_s)}
              hint="Request to green phase"
            />
          </div>

          <Panel
            title="Active emergency vehicles"
            subtitle="Live vehicle status and route edges"
            actions={<StatusPill tone={activeVehicles.length ? "bad" : "good"} dot>{activeVehicles.length} active</StatusPill>}
          >
            {vehicles.length === 0 ? (
              <EmptyBlock
                title="No emergency vehicles reported"
                description="GreenCorridor publishes a vehicle only when an emergency is detected in the simulation. Nothing is simulated or mocked here."
              />
            ) : (
              <div className="grid gap-3 lg:grid-cols-2">
                {vehicles.map((vehicle) => (
                  <article
                    key={`${vehicle.event_id}-${vehicle.vehicle_id}`}
                    className="rounded-lg border border-white/5 bg-white/[0.02] p-3"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex items-center gap-2">
                        <Siren className="h-4 w-4 text-rose-400" aria-hidden />
                        <span className="font-mono text-sm font-semibold text-white">{vehicle.vehicle_id}</span>
                      </div>
                      <div className="flex items-center gap-1.5">
                        <StatusPill tone="bad">{priorityLabel(vehicle.priority)}</StatusPill>
                        <StatusPill tone={vehicle.status.toUpperCase() === "ARRIVED" ? "good" : "warn"}>
                          {titleCase(vehicle.status)}
                        </StatusPill>
                      </div>
                    </div>

                    <dl className="mt-2.5 grid grid-cols-2 gap-x-3 gap-y-1.5 text-[11px]">
                      <Field label="Event" value={vehicle.event_id} mono />
                      <Field label="Sim time" value={clock(vehicle.timestamp_s)} mono />
                      <Field label="Target junction" value={vehicle.tls_id ?? EMPTY} mono />
                      <Field label="Distance" value={metres(vehicle.distance_m)} mono />
                      <Field label="ETA" value={duration(vehicle.eta_s)} mono />
                      <Field label="Route edges" value={int(vehicle.route.length)} mono />
                    </dl>

                    {vehicle.route.length > 0 && (
                      <div className="mt-2">
                        <p className="text-[10px] uppercase tracking-[0.12em] text-slate-500">Route</p>
                        <p className="mt-1 break-all font-mono text-[10px] leading-relaxed text-slate-400">
                          {vehicle.route.join(" → ")}
                        </p>
                      </div>
                    )}
                  </article>
                ))}
              </div>
            )}
          </Panel>

          <div className="grid gap-4 xl:grid-cols-2">
            <Panel
              title="Priority requests & approvals"
              subtitle="EMERGENCY_PRIORITY decisions"
              actions={<StatusPill tone="info">{priorityDecisions.length}</StatusPill>}
            >
              {priorityDecisions.length === 0 ? (
                <EmptyBlock
                  title="No priority requests yet"
                  description="A request appears here once GreenCorridor raises an emergency event and the orchestrator evaluates it."
                />
              ) : (
                <div className="max-h-96 space-y-2 overflow-y-auto pr-1">
                  {priorityDecisions.slice(0, 40).map((decision) => (
                    <div key={decision.decision_id} className="rounded-lg border border-white/5 bg-white/[0.02] p-2.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <StatusPill tone={decision.applied ? "good" : "bad"}>
                          {decision.applied ? "Approved" : "Rejected"}
                        </StatusPill>
                        <span className="font-mono text-[10px] text-slate-500">t={clock(decision.timestamp_s)}</span>
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
                            <li key={action.action_id} className="font-mono text-[10px] text-slate-400">
                              {action.tls_id} · {action.action_type} · {action.duration_s.toFixed(0)}s
                              {action.emergency_vehicle_id ? ` · ${action.emergency_vehicle_id}` : ""}
                            </li>
                          ))}
                        </ul>
                      )}
                      {decision.validation?.reasons.length ? (
                        <p className="mt-1 text-[10px] text-amber-300/80">
                          {decision.validation.reasons.join(" · ")}
                        </p>
                      ) : null}
                      {decision.rejected_reason && (
                        <p className="mt-1 text-[10px] text-rose-300">{decision.rejected_reason}</p>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </Panel>

            <Panel
              title="Emergency event history"
              subtitle="Emergency events from GET /emergency"
              actions={<StatusPill tone="info">{events.length}</StatusPill>}
            >
              {events.length === 0 ? (
                <EmptyBlock title="No emergency events recorded" />
              ) : (
                <div className="max-h-96 overflow-y-auto">
                  <table className="w-full border-collapse text-left">
                    <thead className="table-head">
                      <tr>
                        <th className="px-3 py-2">Type</th>
                        <th className="px-3 py-2">Status</th>
                        <th className="px-3 py-2">Sim time</th>
                        <th className="px-3 py-2">Created</th>
                      </tr>
                    </thead>
                    <tbody>
                      {events.map((event) => (
                        <tr key={event.event_id}>
                          <td className="table-cell">
                            <span className="font-mono text-[11px] text-slate-200">{event.event_type}</span>
                            <span className="ml-1 text-[10px] text-slate-500">{priorityLabel(event.priority)}</span>
                          </td>
                          <td className="table-cell">
                            <StatusPill tone={eventStatusTone(event.status)}>{titleCase(event.status)}</StatusPill>
                          </td>
                          <td className="table-cell font-mono text-xs">{clock(event.timestamp_s)}</td>
                          <td className="table-cell font-mono text-xs text-slate-500">{timeOfDay(event.created_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Panel>
          </div>

          <Panel title="How this page gets its data" subtitle="No mock emergencies">
            <ul className="space-y-1.5 text-xs leading-relaxed text-slate-400">
              <li className="flex gap-2">
                <Route className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" aria-hidden />
                Vehicles come from <code className="font-mono text-slate-300">GET /emergency</code> (live
                vehicles) with <code className="font-mono text-slate-300">city-state.emergency_vehicles</code> as
                a fallback. Routes are the edge IDs the agent actually reported.
              </li>
              <li className="flex gap-2">
                <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" aria-hidden />
                Approvals are decisions with <code className="font-mono text-slate-300">decision_type = EMERGENCY_PRIORITY</code>{" "}
                and <code className="font-mono text-slate-300">applied = true</code>. Rejections are the same
                decisions with <code className="font-mono text-slate-300">applied = false</code>.
              </li>
              <li className="flex gap-2">
                <Timer className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" aria-hidden />
                Travel and response times are read verbatim from{" "}
                <code className="font-mono text-slate-300">GET /metrics</code>. When the backend reports{" "}
                <code className="font-mono text-slate-300">null</code> the card shows an em dash.
              </li>
            </ul>
          </Panel>
        </div>
      </AsyncSection>
    </div>
  );
}

function Field({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-slate-500">{label}</dt>
      <dd className={`truncate text-slate-300 ${mono ? "font-mono" : ""}`} title={value}>
        {value}
      </dd>
    </div>
  );
}
