import { apiClient, toApiError } from "./api";
import { DECISIONS_LIMIT, EVENTS_LIMIT } from "./config";
import type {
  AgentStatus,
  CityState,
  EmergencyVehicle,
  OrchestratorDecision,
  OrchestratorEvent,
  ParkingAnalytics,
  SimulationStartResponse,
  SimulationStepResponse,
  SimulationStopResponse,
  SystemMetrics,
  SystemStatus,
  TrafficAnalytics,
} from "./types";

/**
 * One function per endpoint in backend/orchestrator/api.py.
 *
 * Nothing is invented: each path, query parameter and return type below is
 * taken from the FastAPI route decorators and the Pydantic models they return.
 */

/* ------------------------------------------------------------------- meta */
export async function getHealth(): Promise<{ status: string }> {
  try {
    const { data } = await apiClient.get<{ status: string }>("/health");
    return data;
  } catch (error) {
    throw toApiError(error);
  }
}

export async function getStatus(): Promise<SystemStatus> {
  try {
    const { data } = await apiClient.get<SystemStatus>("/status");
    return data;
  } catch (error) {
    throw toApiError(error);
  }
}

/* ------------------------------------------------------ simulation control */
export async function startSimulation(): Promise<SimulationStartResponse> {
  try {
    const { data } = await apiClient.post<SimulationStartResponse>("/simulation/start");
    return data;
  } catch (error) {
    throw toApiError(error);
  }
}

export async function stopSimulation(): Promise<SimulationStopResponse> {
  try {
    const { data } = await apiClient.post<SimulationStopResponse>("/simulation/stop");
    return data;
  } catch (error) {
    throw toApiError(error);
  }
}

/** Advance the simulation. `n` must be 1..600 (backend validation). */
export async function stepSimulation(n = 1): Promise<SimulationStepResponse> {
  try {
    const { data } = await apiClient.post<SimulationStepResponse>("/simulation/step", null, {
      params: { n },
    });
    return data;
  } catch (error) {
    throw toApiError(error);
  }
}

/* ------------------------------------------------------------ city & agents */
export async function getCityState(): Promise<CityState> {
  try {
    const { data } = await apiClient.get<CityState>("/city-state");
    return data;
  } catch (error) {
    throw toApiError(error);
  }
}

export async function getAgents(): Promise<AgentStatus[]> {
  try {
    const { data } = await apiClient.get<AgentStatus[]>("/agents");
    return data;
  } catch (error) {
    throw toApiError(error);
  }
}

/* --------------------------------------------------------------- decisions */
export async function getLatestDecision(): Promise<OrchestratorDecision | null> {
  try {
    const { data } = await apiClient.get<OrchestratorDecision | null>("/decisions/latest");
    return data;
  } catch (error) {
    throw toApiError(error);
  }
}

export async function getDecisions(limit = DECISIONS_LIMIT): Promise<OrchestratorDecision[]> {
  try {
    const { data } = await apiClient.get<OrchestratorDecision[]>("/decisions", {
      params: { limit },
    });
    return data;
  } catch (error) {
    throw toApiError(error);
  }
}

/* ------------------------------------------------------------------ events */
export async function getEvents(limit = EVENTS_LIMIT): Promise<OrchestratorEvent[]> {
  try {
    const { data } = await apiClient.get<OrchestratorEvent[]>("/events", { params: { limit } });
    return data;
  } catch (error) {
    throw toApiError(error);
  }
}

/**
 * `/emergency` returns a mixed array: active `EmergencyEvent` objects (which
 * carry `vehicle_id`) followed by `Event` objects (which carry `event_type`).
 * They are split here so the UI never has to guess.
 */
export interface EmergencyPayload {
  vehicles: EmergencyVehicle[];
  events: OrchestratorEvent[];
}

export async function getEmergency(): Promise<EmergencyPayload> {
  try {
    const { data } = await apiClient.get<unknown[]>("/emergency");
    const vehicles: EmergencyVehicle[] = [];
    const events: OrchestratorEvent[] = [];
    for (const item of data ?? []) {
      if (typeof item !== "object" || item === null) continue;
      const record = item as Record<string, unknown>;
      if (typeof record.vehicle_id === "string") {
        vehicles.push(record as unknown as EmergencyVehicle);
      } else if (typeof record.event_type === "string") {
        events.push(record as unknown as OrchestratorEvent);
      }
    }
    return { vehicles, events };
  } catch (error) {
    throw toApiError(error);
  }
}

/* --------------------------------------------------------------- analytics */
export async function getTrafficAnalytics(): Promise<TrafficAnalytics> {
  try {
    const { data } = await apiClient.get<TrafficAnalytics>("/analytics/traffic");
    return data;
  } catch (error) {
    throw toApiError(error);
  }
}

export async function getParkingAnalytics(): Promise<ParkingAnalytics> {
  try {
    const { data } = await apiClient.get<ParkingAnalytics>("/analytics/parking");
    return data;
  } catch (error) {
    throw toApiError(error);
  }
}

export async function getMetrics(): Promise<SystemMetrics> {
  try {
    const { data } = await apiClient.get<SystemMetrics>("/metrics");
    return data;
  } catch (error) {
    throw toApiError(error);
  }
}
