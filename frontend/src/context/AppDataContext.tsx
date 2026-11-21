import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { POLL_IDLE_MS, POLL_RUNNING_MS } from "../lib/config";
import { ApiError } from "../lib/api";
import {
  getAgents,
  getCityState,
  getDecisions,
  getEmergency,
  getEvents,
  getMetrics,
  getParkingAnalytics,
  getStatus,
  getTrafficAnalytics,
  type EmergencyPayload,
} from "../lib/services";
import type {
  AgentStatus,
  CityState,
  OrchestratorDecision,
  OrchestratorEvent,
  ParkingAnalytics,
  SystemMetrics,
  SystemStatus,
  TrafficAnalytics,
} from "../lib/types";

interface AppDataValue {
  status: SystemStatus | null;
  cityState: CityState | null;
  agents: AgentStatus[];
  decisions: OrchestratorDecision[];
  events: OrchestratorEvent[];
  emergency: EmergencyPayload;
  traffic: TrafficAnalytics | null;
  parking: ParkingAnalytics | null;
  metrics: SystemMetrics | null;

  loading: boolean;
  refreshing: boolean;
  error: ApiError | null;
  lastUpdated: number | null;
  pollIntervalMs: number;
  refresh: () => Promise<void>;
}

const AppDataContext = createContext<AppDataValue | null>(null);

const EMPTY_EMERGENCY: EmergencyPayload = { vehicles: [], events: [] };

/**
 * The single polling source of truth for the dashboard.
 *
 * Every endpoint is fetched in parallel with `Promise.allSettled`, so one
 * failing slice (for example `/emergency` returning 500 while the rest works)
 * does not blank the whole page. The first error encountered is surfaced.
 */
export function AppDataProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [cityState, setCityState] = useState<CityState | null>(null);
  const [agents, setAgents] = useState<AgentStatus[]>([]);
  const [decisions, setDecisions] = useState<OrchestratorDecision[]>([]);
  const [events, setEvents] = useState<OrchestratorEvent[]>([]);
  const [emergency, setEmergency] = useState<EmergencyPayload>(EMPTY_EMERGENCY);
  const [traffic, setTraffic] = useState<TrafficAnalytics | null>(null);
  const [parking, setParking] = useState<ParkingAnalytics | null>(null);
  const [metrics, setMetrics] = useState<SystemMetrics | null>(null);

  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);

  const inFlight = useRef(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setRefreshing(true);

    const results = await Promise.allSettled([
      getStatus(),
      getCityState(),
      getAgents(),
      getDecisions(),
      getEvents(),
      getEmergency(),
      getTrafficAnalytics(),
      getParkingAnalytics(),
      getMetrics(),
    ]);

    if (!mounted.current) {
      inFlight.current = false;
      return;
    }

    let firstError: ApiError | null = null;
    const note = (index: number) => {
      const result = results[index];
      if (result.status === "rejected" && !firstError) {
        firstError =
          result.reason instanceof ApiError ? result.reason : new ApiError(String(result.reason));
      }
    };

    note(0);
    if (results[0].status === "fulfilled") setStatus(results[0].value);
    note(1);
    if (results[1].status === "fulfilled") setCityState(results[1].value);
    note(2);
    if (results[2].status === "fulfilled") setAgents(results[2].value);
    note(3);
    if (results[3].status === "fulfilled") setDecisions(results[3].value);
    note(4);
    if (results[4].status === "fulfilled") setEvents(results[4].value);
    note(5);
    if (results[5].status === "fulfilled") setEmergency(results[5].value);
    note(6);
    if (results[6].status === "fulfilled") setTraffic(results[6].value);
    note(7);
    if (results[7].status === "fulfilled") setParking(results[7].value);
    note(8);
    if (results[8].status === "fulfilled") setMetrics(results[8].value);

    setError(firstError);
    setLastUpdated(Date.now());
    setLoading(false);
    setRefreshing(false);
    inFlight.current = false;
  }, []);

  // Poll faster while the simulation is running, slower while it is stopped.
  const pollIntervalMs = status?.running ? POLL_RUNNING_MS : POLL_IDLE_MS;

  useEffect(() => {
    void load();
    const id = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      void load();
    }, pollIntervalMs);

    const onVisible = () => {
      if (document.visibilityState === "visible") void load();
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load, pollIntervalMs]);

  const value = useMemo<AppDataValue>(
    () => ({
      status,
      cityState,
      agents,
      decisions,
      events,
      emergency,
      traffic,
      parking,
      metrics,
      loading,
      refreshing,
      error,
      lastUpdated,
      pollIntervalMs,
      refresh: load,
    }),
    [
      status,
      cityState,
      agents,
      decisions,
      events,
      emergency,
      traffic,
      parking,
      metrics,
      loading,
      refreshing,
      error,
      lastUpdated,
      pollIntervalMs,
      load,
    ]
  );

  return <AppDataContext.Provider value={value}>{children}</AppDataContext.Provider>;
}

export function useAppData(): AppDataValue {
  const context = useContext(AppDataContext);
  if (!context) throw new Error("useAppData must be used inside <AppDataProvider>");
  return context;
}
