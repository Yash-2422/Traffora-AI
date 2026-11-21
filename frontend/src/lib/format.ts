/**
 * Null-safe formatters.
 *
 * The backend returns `null` for any metric it has not measured yet. These
 * helpers render that as an em dash instead of `0`, so the dashboard never
 * implies a measurement that did not happen.
 */

export const EMPTY = "—";

export function isNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function num(value: number | null | undefined, digits = 1): string {
  if (!isNumber(value)) return EMPTY;
  return value.toFixed(digits);
}

export function int(value: number | null | undefined): string {
  if (!isNumber(value)) return EMPTY;
  return Math.round(value).toLocaleString("en-US");
}

export function pct(value: number | null | undefined, digits = 1): string {
  if (!isNumber(value)) return EMPTY;
  return `${value.toFixed(digits)}%`;
}

/** Seconds -> "m:ss" for simulation clocks. */
export function clock(seconds: number | null | undefined): string {
  if (!isNumber(seconds)) return EMPTY;
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** Seconds -> "12.4 s" / "3 m 12 s". */
export function duration(seconds: number | null | undefined): string {
  if (!isNumber(seconds)) return EMPTY;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m} m ${s} s`;
}

export function metres(value: number | null | undefined): string {
  if (!isNumber(value)) return EMPTY;
  if (value >= 1000) return `${(value / 1000).toFixed(2)} km`;
  return `${value.toFixed(0)} m`;
}

/** ISO timestamp -> local "HH:MM:SS". */
export function timeOfDay(iso: string | null | undefined): string {
  if (!iso) return EMPTY;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return EMPTY;
  return date.toLocaleTimeString("en-GB", { hour12: false });
}

/** ISO timestamp -> local date + time. */
export function dateTime(iso: string | null | undefined): string {
  if (!iso) return EMPTY;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return EMPTY;
  return date.toLocaleString("en-GB", { hour12: false });
}

export function titleCase(value: string | null | undefined): string {
  if (!value) return EMPTY;
  return value
    .replace(/[_-]+/g, " ")
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/** "traffic_mind" -> "TrafficMind" style label, falling back to the raw id. */
export function agentLabel(value: string | null | undefined): string {
  if (!value) return EMPTY;
  const known: Record<string, string> = {
    traffic_mind: "TrafficMind",
    green_corridor: "GreenCorridor",
    park_sense: "ParkSense",
  };
  return known[value] ?? titleCase(value);
}

export function priorityLabel(priority: number | null | undefined): string {
  switch (priority) {
    case 1:
      return "P1 · Emergency";
    case 2:
      return "P2 · Traffic";
    case 3:
      return "P3 · Parking";
    default:
      return EMPTY;
  }
}
