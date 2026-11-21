# Traffora-AI Frontend — Architecture

This document describes the React dashboard that consumes the existing
orchestrator HTTP API. It is derived from a line-by-line reading of
`backend/orchestrator/api.py`, `backend/orchestrator/schemas.py` and
`backend/orchestrator/orchestrator.py`. Nothing here invents an endpoint,
field or event.

## 1. What the backend actually exposes

`create_app()` registers exactly 14 routes. There is **no WebSocket** endpoint,
no SSE endpoint and no `/network` geometry endpoint.

| Method | Path | Query | Response shape (verified) |
|---|---|---|---|
| GET | `/health` | – | `{ status: "ok" }` |
| GET | `/status` | – | `SystemStatus` |
| POST | `/simulation/start` | – | `{ started: true, running, ended, label, sumocfg, sim_time_s, traffic_lights, parking_areas, applied_signal_actions }` |
| POST | `/simulation/stop` | – | `{ started: false, running: false }` |
| POST | `/simulation/step` | `n` (1..600, default 1) | `{ simulation_time_s, decisions: OrchestratorDecision[] }` |
| GET | `/city-state` | – | `CityState` |
| GET | `/agents` | – | `AgentStatus[]` |
| GET | `/decisions/latest` | – | `OrchestratorDecision \| null` |
| GET | `/decisions` | `limit` (1..500, default 50) | `OrchestratorDecision[]` newest first |
| GET | `/events` | `limit` (1..1000, default 100) | `Event[]` oldest first |
| GET | `/emergency` | – | `EmergencyEvent[] + Event[]` (mixed array) |
| GET | `/analytics/traffic` | – | `{ simulation_time_s, traffic, average_waiting_time_s, average_queue_length, average_travel_time_s, vehicles_departed, vehicles_arrived, congestion_events }` |
| GET | `/analytics/parking` | – | `{ parking, occupancy_pct, search_time_s }` |
| GET | `/metrics` | – | `SystemMetrics` |

Error contract: `OrchestratorError` returns `{ error: { ... } }` with HTTP 409
when the simulation is not running and 400 otherwise. The client surfaces
`error.code` and `error.message`.

### Consequences for the UI

* **No WebSocket ⇒ polling.** A single `AppDataProvider` polls on an interval
  (2 s while running, 5 s while idle) and is the only place that talks to the
  API. Documented in `src/lib/config.ts`.
* **No geographic coordinates in the API.** `EmergencyEvent` carries `tls_id`,
  `route` (edge IDs) and `distance_m`/`eta_s`, but no lat/lng. The network
  geometry is therefore exported once from the real SUMO network
  (`simulation/green_corridor/sample_network/grid.net.xml`) into
  `src/data/network.json` by `frontend/scripts/export-network.mjs`. Parking
  coordinates come from `simulation/orchestrator/parking_data/zones.json`.
  The map renders those real planar coordinates with Leaflet `CRS.Simple` —
  no invented latitude or longitude anywhere.
* **`/emergency` mixes two shapes.** The client discriminates on the presence
  of `vehicle_id` (active vehicle) versus `event_id` + `event_type` (event).
* **Metrics are `null` until measured.** Every numeric card renders `—` for
  `null`/`undefined` instead of `0`, so nothing looks like a fabricated value.

## 2. Directory layout

```
frontend/
├── index.html
├── package.json
├── vite.config.ts
├── tailwind.config.js
├── postcss.config.js
├── tsconfig.json / tsconfig.node.json
├── scripts/export-network.mjs      # real geometry -> src/data/network.json
└── src/
    ├── main.tsx
    ├── App.tsx                     # router + layout shell
    ├── index.css                   # Tailwind layers + design tokens
    ├── lib/
    │   ├── config.ts               # API base URL, poll intervals
    │   ├── types.ts                # TS mirrors of the Pydantic schemas
    │   ├── api.ts                  # axios instance + error normalisation
    │   ├── services.ts             # one function per endpoint
    │   └── format.ts               # null-safe number/time formatters
    ├── hooks/
    │   ├── usePolling.ts           # interval + visibility-aware refresh
    │   └── useSimulation.ts        # start/stop/step with guard rails
    ├── context/AppDataContext.tsx  # single polling source of truth
    ├── components/
    │   ├── Layout.tsx  Sidebar.tsx  TopBar.tsx
    │   ├── Panel.tsx  StatCard.tsx  StatusPill.tsx
    │   ├── AsyncState.tsx          # loading / empty / error
    │   ├── SimulationControls.tsx
    │   └── charts/…                # Recharts wrappers
    └── pages/
        ├── Overview.tsx  CityMap.tsx  TrafficMind.tsx
        ├── GreenCorridor.tsx  ParkSense.tsx
        ├── Orchestrator.tsx  Analytics.tsx
```

## 3. Data flow

```
FastAPI ──HTTP──> services.ts ──> AppDataContext (poll) ──> pages/components
                                        │
                                   useSimulation (mutations, then refetch)
```

* `AppDataContext` holds `status`, `cityState`, `agents`, `decisions`,
  `events`, `emergency`, `trafficAnalytics`, `parkingAnalytics`, `metrics`,
  plus per-slice `error` and `loading`.
* Mutations (`start`, `stop`, `step`) go through `useSimulation`, which blocks
  duplicate requests, disables invalid buttons (start while running, stop/step
  while stopped) and surfaces the backend error body verbatim.
* Polling pauses when the tab is hidden and resumes on focus.

## 4. Pages and their real data sources

| Page | Sources |
|---|---|
| Overview | `/status`, `/metrics`, `/agents`, `/city-state` |
| City Map | `/city-state` (junction levels, emergency), `network.json`, `zones.json` |
| TrafficMind | `/analytics/traffic`, `/city-state`, `/decisions` (TRAFFIC_OPTIMIZATION) |
| GreenCorridor | `/emergency`, `/decisions` (EMERGENCY_PRIORITY), `/metrics` |
| ParkSense | `/analytics/parking`, `/city-state`, `/decisions` (PARKING_RECOMMENDATION) |
| Orchestrator | `/agents`, `/decisions`, `/events`, validation outcomes |
| Analytics | `/metrics`, `/analytics/traffic`, `/analytics/parking` |

## 5. Backend change (only one)

`CORSMiddleware` added to `create_app()`, driven by
`TRAFFORA_CORS_ORIGINS` (comma-separated). Defaults to the Vite dev origins
`http://localhost:5173` and `http://127.0.0.1:5173`. No wildcard, no static
mounting, no change to any route or agent.

## 6. Verification performed

Everything below was executed against a live backend and SUMO, not assumed.

| Check | Result |
|---|---|
| `npm run build` (`tsc -b && vite build`) | passes, no TypeScript errors |
| All 14 endpoints reachable | verified with `curl` |
| Overview, Map, TrafficMind, GreenCorridor, ParkSense, Orchestrator, Analytics | all render, zero console errors |
| Simulation start / step / stop from the UI | status pill flips Running ⇄ Stopped, poll rate changes 2 s ⇄ 5 s |
| Invalid action prevention | Start disabled while running; Stop/Step disabled while stopped |
| Backend unreachable | banner: "Cannot reach the orchestrator at …" + Retry, recovers automatically |
| `POST /simulation/start` failure | backend `SIMULATION_START_ERROR` body rendered verbatim |
| CORS preflight from `http://localhost:5173` | `200`, `access-control-allow-origin` echoed |
| CORS preflight from an unlisted origin | `400`, no allow-origin header |
| Emergency rendering | real `amb_1` from `priority.sumocfg`: route of 8 edges, `tls_id E3`, 217 m, ETA 16 s |
| Empty / idle state | every unmeasured metric renders `—`, never `0` |

Scenarios used: `simulation/orchestrator/unified.sumocfg` (default) and
`simulation/green_corridor/scenarios/priority.sumocfg` (to exercise the
emergency path, which the unified scenario does not produce).
