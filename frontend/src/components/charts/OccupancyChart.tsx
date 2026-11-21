import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ParkingZone } from "../../lib/types";
import { EmptyBlock } from "../AsyncState";

function barColor(pct: number): string {
  if (pct >= 90) return "#f43f5e";
  if (pct >= 70) return "#f59e0b";
  return "#10b981";
}

export function OccupancyChart({ zones, height = 240 }: { zones: ParkingZone[]; height?: number }) {
  if (!zones.length) {
    return (
      <EmptyBlock
        title="No parking zones reported"
        description="The orchestrator reports zones only after the simulation has started and ParkSense has sampled occupancy."
      />
    );
  }

  const data = zones.map((zone) => ({
    name: zone.zone_id,
    label: zone.name || zone.zone_id,
    occupancy: Number(zone.occupancy_pct.toFixed(1)),
    available: zone.available,
    capacity: zone.capacity,
  }));

  return (
    <div style={{ height }} className="w-full">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: -16 }}>
          <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
          <XAxis dataKey="name" stroke="#475569" tick={{ fontSize: 10, fill: "#64748b" }} />
          <YAxis
            domain={[0, 100]}
            stroke="#475569"
            tick={{ fontSize: 10, fill: "#64748b" }}
            width={44}
            tickFormatter={(value: number) => `${value}%`}
          />
          <Tooltip
            cursor={{ fill: "rgba(255,255,255,0.04)" }}
            contentStyle={{
              backgroundColor: "#0f172a",
              border: "1px solid rgba(255,255,255,0.08)",
              borderRadius: 8,
              fontSize: 12,
              color: "#e2e8f0",
            }}
            formatter={(value, _name, item) => {
              const payload = item?.payload as { available?: number; capacity?: number; label?: string };
              return [
                `${Number(value).toFixed(1)}% · ${payload?.available ?? "—"}/${payload?.capacity ?? "—"} free`,
                payload?.label ?? "Occupancy",
              ];
            }}
          />
          <Bar dataKey="occupancy" radius={[4, 4, 0, 0]} isAnimationActive={false}>
            {data.map((entry) => (
              <Cell key={entry.name} fill={barColor(entry.occupancy)} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
