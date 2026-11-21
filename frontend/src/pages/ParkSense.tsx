import { Car, CircleParking, Gauge, Search, TrendingUp } from "lucide-react";
import { useAppData } from "../context/AppDataContext";
import { Panel } from "../components/Panel";
import { StatCard } from "../components/StatCard";
import { StatusPill } from "../components/StatusPill";
import { AsyncSection, EmptyBlock } from "../components/AsyncState";
import { Gauge as GaugeChart } from "../components/charts/Gauge";
import { OccupancyChart } from "../components/charts/OccupancyChart";
import { EMPTY, duration, int, isNumber, pct, titleCase } from "../lib/format";

function zoneTone(status: string): "good" | "warn" | "bad" | "neutral" {
  const value = status.toUpperCase();
  if (value === "AVAILABLE" || value === "FREE" || value === "OPEN") return "good";
  if (value === "FULL" || value === "CLOSED") return "bad";
  if (value === "LIMITED" || value === "BUSY") return "warn";
  return "neutral";
}

export function ParkSense() {
  const { parking, cityState, decisions, metrics, loading, error, refresh } = useAppData();

  const state = parking?.parking ?? cityState?.parking ?? null;
  const zones = state?.zones ?? [];

  const parkingDecisions = decisions.filter(
    (decision) => decision.decision_type === "PARKING_RECOMMENDATION" || decision.source_agent === "park_sense"
  );

  // Recommendations are carried on the decisions themselves; there is no
  // standalone recommendations endpoint.
  const recommendations = parkingDecisions
    .flatMap((decision) =>
      decision.parking_recommendations.map((rec) => ({
        ...rec,
        decisionId: decision.decision_id,
        timestamp: decision.timestamp_s,
        applied: decision.applied,
        explanation: decision.explanation,
      }))
    )
    .sort((a, b) => b.timestamp - a.timestamp);

  const occupancy = parking?.occupancy_pct ?? state?.overall_occupancy_pct ?? null;

  return (
    <div className="space-y-4">
      <AsyncSection loading={loading && !parking && !cityState} error={error && !cityState ? error : null} onRetry={refresh}>
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
            <StatCard
              label="Overall occupancy"
              value={pct(occupancy)}
              icon={Gauge}
              tone={isNumber(occupancy) ? (occupancy >= 90 ? "bad" : occupancy >= 70 ? "warn" : "good") : "default"}
              unavailable={!isNumber(occupancy)}
              hint="ParkSense"
            />
            <StatCard
              label="Total capacity"
              value={int(state?.total_capacity)}
              icon={CircleParking}
              unavailable={!isNumber(state?.total_capacity)}
            />
            <StatCard
              label="Occupied bays"
              value={int(state?.total_occupied)}
              icon={Car}
              unavailable={!isNumber(state?.total_occupied)}
            />
            <StatCard
              label="Available bays"
              value={int(state?.total_available)}
              icon={CircleParking}
              tone={isNumber(state?.total_available) && state.total_available === 0 ? "bad" : "good"}
              unavailable={!isNumber(state?.total_available)}
            />
            <StatCard
              label="Search time"
              value={duration(parking?.search_time_s ?? metrics?.parking_search_time_s)}
              icon={Search}
              unavailable={!isNumber(parking?.search_time_s ?? metrics?.parking_search_time_s)}
              hint="GET /metrics"
            />
            <StatCard
              label="Recommendations"
              value={int(recommendations.length)}
              icon={TrendingUp}
              hint="From the decision log"
            />
          </div>

          <div className="grid gap-4 xl:grid-cols-3">
            <Panel className="xl:col-span-2" title="Zone occupancy" subtitle="Live occupancy per zone">
              <OccupancyChart zones={zones} height={260} />
            </Panel>

            <Panel title="Network occupancy" subtitle="Aggregate across all zones">
              <div className="flex flex-col items-center justify-center gap-4 py-2">
                <GaugeChart value={occupancy} label="Occupied" unit="%" invert />
                <dl className="w-full space-y-2 text-xs">
                  <Row label="Simulation time" value={state ? `${state.timestamp_s.toFixed(0)} s` : EMPTY} />
                  <Row label="Zones reporting" value={int(zones.length)} />
                  <Row label="Capacity" value={int(state?.total_capacity)} />
                  <Row label="Occupied" value={int(state?.total_occupied)} />
                  <Row label="Available" value={int(state?.total_available)} />
                </dl>
              </div>
            </Panel>
          </div>

          <Panel
            title="Zone availability"
            subtitle="city-state.parking.zones"
            actions={<StatusPill tone="info">{zones.length} zones</StatusPill>}
          >
            {zones.length === 0 ? (
              <EmptyBlock
                title="No parking zones reported"
                description="ParkSense samples zone occupancy during each control cycle after the simulation starts."
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px] border-collapse text-left">
                  <thead className="table-head">
                    <tr>
                      <th className="px-3 py-2">Zone</th>
                      <th className="px-3 py-2">Name</th>
                      <th className="px-3 py-2 text-right">Capacity</th>
                      <th className="px-3 py-2 text-right">Occupied</th>
                      <th className="px-3 py-2 text-right">Available</th>
                      <th className="px-3 py-2 text-right">Occupancy</th>
                      <th className="px-3 py-2">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {zones.map((zone) => (
                      <tr key={zone.zone_id}>
                        <td className="table-cell font-mono text-xs">{zone.zone_id}</td>
                        <td className="table-cell">{zone.name || EMPTY}</td>
                        <td className="table-cell text-right font-mono">{int(zone.capacity)}</td>
                        <td className="table-cell text-right font-mono">{int(zone.occupied)}</td>
                        <td className="table-cell text-right font-mono">{int(zone.available)}</td>
                        <td className="table-cell text-right">
                          <div className="flex items-center justify-end gap-2">
                            <div className="h-1.5 w-16 overflow-hidden rounded-full bg-white/5">
                              <div
                                className={`h-full rounded-full ${
                                  zone.occupancy_pct >= 90
                                    ? "bg-rose-500"
                                    : zone.occupancy_pct >= 70
                                      ? "bg-amber-500"
                                      : "bg-emerald-500"
                                }`}
                                style={{ width: `${Math.min(100, Math.max(0, zone.occupancy_pct))}%` }}
                              />
                            </div>
                            <span className="w-14 text-right font-mono text-xs">{pct(zone.occupancy_pct)}</span>
                          </div>
                        </td>
                        <td className="table-cell">
                          <StatusPill tone={zoneTone(zone.status)}>{titleCase(zone.status)}</StatusPill>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>

          <Panel
            title="Recommendations"
            subtitle="parking_recommendations on ParkSense decisions"
            actions={<StatusPill tone="info">{recommendations.length}</StatusPill>}
          >
            {recommendations.length === 0 ? (
              <EmptyBlock
                title="No recommendations yet"
                description="A recommendation is only listed when the orchestrator actually produced one. Nothing is generated client-side."
              />
            ) : (
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                {recommendations.slice(0, 24).map((rec, index) => (
                  <article
                    key={`${rec.decisionId}-${rec.zone_id}-${index}`}
                    className="rounded-lg border border-white/5 bg-white/[0.02] p-3"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <p className="truncate text-sm font-semibold text-white">{rec.name || rec.zone_id}</p>
                        <p className="font-mono text-[10px] text-slate-500">{rec.zone_id}</p>
                      </div>
                      <StatusPill tone={rec.applied ? "good" : "neutral"}>
                        {rec.applied ? "Applied" : "Logged"}
                      </StatusPill>
                    </div>
                    <div className="mt-2 grid grid-cols-3 gap-2 text-center">
                      <Metric label="Score" value={rec.score.toFixed(2)} />
                      <Metric label="Free" value={int(rec.available)} />
                      <Metric label="Occ." value={pct(rec.occupancy_pct, 0)} />
                    </div>
                    <p className="mt-2 text-[11px] leading-snug text-slate-400">{rec.reason || rec.explanation || EMPTY}</p>
                  </article>
                ))}
              </div>
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

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md bg-white/[0.03] px-1.5 py-1">
      <p className="text-[9px] uppercase tracking-[0.1em] text-slate-500">{label}</p>
      <p className="font-mono text-xs text-slate-200">{value}</p>
    </div>
  );
}
