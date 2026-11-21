import { EMPTY, isNumber } from "../../lib/format";

/**
 * Simple SVG radial gauge. Renders an em dash when the value is not measured,
 * so an unmeasured metric never looks like zero.
 */
export function Gauge({
  value,
  label,
  unit = "%",
  size = 132,
  invert = false,
}: {
  value: number | null | undefined;
  label: string;
  unit?: string;
  size?: number;
  /** When true, a high value is bad (occupancy); otherwise high is good. */
  invert?: boolean;
}) {
  const measured = isNumber(value);
  const clamped = measured ? Math.max(0, Math.min(100, value)) : 0;
  const radius = size / 2 - 10;
  const circumference = Math.PI * radius;
  const offset = circumference - (clamped / 100) * circumference;

  const tone = !measured
    ? "#334155"
    : invert
      ? clamped >= 90
        ? "#f43f5e"
        : clamped >= 70
          ? "#f59e0b"
          : "#10b981"
      : clamped >= 66
        ? "#10b981"
        : clamped >= 33
          ? "#f59e0b"
          : "#f43f5e";

  return (
    <div className="flex flex-col items-center gap-1">
      <svg width={size} height={size / 2 + 16} viewBox={`0 0 ${size} ${size / 2 + 16}`} role="img" aria-label={`${label}: ${measured ? `${clamped}${unit}` : "not measured"}`}>
        <path
          d={`M 10 ${size / 2} A ${radius} ${radius} 0 0 1 ${size - 10} ${size / 2}`}
          fill="none"
          stroke="rgba(255,255,255,0.07)"
          strokeWidth={9}
          strokeLinecap="round"
        />
        {measured && (
          <path
            d={`M 10 ${size / 2} A ${radius} ${radius} 0 0 1 ${size - 10} ${size / 2}`}
            fill="none"
            stroke={tone}
            strokeWidth={9}
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={offset}
          />
        )}
        <text
          x={size / 2}
          y={size / 2 - 4}
          textAnchor="middle"
          className="fill-white font-mono"
          style={{ fontSize: 20, fontWeight: 600 }}
        >
          {measured ? `${clamped.toFixed(1)}${unit}` : EMPTY}
        </text>
      </svg>
      <span className="text-[11px] font-medium uppercase tracking-[0.12em] text-slate-400">{label}</span>
    </div>
  );
}
