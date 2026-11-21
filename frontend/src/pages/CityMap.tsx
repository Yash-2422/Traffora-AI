import { useMemo, useState } from "react";
import { CircleMarker, MapContainer, Polyline, Popup, Tooltip, useMap } from "react-leaflet";
import L from "leaflet";
import { Car, Info, Siren } from "lucide-react";
import networkJson from "../data/network.json";
import type { NetworkData } from "../lib/types";
import { useAppData } from "../context/AppDataContext";
import { Panel } from "../components/Panel";
import { StatusPill, levelTone } from "../components/StatusPill";
import { EmptyBlock } from "../components/AsyncState";
import { EMPTY, int, isNumber, num, pct, titleCase } from "../lib/format";

const network = networkJson as unknown as NetworkData;

const LEVEL_COLORS: Record<string, string> = {
  HIGH: "#f43f5e",
  MEDIUM: "#f59e0b",
  LOW: "#10b981",
};

const DEFAULT_COLOR = "#475569";

/**
 * The SUMO network is a planar coordinate system, not latitude/longitude.
 * Leaflet's CRS.Simple is used with `latLng = [y, x]` so the geometry is drawn
 * exactly as authored. No geographic projection is invented.
 */
const bounds = L.latLngBounds(
  [network.bounds.minY, network.bounds.minX],
  [network.bounds.maxY, network.bounds.maxX]
);

function FitBounds() {
  const map = useMap();
  useMemo(() => {
    map.fitBounds(bounds, { padding: [24, 24] });
  }, [map]);
  return null;
}

export function CityMap() {
  const { cityState, parking, loading, error } = useAppData();
  const [showEdges, setShowEdges] = useState(true);

  const traffic = cityState?.traffic ?? null;
  const levels = traffic?.junction_levels ?? {};
  const emergencies = cityState?.emergency_vehicles ?? [];
  const zones = parking?.parking?.zones ?? cityState?.parking?.zones ?? [];

  const zoneById = useMemo(() => {
    const map = new Map<string, (typeof zones)[number]>();
    for (const zone of zones) map.set(zone.zone_id, zone);
    return map;
  }, [zones]);

  const junctionById = useMemo(() => {
    const map = new Map<string, NetworkData["junctions"][number]>();
    for (const junction of network.junctions) map.set(junction.id, junction);
    return map;
  }, []);

  const emergencyMarkers = emergencies
    .map((vehicle) => {
      const junction = vehicle.tls_id ? junctionById.get(vehicle.tls_id) : undefined;
      return junction ? { vehicle, junction } : null;
    })
    .filter((value): value is { vehicle: (typeof emergencies)[number]; junction: NetworkData["junctions"][number] } => value !== null);

  const emergencyRoutes = emergencies
    .map((vehicle) => {
      const points = vehicle.route
        .map((edgeId) => network.edges[edgeId])
        .filter((shape): shape is [number, number][] => Array.isArray(shape));
      const flat = points.flat().map(([x, y]) => [y, x] as [number, number]);
      return { vehicle, points: flat };
    })
    .filter((entry) => entry.points.length >= 2);

  const congestedCount = traffic?.congested_junctions.length ?? 0;

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
      <Panel
        title="SUMO network · planar coordinates"
        subtitle={`${network.junctions.length} junctions · ${network.tlsJunctions.length} signalised · source: ${network.source}`}
        flush
        actions={
          <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-slate-400">
            <input
              type="checkbox"
              checked={showEdges}
              onChange={(event) => setShowEdges(event.target.checked)}
              className="h-3 w-3 accent-cyan-400"
            />
            Road geometry
          </label>
        }
        bodyClassName="p-0"
      >
        <div className="relative h-[520px] w-full overflow-hidden rounded-b-xl sm:h-[620px]">
          <MapContainer
            crs={L.CRS.Simple}
            bounds={bounds}
            minZoom={-3}
            maxZoom={3}
            zoomSnap={0.25}
            attributionControl={false}
            className="h-full w-full"
            style={{ background: "#070b14" }}
          >
            <FitBounds />

            {showEdges &&
              Object.entries(network.edges).map(([id, shape]) => (
                <Polyline
                  key={id}
                  positions={shape.map(([x, y]) => [y, x] as [number, number])}
                  pathOptions={{ color: "#1e293b", weight: 2, opacity: 0.9 }}
                />
              ))}

            {/* Emergency routes (real edge IDs from the backend) */}
            {emergencyRoutes.map(({ vehicle, points }) => (
              <Polyline
                key={`route-${vehicle.event_id}`}
                positions={points}
                pathOptions={{ color: "#f43f5e", weight: 4, opacity: 0.85, dashArray: "8 6" }}
              >
                <Tooltip sticky>{vehicle.vehicle_id} route ({vehicle.route.length} edges)</Tooltip>
              </Polyline>
            ))}

            {/* Junctions coloured by the backend's congestion level */}
            {network.junctions.map((junction) => {
              const level = levels[junction.id];
              const isTls = junction.type === "traffic_light";
              const color = level ? (LEVEL_COLORS[level] ?? DEFAULT_COLOR) : DEFAULT_COLOR;
              return (
                <CircleMarker
                  key={junction.id}
                  center={[junction.y, junction.x]}
                  radius={isTls ? 7 : 4}
                  pathOptions={{
                    color,
                    fillColor: color,
                    fillOpacity: level ? 0.85 : 0.35,
                    weight: isTls ? 2 : 1,
                  }}
                >
                  <Tooltip direction="top" offset={[0, -6]}>
                    <span className="font-mono text-[11px]">{junction.id}</span>
                    {isTls ? " · TLS" : ""}
                    {level ? ` · ${level}` : " · no data"}
                  </Tooltip>
                  <Popup>
                    <div className="space-y-1">
                      <p className="font-semibold">{junction.id}</p>
                      <p>Type: {junction.type}</p>
                      <p>Congestion: {level ? titleCase(level) : "not reported"}</p>
                      <p className="font-mono text-[10px] text-slate-400">
                        x={junction.x} y={junction.y}
                      </p>
                    </div>
                  </Popup>
                </CircleMarker>
              );
            })}

            {/* Parking zones: coordinates from the orchestrator's zones.json */}
            {network.parkingZones.map((zone) => {
              const live = zoneById.get(zone.zone_id);
              const occupancy = live?.occupancy_pct;
              const color = !isNumber(occupancy)
                ? "#64748b"
                : occupancy >= 90
                  ? "#f43f5e"
                  : occupancy >= 70
                    ? "#f59e0b"
                    : "#22d3ee";
              return (
                <CircleMarker
                  key={zone.zone_id}
                  center={[zone.y, zone.x]}
                  radius={9}
                  pathOptions={{ color, fillColor: color, fillOpacity: 0.35, weight: 2 }}
                >
                  <Tooltip direction="top" offset={[0, -8]}>
                    {zone.name} · {isNumber(occupancy) ? `${occupancy.toFixed(0)}%` : "no data"}
                  </Tooltip>
                  <Popup>
                    <div className="space-y-1">
                      <p className="font-semibold">{zone.name}</p>
                      <p>Zone: {zone.zone_id}</p>
                      <p>Capacity: {zone.capacity}</p>
                      <p>Occupied: {live ? live.occupied : EMPTY}</p>
                      <p>Available: {live ? live.available : EMPTY}</p>
                      <p>Occupancy: {isNumber(occupancy) ? `${occupancy.toFixed(1)}%` : "not reported"}</p>
                    </div>
                  </Popup>
                </CircleMarker>
              );
            })}

            {/* Destinations (hospital POI from zones.json) */}
            {network.destinations.map((destination) => (
              <CircleMarker
                key={destination.id}
                center={[destination.y, destination.x]}
                radius={6}
                pathOptions={{ color: "#a78bfa", fillColor: "#a78bfa", fillOpacity: 0.5, weight: 2 }}
              >
                <Tooltip direction="top" offset={[0, -6]}>{destination.name}</Tooltip>
              </CircleMarker>
            ))}

            {/* Emergency vehicles: plotted at their next signalised junction */}
            {emergencyMarkers.map(({ vehicle, junction }) => (
              <CircleMarker
                key={vehicle.event_id}
                center={[junction.y, junction.x]}
                radius={13}
                pathOptions={{ color: "#fb7185", fillColor: "#f43f5e", fillOpacity: 0.5, weight: 3 }}
              >
                <Tooltip direction="top" offset={[0, -10]} permanent>
                  <span className="text-[10px] font-bold text-rose-600">{vehicle.vehicle_id}</span>
                </Tooltip>
                <Popup>
                  <div className="space-y-1">
                    <p className="font-semibold">{vehicle.vehicle_id}</p>
                    <p>Next signal: {vehicle.tls_id ?? EMPTY}</p>
                    <p>Distance: {isNumber(vehicle.distance_m) ? `${vehicle.distance_m.toFixed(0)} m` : EMPTY}</p>
                    <p>ETA: {isNumber(vehicle.eta_s) ? `${vehicle.eta_s.toFixed(0)} s` : EMPTY}</p>
                    <p>Route edges: {vehicle.route.length}</p>
                  </div>
                </Popup>
              </CircleMarker>
            ))}
          </MapContainer>

          {/* Legend */}
          <div className="pointer-events-none absolute bottom-3 left-3 z-[400] rounded-lg border border-white/10 bg-ink-950/85 p-2.5 backdrop-blur">
            <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-400">
              Legend
            </p>
            <ul className="space-y-1 text-[10px] text-slate-300">
              <LegendItem color="#10b981" label="Low congestion" />
              <LegendItem color="#f59e0b" label="Medium congestion" />
              <LegendItem color="#f43f5e" label="High congestion / emergency" />
              <LegendItem color="#22d3ee" label="Parking zone" />
              <LegendItem color="#a78bfa" label="Destination (hospital)" />
            </ul>
          </div>
        </div>
      </Panel>

      <div className="space-y-4">
        <Panel title="Map data sources" subtitle="No coordinates are fabricated">
          <ul className="space-y-2 text-[11px] leading-relaxed text-slate-400">
            <li className="flex gap-2">
              <Info className="mt-0.5 h-3 w-3 shrink-0 text-accent" aria-hidden />
              Junction and road geometry is exported from the real SUMO network
              <span className="font-mono text-slate-500">grid.net.xml</span> by
              <span className="font-mono text-slate-500"> scripts/export-network.mjs</span>.
            </li>
            <li className="flex gap-2">
              <Info className="mt-0.5 h-3 w-3 shrink-0 text-accent" aria-hidden />
              Congestion colours come from <span className="font-mono">city-state.traffic.junction_levels</span>.
            </li>
            <li className="flex gap-2">
              <Info className="mt-0.5 h-3 w-3 shrink-0 text-accent" aria-hidden />
              Parking positions come from the orchestrator's
              <span className="font-mono"> parking_data/zones.json</span>; occupancy from the API.
            </li>
            <li className="flex gap-2">
              <Info className="mt-0.5 h-3 w-3 shrink-0 text-accent" aria-hidden />
              The API does not expose vehicle x/y. Emergency vehicles are therefore drawn at their
              <em> next signalised junction</em>, with the route polyline built from the reported
              edge IDs.
            </li>
          </ul>
        </Panel>

        <Panel
          title="Congestion by junction"
          subtitle="city-state.traffic.junction_levels"
          actions={<StatusPill tone={levelTone(traffic?.congestion_level)}>{titleCase(traffic?.congestion_level ?? "no data")}</StatusPill>}
        >
          {Object.keys(levels).length === 0 ? (
            <EmptyBlock
              title="No junction levels reported"
              description="Start the simulation and advance it so TrafficMind can publish congestion levels."
            />
          ) : (
            <div className="max-h-64 space-y-1 overflow-y-auto pr-1">
              {Object.entries(levels)
                .sort((a, b) => {
                  const rank = (level: string) => (level === "HIGH" ? 0 : level === "MEDIUM" ? 1 : 2);
                  return rank(a[1]) - rank(b[1]) || a[0].localeCompare(b[0]);
                })
                .map(([junction, level]) => (
                  <div
                    key={junction}
                    className="flex items-center justify-between rounded-md border border-white/5 bg-white/[0.02] px-2 py-1.5"
                  >
                    <span className="font-mono text-xs text-slate-300">{junction}</span>
                    <StatusPill tone={levelTone(level)}>{titleCase(level)}</StatusPill>
                  </div>
                ))}
            </div>
          )}
          <p className="mt-2 text-[10px] text-slate-500">
            {congestedCount} junction{congestedCount === 1 ? "" : "s"} at MEDIUM or HIGH
          </p>
        </Panel>

        <Panel title="Parking zones" subtitle="city-state.parking.zones">
          {network.parkingZones.length === 0 ? (
            <p className="text-xs text-slate-500">No parking zones in the network file.</p>
          ) : (
            <ul className="space-y-1.5">
              {network.parkingZones.map((zone) => {
                const live = zoneById.get(zone.zone_id);
                return (
                  <li
                    key={zone.zone_id}
                    className="flex items-center justify-between gap-2 rounded-md border border-white/5 bg-white/[0.02] px-2 py-1.5"
                  >
                    <span className="flex min-w-0 items-center gap-1.5">
                      <Car className="h-3 w-3 shrink-0 text-accent" aria-hidden />
                      <span className="truncate text-xs text-slate-300">{zone.name}</span>
                    </span>
                    <span className="shrink-0 font-mono text-[10px] text-slate-400">
                      {live ? `${live.available}/${live.capacity}` : "—"}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </Panel>

        <Panel
          title="Emergency vehicles"
          subtitle="city-state.emergency_vehicles"
          actions={
            <StatusPill tone={emergencies.length ? "bad" : "neutral"} dot>
              {emergencies.length} active
            </StatusPill>
          }
        >
          {emergencies.length === 0 ? (
            <p className="text-xs text-slate-500">
              No active emergency vehicles{loading ? " (loading…)" : ""}.
              {error ? ` ${error.message}` : ""}
            </p>
          ) : (
            <ul className="space-y-2">
              {emergencies.map((vehicle) => (
                <li
                  key={vehicle.event_id}
                  className="rounded-md border border-rose-500/20 bg-rose-500/[0.06] p-2"
                >
                  <div className="flex items-center justify-between">
                    <span className="flex items-center gap-1.5 text-xs font-semibold text-rose-200">
                      <Siren className="h-3 w-3" aria-hidden />
                      {vehicle.vehicle_id}
                    </span>
                    <span className="font-mono text-[10px] text-rose-300/80">
                      {vehicle.tls_id ?? "no TLS"}
                    </span>
                  </div>
                  <p className="mt-1 font-mono text-[10px] text-slate-400">
                    {num(vehicle.distance_m, 0)} m · ETA {num(vehicle.eta_s, 0)} s ·{" "}
                    {vehicle.route.length} edges
                  </p>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Network summary" subtitle="Exported geometry">
          <dl className="space-y-1.5 text-xs">
            <SummaryRow label="Junctions" value={int(network.junctions.length)} />
            <SummaryRow label="Signalised" value={int(network.tlsJunctions.length)} />
            <SummaryRow label="Road edges" value={int(Object.keys(network.edges).length)} />
            <SummaryRow label="Parking zones" value={int(network.parkingZones.length)} />
            <SummaryRow
              label="Extent"
              value={`${network.bounds.maxX - network.bounds.minX} × ${
                network.bounds.maxY - network.bounds.minY
              } m`}
            />
            <SummaryRow label="Overall occupancy" value={pct(parking?.occupancy_pct)} />
          </dl>
        </Panel>
      </div>
    </div>
  );
}

function LegendItem({ color, label }: { color: string; label: string }) {
  return (
    <li className="flex items-center gap-1.5">
      <span className="h-2 w-2 rounded-full" style={{ backgroundColor: color }} aria-hidden />
      {label}
    </li>
  );
}

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <dt className="text-slate-500">{label}</dt>
      <dd className="font-mono text-slate-300">{value}</dd>
    </div>
  );
}
