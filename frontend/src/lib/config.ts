/**
 * Runtime configuration.
 *
 * The API base URL is the only required setting. It is read from
 * VITE_API_BASE_URL at build time so the same bundle can be deployed against a
 * different orchestrator host without a code change.
 */

const rawBase = (import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000").trim();

/** Normalised base URL, no trailing slash. */
export const API_BASE_URL = rawBase.replace(/\/+$/, "");

/**
 * Polling intervals, in milliseconds.
 *
 * The backend exposes no WebSocket or SSE endpoint (see ARCHITECTURE.md), so
 * the dashboard polls. Two rates are used:
 *
 *  - `POLL_RUNNING_MS`  while a simulation is live, because the state changes
 *    only when POST /simulation/step is called but decisions accumulate fast.
 *  - `POLL_IDLE_MS`     while stopped, where almost nothing can change.
 *
 * Polling is suspended entirely while the browser tab is hidden.
 */
export const POLL_RUNNING_MS = 2_000;
export const POLL_IDLE_MS = 5_000;

/** Request timeout for every API call. */
export const REQUEST_TIMEOUT_MS = 20_000;

/** Default page size requested from /decisions and /events. */
export const DECISIONS_LIMIT = 100;
export const EVENTS_LIMIT = 200;

/** Human-readable agent labels. Keys match the backend `AgentName` enum. */
export const AGENT_LABELS: Record<string, string> = {
  traffic_mind: "TrafficMind",
  green_corridor: "GreenCorridor",
  park_sense: "ParkSense",
};

export const AGENT_DESCRIPTIONS: Record<string, string> = {
  traffic_mind: "Congestion monitoring, forecasting and signal optimisation",
  green_corridor: "Emergency vehicle detection and signal preemption",
  park_sense: "Parking occupancy, demand prediction and recommendations",
};
