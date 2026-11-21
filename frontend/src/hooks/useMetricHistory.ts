import { useMemo } from "react";
import { useAppData } from "../context/AppDataContext";

export interface MetricSample {
  t: number;
  waiting: number | null;
  queue: number | null;
  travel: number | null;
  departed: number | null;
  arrived: number | null;
  occupancy: number | null;
  vehicles: number | null;
}

const MAX_POINTS = 240;

/**
 * Rolling client-side history of measured metrics.
 *
 * The backend exposes current values only (no time-series endpoint), so the
 * dashboard accumulates the samples it actually received. Nothing is
 * interpolated or invented: a sample is appended only when the simulation
 * clock advances or a value changes, and `null` stays `null`.
 *
 * The buffer lives for the lifetime of the tab and is capped at 240 points.
 */
export function useMetricHistory(): MetricSample[] {
  const { metrics, cityState } = useAppData();

  return useMemo(() => {
    // Rebuilt from the current snapshot each render; the buffer is kept in a
    // module-level store keyed by nothing, so use a ref-free approach below.
    return buildSamples(metrics, cityState);
  }, [metrics, cityState]);
}

/** Internal buffer, module scoped so it survives re-renders and route changes. */
const buffer: MetricSample[] = [];
let lastKey = "";

function buildSamples(
  metrics: ReturnType<typeof useAppData>["metrics"],
  cityState: ReturnType<typeof useAppData>["cityState"]
): MetricSample[] {
  if (!metrics) return buffer.slice();

  const t = metrics.simulation_time_s;
  const key = `${t}|${metrics.average_waiting_time_s}|${metrics.average_queue_length_m}|${metrics.average_travel_time_s}|${metrics.vehicles_departed}|${metrics.vehicles_arrived}|${metrics.parking_occupancy_pct}`;

  if (key !== lastKey) {
    lastKey = key;
    const sample: MetricSample = {
      t,
      waiting: metrics.average_waiting_time_s ?? null,
      queue: metrics.average_queue_length_m ?? null,
      travel: metrics.average_travel_time_s ?? null,
      departed: metrics.vehicles_departed ?? null,
      arrived: metrics.vehicles_arrived ?? null,
      occupancy: metrics.parking_occupancy_pct ?? cityState?.parking?.overall_occupancy_pct ?? null,
      vehicles: cityState?.traffic?.vehicle_count ?? null,
    };
    buffer.push(sample);
    if (buffer.length > MAX_POINTS) buffer.splice(0, buffer.length - MAX_POINTS);
  }

  return buffer.slice();
}

/** Clear the buffer (used when the simulation is restarted). */
export function resetMetricHistory(): void {
  buffer.length = 0;
  lastKey = "";
}
