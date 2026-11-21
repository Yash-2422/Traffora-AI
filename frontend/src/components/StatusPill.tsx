import type { ReactNode } from "react";

export type PillTone = "neutral" | "good" | "warn" | "bad" | "accent" | "info";

const TONES: Record<PillTone, string> = {
  neutral: "border-white/10 bg-white/5 text-slate-300",
  good: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
  warn: "border-amber-500/30 bg-amber-500/10 text-amber-300",
  bad: "border-rose-500/30 bg-rose-500/10 text-rose-300",
  accent: "border-accent/30 bg-accent/10 text-accent",
  info: "border-sky-500/30 bg-sky-500/10 text-sky-300",
};

interface StatusPillProps {
  tone?: PillTone;
  children: ReactNode;
  dot?: boolean;
  className?: string;
}

export function StatusPill({ tone = "neutral", children, dot = false, className = "" }: StatusPillProps) {
  return (
    <span className={`chip ${TONES[tone]} ${className}`}>
      {dot && <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden />}
      {children}
    </span>
  );
}

/** Congestion / occupancy level -> tone. Matches the backend's LOW/MEDIUM/HIGH. */
export function levelTone(level: string | null | undefined): PillTone {
  switch ((level ?? "").toUpperCase()) {
    case "HIGH":
    case "CRITICAL":
      return "bad";
    case "MEDIUM":
      return "warn";
    case "LOW":
      return "good";
    default:
      return "neutral";
  }
}

export function healthTone(health: string | null | undefined): PillTone {
  switch ((health ?? "").toUpperCase()) {
    case "READY":
      return "good";
    case "DEGRADED":
      return "warn";
    case "FAILED":
      return "bad";
    case "INITIALIZING":
      return "info";
    default:
      return "neutral";
  }
}

export function eventStatusTone(status: string | null | undefined): PillTone {
  switch ((status ?? "").toUpperCase()) {
    case "PROCESSED":
      return "good";
    case "REJECTED":
    case "FAILED":
      return "bad";
    case "PROCESSING":
      return "info";
    case "PENDING":
      return "warn";
    default:
      return "neutral";
  }
}
