/**
 * TypeScript mirrors of backend/orchestrator/schemas.py.
 *
 * Every field here exists in the Pydantic model with the same name. Optional
 * (`?`) is used where the backend declares `Optional[...]` or gives a default
 * that may be absent before the first control cycle.
 */

export type EventPriority = 1 | 2 | 3;

export type EventType =
  | "CONGESTION_DETECTED"
  | "CONGESTION_PREDICTED"
  | "EMERGENCY_DETECTED"
  | "EMERGENCY_ROUTE_UPDATE"
  | "SIGNAL_PRIORITY_REQUEST"
  | "PARKING_OCCUPANCY_CHANGED"
  | "PARKING_DEMAND_ALERT";

export type EventStatus = "PENDING" | "PROCESSING" | "PROCESSED" | "REJECTED" | "FAILED";

export type AgentName = "traffic_mind" | "green_corridor" | "park_sense";

export type AgentHealth =
  | "UNKNOWN"
  | "INITIALIZING"
  | "READY"
  | "DEGRADED"
  | "FAILED"
  | "STOPPED";

export type DecisionType =
  | "EMERGENCY_PRIORITY"
  | "TRAFFIC_OPTIMIZATION"
  | "PARKING_RECOMMENDATION"
  | "NO_ACTION";

export type ActionType = "SET_PHASE_DURATION" | "RESTORE_SIGNAL" | "PARKING_ADVICE" | "NONE";

export type ValidationStatus = "VALID" | "REJECTED" | "DEFERRED";

export type CongestionLevel = "LOW" | "MEDIUM" | "HIGH" | string;

/* ------------------------------------------------------------------ traffic */
export interface TrafficState {
  timestamp_s: number;
  vehicle_count: number;
  average_speed_mps: number;
  average_waiting_time_s: number;
  average_queue_length_m: number;
  congestion_level: CongestionLevel;
  congested_junctions: string[];
  junction_levels: Record<string, CongestionLevel>;
  source: string;
}

/* ---------------------------------------------------------------- emergency */
export interface EmergencyVehicle {
  event_id: string;
  vehicle_id: string;
  timestamp_s: number;
  tls_id?: string | null;
  distance_m?: number | null;
  eta_s?: number | null;
  route: string[];
  status: string;
  priority: EventPriority;
}

/* ------------------------------------------------------------------ parking */
export interface ParkingZone {
  zone_id: string;
  name: string;
  capacity: number;
  occupied: number;
  available: number;
  occupancy_pct: number;
  status: string;
}

export interface ParkingState {
  timestamp_s: number;
  zones: ParkingZone[];
  total_capacity: number;
  total_occupied: number;
  total_available: number;
  overall_occupancy_pct: number;
}

/* ------------------------------------------------------------------- agents */
export interface AgentStatus {
  agent: AgentName;
  health: AgentHealth;
  available: boolean;
  message: string;
  last_update_s?: number | null;
  latency_ms?: number | null;
  details: Record<string, unknown>;
}

/* ------------------------------------------------------ actions / decisions */
export interface SignalAction {
  action_id: string;
  tls_id: string;
  action_type: ActionType;
  duration_s: number;
  phase_index?: number | null;
  reason: string;
  source_agent: AgentName;
  created_at_s: number;
  emergency_vehicle_id?: string | null;
}

export interface ParkingRecommendation {
  zone_id: string;
  name: string;
  score: number;
  available: number;
  occupancy_pct: number;
  reason: string;
}

export interface ValidationResult {
  valid: boolean;
  status: ValidationStatus;
  reasons: string[];
  checked_at_s: number;
}

export interface OrchestratorDecision {
  decision_id: string;
  timestamp_s: number;
  decision_type: DecisionType;
  priority: EventPriority;
  source_agent?: AgentName | null;
  signal_actions: SignalAction[];
  parking_recommendations: ParkingRecommendation[];
  explanation: string;
  validation?: ValidationResult | null;
  applied: boolean;
  rejected_reason?: string | null;
  metadata: Record<string, unknown>;
}

/* ------------------------------------------------------------------- events */
export interface OrchestratorEvent {
  event_id: string;
  event_type: EventType;
  source_agent: AgentName;
  timestamp_s: number;
  priority: EventPriority;
  payload: Record<string, unknown>;
  status: EventStatus;
  created_at: string;
  processed_at?: string | null;
  error?: string | null;
  dedupe_key?: string | null;
}

/* -------------------------------------------------------------- city state */
export interface CityState {
  timestamp_s: number;
  traffic?: TrafficState | null;
  emergency_vehicles: EmergencyVehicle[];
  parking?: ParkingState | null;
  agents: AgentStatus[];
  latest_decision?: OrchestratorDecision | null;
}

/* ----------------------------------------------------------------- metrics */
export interface SystemMetrics {
  simulation_time_s: number;
  steps: number;
  vehicles_departed: number;
  vehicles_arrived: number;
  average_waiting_time_s?: number | null;
  average_queue_length_m?: number | null;
  average_travel_time_s?: number | null;
  emergency_vehicle_travel_time_s?: number | null;
  emergency_priority_response_time_s?: number | null;
  parking_occupancy_pct?: number | null;
  parking_search_time_s?: number | null;
  accepted_decisions: number;
  rejected_decisions: number;
  orchestrator_processing_latency_ms?: number | null;
  agent_availability_pct?: number | null;
  notes: string[];
}

export interface SystemStatus {
  running: boolean;
  simulation_time_s: number;
  config: Record<string, unknown>;
  agents: AgentStatus[];
  pending_events: number;
  decisions: number;
  metrics: SystemMetrics;
}

/* --------------------------------------------------------------- analytics */
export interface TrafficAnalytics {
  simulation_time_s: number;
  traffic?: TrafficState | null;
  average_waiting_time_s?: number | null;
  average_queue_length?: number | null;
  average_travel_time_s?: number | null;
  vehicles_departed: number;
  vehicles_arrived: number;
  congestion_events: number;
}

export interface ParkingAnalytics {
  parking?: ParkingState | null;
  occupancy_pct?: number | null;
  search_time_s?: number | null;
}

/* ------------------------------------------------------- simulation control */
export interface SimulationStartResponse {
  started: boolean;
  running: boolean;
  ended?: boolean;
  label?: string;
  sumocfg?: string;
  sim_time_s?: number | null;
  traffic_lights?: number;
  parking_areas?: number;
  applied_signal_actions?: number;
}

export interface SimulationStopResponse {
  started: boolean;
  running: boolean;
}

export interface SimulationStepResponse {
  simulation_time_s: number;
  decisions: OrchestratorDecision[];
}

/* --------------------------------------------------------------- api errors */
export interface ApiErrorBody {
  error?: {
    code?: string;
    message?: string;
    details?: unknown;
    [key: string]: unknown;
  };
  detail?: unknown;
}

/* ------------------------------------------------------------ network asset */
export interface NetworkJunction {
  id: string;
  x: number;
  y: number;
  type: string;
}

export interface NetworkZone {
  zone_id: string;
  name: string;
  capacity: number;
  x: number;
  y: number;
}

export interface NetworkData {
  source: string;
  bounds: { minX: number; maxX: number; minY: number; maxY: number };
  junctions: NetworkJunction[];
  tlsJunctions: string[];
  edges: Record<string, [number, number][]>;
  parkingZones: NetworkZone[];
  destinations: { id: string; name: string; x: number; y: number }[];
}
