import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

interface StatCardProps {
  label: string;
  value: ReactNode;
  unit?: string;
  hint?: string;
  icon?: LucideIcon;
  tone?: "default" | "good" | "warn" | "bad" | "accent";
  /** Renders a muted "not measured" style when the backend returned null. */
  unavailable?: boolean;
}

const TONES: Record<string, string> = {
  default: "text-white",
  good: "text-emerald-400",
  warn: "text-amber-400",
  bad: "text-rose-400",
  accent: "text-accent",
};

export function StatCard({
  label,
  value,
  unit,
  hint,
  icon: Icon,
  tone = "default",
  unavailable = false,
}: StatCardProps) {
  return (
    <div className="panel p-4">
      <div className="flex items-start justify-between gap-2">
        <span className="stat-label">{label}</span>
        {Icon && <Icon className="h-4 w-4 shrink-0 text-slate-500" aria-hidden />}
      </div>
      <div className="mt-2 flex items-baseline gap-1.5">
        <span
          className={`stat-value ${unavailable ? "text-slate-600" : TONES[tone]}`}
          aria-label={unavailable ? "not measured" : undefined}
        >
          {value}
        </span>
        {unit && !unavailable && <span className="text-xs text-slate-500">{unit}</span>}
      </div>
      {hint && <p className="mt-1 text-[11px] leading-snug text-slate-500">{hint}</p>}
    </div>
  );
}
