import { Activity, AlertOctagon, CheckCircle2, GitBranch, Layers, ShieldCheck } from "lucide-react";
import { useAppData } from "../context/AppDataContext";
import { Panel } from "../components/Panel";
import { StatCard } from "../components/StatCard";
import { StatusPill, eventStatusTone, healthTone } from "../components/StatusPill";
import { AsyncSection, EmptyBlock } from "../components/AsyncState";
import { AGENT_DESCRIPTIONS, AGENT_LABELS } from "../lib/config";
import { EMPTY, agentLabel, clock, dateTime, int, priorityLabel, titleCase } from "../lib/format";

export function Orchestrator() {
  const { agents, status, decisions, events, metrics, loading, error, refresh } = useAppData();

  // `/agents` and `/status.agents` describe the same registry; prefer the
  // dedicated endpoint and fall back to the status snapshot.
  const agentList = agents.length > 0 ? agents : status?.agents ?? [];
  const availableAgents = agentList.filter((agent) => agent.available).length;

  const rejected = decisions.filter(
    (decision) => !decision.applied || decision.validation?.status === "REJECTED"
  );
  const latest = decisions.slice(0, 12);
  const failedEvents = events.filter((event) => event.status === "FAILED" || event.status === "REJECTED");

  return (
    <div className="space-y-4">
      <AsyncSection loading={loading && !status} error={error && !status ? error : null} onRetry={refresh}>
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
            <StatCard
              label="Agents available"
              value={`${availableAgents}/${agentList.length || 0}`}
              icon={Layers}
              tone={agentList.length > 0 && availableAgents === agentList.length ? "good" : "warn"}
              hint="GET /agents"
            />
            <StatCard label="Decisions logged" value={int(status?.decisions ?? decisions.length)} icon={GitBranch} />
            <StatCard
              label="Pending events"
              value={int(status?.pending_events)}
              icon={Activity}
              tone={isNonZero(status?.pending_events) ? "warn" : "good"}
              hint="Queue depth"
            />
            <StatCard
              label="Accepted decisions"
              value={int(metrics?.accepted_decisions)}
              icon={CheckCircle2}
              tone="good"
            />
            <StatCard
              label="Rejected decisions"
              value={int(metrics?.rejected_decisions)}
              icon={AlertOctagon}
              tone={isNonZero(metrics?.rejected_decisions) ? "bad" : "good"}
            />
            <StatCard
              label="Processing latency"
              value={
                metrics?.orchestrator_processing_latency_ms != null
                  ? metrics.orchestrator_processing_latency_ms.toFixed(1)
                  : EMPTY
              }
              unit="ms"
              icon={ShieldCheck}
              unavailable={metrics?.orchestrator_processing_latency_ms == null}
            />
          </div>

          <Panel title="Agent status" subtitle="Registry health reported by the orchestrator">
            {agentList.length === 0 ? (
              <EmptyBlock
                title="No agents registered"
                description="The registry is populated when the orchestrator is built, which happens on the first API call."
              />
            ) : (
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                {agentList.map((agent) => (
                  <article key={agent.agent} className="rounded-lg border border-white/5 bg-white/[0.02] p-3">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <p className="truncate text-sm font-semibold text-white">
                          {AGENT_LABELS[agent.agent] ?? agentLabel(agent.agent)}
                        </p>
                        <p className="font-mono text-[10px] text-slate-500">{agent.agent}</p>
                      </div>
                      <StatusPill tone={healthTone(agent.health)} dot>
                        {titleCase(agent.health)}
                      </StatusPill>
                    </div>
                    <p className="mt-2 text-[11px] leading-snug text-slate-400">
                      {agent.message || AGENT_DESCRIPTIONS[agent.agent] || EMPTY}
                    </p>
                    <dl className="mt-2 grid grid-cols-2 gap-x-2 gap-y-1 text-[10px]">
                      <Field label="Available" value={agent.available ? "yes" : "no"} />
                      <Field label="Last update" value={clock(agent.last_update_s)} />
                      <Field
                        label="Latency"
                        value={agent.latency_ms != null ? `${agent.latency_ms.toFixed(1)} ms` : EMPTY}
                      />
                      <Field label="Details" value={String(Object.keys(agent.details ?? {}).length)} />
                    </dl>
                  </article>
                ))}
              </div>
            )}
          </Panel>

          <div className="grid gap-4 xl:grid-cols-2">
            <Panel
              title="Latest decisions"
              subtitle="GET /decisions/latest"
              actions={<StatusPill tone="info">{decisions.length}</StatusPill>}
            >
              {latest.length === 0 ? (
                <EmptyBlock
                  title="No decisions yet"
                  description="Decisions are produced by the orchestrator's control cycle. Start the simulation to generate them."
                />
              ) : (
                <div className="max-h-[26rem] space-y-2 overflow-y-auto pr-1">
                  {latest.map((decision) => (
                    <div key={decision.decision_id} className="rounded-lg border border-white/5 bg-white/[0.02] p-2.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <StatusPill tone={decision.applied ? "good" : "bad"}>
                          {decision.applied ? "Applied" : "Rejected"}
                        </StatusPill>
                        <StatusPill tone="neutral">{titleCase(decision.decision_type)}</StatusPill>
                        <span className="font-mono text-[10px] text-slate-500">t={clock(decision.timestamp_s)}</span>
                        <span className="ml-auto font-mono text-[10px] text-slate-500">
                          {priorityLabel(decision.priority)}
                        </span>
                      </div>
                      <p className="mt-1 text-xs leading-snug text-slate-300">
                        {decision.explanation || "No explanation recorded."}
                      </p>
                      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[10px] text-slate-500">
                        <span>src: {agentLabel(decision.source_agent)}</span>
                        <span>signals: {decision.signal_actions.length}</span>
                        <span>parking: {decision.parking_recommendations.length}</span>
                        {decision.validation && <span>validation: {decision.validation.status}</span>}
                      </div>
                      {decision.rejected_reason && (
                        <p className="mt-1 text-[10px] text-rose-300">{decision.rejected_reason}</p>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </Panel>

            <Panel
              title="Safety validation"
              subtitle="Decisions that failed validation"
              actions={<StatusPill tone={rejected.length ? "bad" : "good"}>{rejected.length}</StatusPill>}
            >
              {rejected.length === 0 ? (
                <EmptyBlock
                  title="No rejected decisions"
                  description="Every decision in the current window passed the safety validator."
                  icon={ShieldCheck}
                />
              ) : (
                <div className="max-h-[26rem] space-y-2 overflow-y-auto pr-1">
                  {rejected.slice(0, 30).map((decision) => (
                    <div
                      key={decision.decision_id}
                      className="rounded-lg border border-rose-500/20 bg-rose-500/[0.05] p-2.5"
                    >
                      <div className="flex flex-wrap items-center gap-1.5">
                        <StatusPill tone="bad">
                          {decision.validation?.status ? titleCase(decision.validation.status) : "Rejected"}
                        </StatusPill>
                        <StatusPill tone="neutral">{titleCase(decision.decision_type)}</StatusPill>
                        <span className="font-mono text-[10px] text-slate-500">t={clock(decision.timestamp_s)}</span>
                      </div>
                      {decision.validation?.reasons.length ? (
                        <ul className="mt-1.5 space-y-0.5">
                          {decision.validation.reasons.map((reason, index) => (
                            <li key={index} className="text-[11px] leading-snug text-rose-200/90">
                              · {reason}
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p className="mt-1 text-[11px] text-rose-200/90">
                          {decision.rejected_reason || "No reason recorded."}
                        </p>
                      )}
                      <p className="mt-1 font-mono text-[10px] text-slate-500">{decision.decision_id}</p>
                    </div>
                  ))}
                </div>
              )}
            </Panel>
          </div>

          <Panel
            title="Event history"
            subtitle="GET /events · newest first"
            actions={
              <div className="flex items-center gap-1.5">
                <StatusPill tone="info">{events.length}</StatusPill>
                {failedEvents.length > 0 && <StatusPill tone="bad">{failedEvents.length} failed</StatusPill>}
              </div>
            }
          >
            {events.length === 0 ? (
              <EmptyBlock title="No events recorded" />
            ) : (
              <div className="max-h-[26rem] overflow-auto">
                <table className="w-full min-w-[720px] border-collapse text-left">
                  <thead className="table-head">
                    <tr>
                      <th className="px-3 py-2">Event</th>
                      <th className="px-3 py-2">Type</th>
                      <th className="px-3 py-2">Source</th>
                      <th className="px-3 py-2">Priority</th>
                      <th className="px-3 py-2">Status</th>
                      <th className="px-3 py-2">Sim time</th>
                      <th className="px-3 py-2">Created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {events.map((event) => (
                      <tr key={event.event_id}>
                        <td className="table-cell font-mono text-[11px]">{event.event_id}</td>
                        <td className="table-cell text-xs">{event.event_type}</td>
                        <td className="table-cell text-xs">{agentLabel(event.source_agent)}</td>
                        <td className="table-cell text-xs">{priorityLabel(event.priority)}</td>
                        <td className="table-cell">
                          <StatusPill tone={eventStatusTone(event.status)}>{titleCase(event.status)}</StatusPill>
                        </td>
                        <td className="table-cell font-mono text-xs">{clock(event.timestamp_s)}</td>
                        <td className="table-cell font-mono text-[10px] text-slate-500">
                          {dateTime(event.created_at)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>
      </AsyncSection>
    </div>
  );
}

function isNonZero(value: number | null | undefined): boolean {
  return typeof value === "number" && value > 0;
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-slate-500">{label}</dt>
      <dd className="truncate font-mono text-slate-300" title={value}>
        {value}
      </dd>
    </div>
  );
}
