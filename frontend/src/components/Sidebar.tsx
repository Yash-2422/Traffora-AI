import { NavLink } from "react-router-dom";
import {
  Activity,
  BarChart3,
  Car,
  LayoutDashboard,
  Map as MapIcon,
  Network,
  Siren,
  X,
} from "lucide-react";
import { useAppData } from "../context/AppDataContext";
import { POLL_IDLE_MS, POLL_RUNNING_MS } from "../lib/config";

const NAV = [
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/map", label: "City Map", icon: MapIcon },
  { to: "/traffic", label: "TrafficMind", icon: Activity },
  { to: "/emergency", label: "GreenCorridor", icon: Siren },
  { to: "/parking", label: "ParkSense", icon: Car },
  { to: "/orchestrator", label: "Orchestrator", icon: Network },
  { to: "/analytics", label: "Analytics", icon: BarChart3 },
];

export function Sidebar({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { status, emergency, pollIntervalMs } = useAppData();
  const running = Boolean(status?.running);
  const activeEmergencies = emergency.vehicles.length;

  return (
    <>
      {/* Mobile scrim */}
      <div
        className={`fixed inset-0 z-30 bg-black/60 backdrop-blur-sm transition-opacity lg:hidden ${
          open ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
        onClick={onClose}
        aria-hidden
      />

      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-64 flex-col border-r border-white/5 bg-ink-900/95 backdrop-blur
          transition-transform duration-200 lg:static lg:translate-x-0 ${
            open ? "translate-x-0" : "-translate-x-full"
          }`}
      >
        <div className="flex items-center gap-2.5 border-b border-white/5 px-4 py-4">
          <div className="grid h-9 w-9 place-items-center rounded-lg bg-accent/15 ring-1 ring-accent/30">
            <Network className="h-5 w-5 text-accent" aria-hidden />
          </div>
          <div className="min-w-0">
            <p className="truncate text-sm font-bold tracking-tight text-white">Traffora-AI</p>
            <p className="truncate text-[11px] text-slate-500">Smart City Control Center</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto rounded p-1 text-slate-400 hover:bg-white/5 lg:hidden"
            aria-label="Close navigation"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <nav className="flex-1 space-y-1 overflow-y-auto p-2.5">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              onClick={onClose}
              className={({ isActive }) =>
                `flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors ${
                  isActive
                    ? "bg-accent/10 font-semibold text-accent ring-1 ring-inset ring-accent/20"
                    : "text-slate-400 hover:bg-white/[0.04] hover:text-slate-200"
                }`
              }
            >
              <Icon className="h-4 w-4 shrink-0" aria-hidden />
              <span className="truncate">{label}</span>
              {to === "/emergency" && activeEmergencies > 0 && (
                <span className="ml-auto rounded-full bg-rose-500/20 px-1.5 py-0.5 text-[10px] font-bold text-rose-300">
                  {activeEmergencies}
                </span>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="border-t border-white/5 p-3">
          <div className="rounded-lg border border-white/5 bg-white/[0.02] p-3">
            <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-500">
              Orchestrator
            </p>
            <div className="mt-2 flex items-center gap-2">
              <span
                className={`h-2 w-2 rounded-full ${running ? "bg-emerald-400" : "bg-slate-600"}`}
                aria-hidden
              />
              <span className="text-xs text-slate-300">{running ? "Simulation live" : "Idle"}</span>
            </div>
            <p className="mt-1.5 text-[10px] leading-relaxed text-slate-500">
              Polling every {pollIntervalMs / 1000}s · no WebSocket available
            </p>
            <p className="mt-1 text-[10px] text-slate-600">
              {running ? `${POLL_RUNNING_MS / 1000}s live` : `${POLL_IDLE_MS / 1000}s idle`} refresh
            </p>
          </div>
        </div>
      </aside>
    </>
  );
}
