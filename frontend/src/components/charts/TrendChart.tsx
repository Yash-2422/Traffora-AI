import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { MetricSample } from "../../hooks/useMetricHistory";
import { EmptyBlock } from "../AsyncState";

export interface SeriesConfig {
  key: keyof MetricSample;
  label: string;
  color: string;
  unit?: string;
}

interface TrendChartProps {
  data: MetricSample[];
  series: SeriesConfig[];
  height?: number;
  yUnit?: string;
}

const tooltipStyle = {
  backgroundColor: "#0f172a",
  border: "1px solid rgba(255,255,255,0.08)",
  borderRadius: 8,
  fontSize: 12,
  color: "#e2e8f0",
} as const;

export function TrendChart({ data, series, height = 240, yUnit }: TrendChartProps) {
  const usable = data.filter((sample) => series.some((s) => sample[s.key] != null));

  if (usable.length < 2) {
    return (
      <EmptyBlock
        title="Not enough samples yet"
        description="The chart fills as the orchestrator reports measured values. Start the simulation and advance it to collect data."
      />
    );
  }

  return (
    <div style={{ height }} className="w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={usable} margin={{ top: 8, right: 12, bottom: 0, left: -12 }}>
          <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
          <XAxis
            dataKey="t"
            stroke="#475569"
            tick={{ fontSize: 10, fill: "#64748b" }}
            tickFormatter={(value: number) => `${Math.round(value)}s`}
            minTickGap={24}
          />
          <YAxis
            stroke="#475569"
            tick={{ fontSize: 10, fill: "#64748b" }}
            width={48}
            tickFormatter={(value: number) => (yUnit ? `${value}${yUnit}` : String(value))}
          />
          <Tooltip
            contentStyle={tooltipStyle}
            labelStyle={{ color: "#94a3b8" }}
            labelFormatter={(value) => `t = ${Math.round(Number(value))}s`}
            formatter={(value, name) => {
              const config = series.find((s) => s.label === name);
              const numeric = typeof value === "number" ? value.toFixed(2) : String(value);
              return [`${numeric}${config?.unit ?? ""}`, name];
            }}
          />
          <Legend
            wrapperStyle={{ fontSize: 11, color: "#94a3b8", paddingTop: 6 }}
            iconType="plainline"
          />
          {series.map((s) => (
            <Line
              key={String(s.key)}
              type="monotone"
              dataKey={s.key}
              name={s.label}
              stroke={s.color}
              strokeWidth={2}
              dot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
