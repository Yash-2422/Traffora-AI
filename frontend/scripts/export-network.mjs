#!/usr/bin/env node
/**
 * Export real SUMO network geometry for the map.
 *
 * The orchestrator API deliberately exposes no geometry: it returns junction
 * IDs, edge IDs and TLS IDs, never coordinates. Rather than invent positions,
 * this script reads the *actual* network the unified scenario runs on
 * (simulation/green_corridor/sample_network/grid.net.xml) plus the parking
 * zone file the orchestrator loads, and writes a small JSON asset.
 *
 * Usage: node scripts/export-network.mjs
 */
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../..");
const netPath = resolve(repoRoot, "simulation/green_corridor/sample_network/grid.net.xml");
const zonesPath = resolve(repoRoot, "simulation/orchestrator/parking_data/zones.json");
const outDir = resolve(here, "../src/data");

const net = readFileSync(netPath, "utf8");

/** Minimal, dependency-free attribute reader. */
const attr = (tag, name) => {
  const m = tag.match(new RegExp(`${name}="([^"]*)"`));
  return m ? m[1] : null;
};

// ---- junctions -----------------------------------------------------------
const junctions = [];
for (const m of net.matchAll(/<junction\s([^>]*?)>/g)) {
  const tag = m[1];
  const id = attr(tag, "id");
  if (!id || id.startsWith(":")) continue; // internal junctions are not useful
  const x = Number(attr(tag, "x"));
  const y = Number(attr(tag, "y"));
  if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
  junctions.push({ id, x, y, type: attr(tag, "type") || "priority" });
}

// ---- edges (for emergency route polylines) -------------------------------
const edges = {};
for (const m of net.matchAll(/<edge\s([^>]*?)>([\s\S]*?)<\/edge>/g)) {
  const tag = m[1];
  const id = attr(tag, "id");
  const fn = attr(tag, "function");
  if (!id || fn === "internal") continue;
  // Plain edges carry no shape attribute; the geometry lives on their lanes.
  const shape = attr(tag, "shape") || attr(m[2].match(/<lane\s[^>]*>/)?.[0] || "", "shape");
  if (!shape) continue;
  const points = shape
    .trim()
    .split(/\s+/)
    .map((pair) => pair.split(",").map(Number))
    .filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y));
  if (points.length >= 2) edges[id] = points;
}

// ---- traffic light junction ids -----------------------------------------
const tlsJunctions = junctions.filter((j) => j.type === "traffic_light").map((j) => j.id);

// ---- parking zones -------------------------------------------------------
const zoneFile = JSON.parse(readFileSync(zonesPath, "utf8"));
const parkingZones = (zoneFile.zones || []).map((z) => ({
  zone_id: z.zone_id,
  name: z.name,
  capacity: z.capacity,
  x: z.x,
  y: z.y,
}));
const destinations = Object.entries(zoneFile.destinations || {}).map(([key, d]) => ({
  id: key,
  name: d.name,
  x: d.x,
  y: d.y,
}));

const xs = junctions.map((j) => j.x);
const ys = junctions.map((j) => j.y);
const bounds = {
  minX: Math.min(...xs),
  maxX: Math.max(...xs),
  minY: Math.min(...ys),
  maxY: Math.max(...ys),
};

const payload = {
  source: "simulation/green_corridor/sample_network/grid.net.xml",
  bounds,
  junctions,
  tlsJunctions,
  edges,
  parkingZones,
  destinations,
};

mkdirSync(outDir, { recursive: true });
writeFileSync(resolve(outDir, "network.json"), JSON.stringify(payload));

console.log(
  `exported ${junctions.length} junctions, ${Object.keys(edges).length} edges, ` +
    `${parkingZones.length} parking zones -> src/data/network.json`
);
