import { BarChart3, Car, Clock, Gauge, Route, Timer } from "lucide-react";
import { useAppData } from "../context/AppDataContext";
import { Panel } from "../components/Panel";
import { StatCard } from "../components/StatCard";
import { AsyncSection, EmptyBlock } from "../components/AsyncState";
import { TrendChart } from "../components/charts/TrendChart";
import { useMetricHistory } from "../hooks/useMetricHistory";
import { EMPTY, clock, duration, int, isNumber, num, pct } from "../lib/format";

export function Analytics() {
  const { metrics, traffic, parking, cityState, status, loading, error, refresh, pollIntervalMs } = useAppData();
  const history = useMetricHistory();

  const waiting = metrics?.average_waiting_time_s ?? traffic?.average_waiting_time_s ?? null;
  const queue = metrics?.average_queue_length_m ?? traffic?.average_queue_length ?? null;
  const travel = metrics?.average_travel_time_s ?? traffic?.average_travel_time_s ?? null;
  const occupancy = metrics?.parking_occupancy_pct ?? parking?.occupancy_pct ?? null;
  const emergencyTravel = metrics?.emergency_vehicle_travel_time_s ?? null;
  const emergencyResponse = metrics?.emergency_priority_response_time_s ?? null;

  const departed = metrics?.vehicles_departed ?? traffic?.vehicles_departed ?? null;
  const arrived = metrics?.vehicles_arrived ?? traffic?.vehicles_arrived ?? null;
  const completion =
    isNumber(departed) && isNumber(arrived) && departed > 0 ? (arrived / departed) * 100 : null;

  const acceptanceRate =
    isNumber(metrics?.accepted_decisions) &&
    isNumber(metrics?.rejected_decisions) &&
    metrics.accepted_decisions + metrics.rejected_decisions > 0
      ? (metrics.accepted_decisions / (metrics.accepted_decisions + metrics.rejected_decisions)) * 100
      : null;

  return (
    <div className="space-y-4">
      <AsyncSection loading={loading && !metrics} error={error && !metrics ? error : null} onRetry={refresh}>
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
            <StatCard
              label="Avg waiting time"
              value={num(waiting)}
              unit="s"
              icon={Clock}
              unavailable={!isNumber(waiting)}
              hint="GET /metrics"
            />
            <StatCard
              label="Avg queue length"
              value={num(queue)}
              unit="m"
              icon={Route}
              unavailable={!isNumber(queue)}
              hint="GET /metrics"
            />
            <StatCard
              label="Avg travel time"
              value={duration(travel)}
              icon={Timer}
              unavailable={!isNumber(travel)}
              hint="GET /metrics"
            />
            <StatCard
              label="Emergency response"
              value={duration(emergencyResponse)}
              icon={Timer}
              tone="warn"
              unavailable={!isNumber(emergencyResponse)}
              hint="Priority request to green"
            />
            <StatCard
              label="Emergency travel"
              value={duration(emergencyTravel)}
              icon={Timer}
              unavailable={!isNumber(emergencyTravel)}
              hint="GET /metrics"
            />
            <StatCard
              label="Parking occupancy"
              value={pct(occupancy)}
              icon={Gauge}
              tone={isNumber(occupancy) ? (occupancy >= 90 ? "bad" : occupancy >= 70 ? "warn" : "good") : "default"}
              unavailable={!isNumber(occupancy)}
              hint="GET /metrics"
            />
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <Panel title="Waiting time" subtitle="Measured average per sample">
              <TrendChart
                data={history}
                height={230}
                yUnit="s"
                series={[{ key: "waiting", label: "Avg waiting", color: "#22d3ee", unit: " s" }]}
              />
            </Panel>
            <Panel title="Queue length" subtitle="Measured average per sample">
              <TrendChart
                data={history}
                height={230}
                yUnit="m"
                series={[{ key: "queue", label: "Avg queue", color: "#f59e0b", unit: " m" }]}
              />
            </Panel>
            <Panel title="Travel time" subtitle="Measured average per sample">
              <TrendChart
                data={history}
                height={230}
                yUnit="s"
                series={[{ key: "travel", label: "Avg travel", color: "#a78bfa", unit: " s" }]}
              />
            </Panel>
            <Panel title="Parking occupancy" subtitle="Measured aggregate per sample">
              <TrendChart
                data={history}
                height={230}
                yUnit="%"
                series={[{ key: "occupancy", label: "Occupancy", color: "#34d399", unit: "%" }]}
              />
            </Panel>
          </div>

          <div className="grid gap-4 xl:grid-cols-3">
            <Panel title="Throughput" subtitle="Vehicles departed vs arrived">
              <TrendChart
                data={history}
                height={220}
                series={[
                  { key: "departed", label: "Departed", color: "#38bdf8", unit: "" },
                  { key: "arrived", label: "Arrived", color: "#4ade80", unit: "" },
                ]}
              />
            </Panel>

            <Panel title="Simulation totals" subtitle="GET /metrics and /status">
              <dl className="space-y-2 text-xs">
                <Row label="Simulation time" value={clock(metrics?.simulation_time_s ?? status?.simulation_time_s)} />
                <Row label="Steps executed" value={int(metrics?.steps)} />
                <Row label="Vehicles departed" value={int(departed)} />
                <Row label="Vehicles arrived" value={int(arrived)} />
                <Row label="Trip completion" value={pct(completion)} />
                <Row label="Vehicles in network" value={int(cityState?.traffic?.vehicle_count)} />
                <Row label="Congestion events" value={int(traffic?.congestion_events)} />
                <Row label="Parking search time" value={duration(metrics?.parking_search_time_s)} />
                <Row label="Agent availability" value={pct(metrics?.agent_availability_pct)} />
              </dl>
            </Panel>

            <Panel title="Decision quality" subtitle="Orchestrator validation outcomes">
              <dl className="space-y-2 text-xs">
                <Row label="Accepted decisions" value={int(metrics?.accepted_decisions)} />
                <Row label="Rejected decisions" value={int(metrics?.rejected_decisions)} />
                <Row label="Acceptance rate" value={pct(acceptanceRate)} />
                <Row
                  label="Processing latency"
                  value={
                    metrics?.orchestrator_processing_latency_ms != null
                      ? `${metrics.orchestrator_processing_latency_ms.toFixed(1)} ms`
                      : EMPTY
                  }
                />
                <Row label="Pending events" value={int(status?.pending_events)} />
                <Row label="Decisions in log" value={int(status?.decisions)} />
              </dl>
            </Panel>
          </div>

          <Panel title="Metric provenance" subtitle="What is measured and what is not">
            {metrics?.notes?.length ? (
              <ul className="mb-3 space-y-1 rounded-lg border border-white/5 bg-white/[0.02] p-3">
                {metrics.notes.map((note, index) => (
                  <li key={index} className="text-[11px] leading-relaxed text-slate-400">
                    · {note}
                  </li>
                ))}
              </ul>
            ) : null}
            <div className="grid gap-3 md:grid-cols-2">
              <div className="rounded-lg border border-white/5 bg-white/[0.02] p-3">
                <p className="flex items-center gap-2 text-xs font-semibold text-slate-200">
                  <BarChart3 className="h-3.5 w-3.5 text-accent" aria-hidden />
                  Time series
                </p>
                <p className="mt-1 text-[11px] leading-relaxed text-slate-400">
                  The backend exposes current values only — there is no history endpoint. These charts
                  accumulate the {history.length} samples this browser received while polling every{" "}
                  {(pollIntervalMs / 1000).toFixed(0)} s. Values are never interpolated or back-filled.
                </p>
              </div>
              <div className="rounded-lg border border-white/5 bg-white/[0.02] p-3">
                <p className="flex items-center gap-2 text-xs font-semibold text-slate-200">
                  <Car className="h-3.5 w-3.5 text-accent" aria-hidden />
                  Missing values
                </p>
                <p className="mt-1 text-[11px] leading-relaxed text-slate-400">
                  Any metric the orchestrator reports as <code className="font-mono text-slate-300">null</code>{" "}
                  is rendered as an em dash and excluded from the charts, so an unmeasured value can never be
                  mistaken for zero.
                </p>
              </div>
            </div>
            {!metrics && (
              <EmptyBlock
                title="No metrics received"
                description="Start the simulation so the orchestrator can report metrics."
              />
            )}
          </Panel>
        </div>
      </AsyncSection>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <dt className="text-slate-500">{label}</dt>
      <dd className="font-mono text-slate-300">{value}</dd>
    </div>
  );
}
