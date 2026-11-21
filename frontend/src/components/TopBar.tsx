import { Menu, RefreshCw, Wifi, WifiOff } from "lucide-react";
import { useAppData } from "../context/AppDataContext";
import { timeOfDay } from "../lib/format";
import { SimulationControls } from "./SimulationControls";

export function TopBar({ title, subtitle, onMenu }: { title: string; subtitle?: string; onMenu: () => void }) {
  const { error, refreshing, lastUpdated, refresh } = useAppData();

  return (
    <header className="sticky top-0 z-20 border-b border-white/5 bg-ink-950/85 backdrop-blur">
      <div className="flex flex-wrap items-center gap-3 px-4 py-3 sm:px-6">
        <button
          type="button"
          onClick={onMenu}
          className="rounded-lg border border-white/10 p-1.5 text-slate-300 hover:bg-white/5 lg:hidden"
          aria-label="Open navigation"
        >
          <Menu className="h-4 w-4" />
        </button>

        <div className="min-w-0 flex-1">
          <h1 className="truncate text-base font-semibold tracking-tight text-white sm:text-lg">
            {title}
          </h1>
          {subtitle && <p className="truncate text-xs text-slate-500">{subtitle}</p>}
        </div>

        <div className="flex items-center gap-2">
          <span
            className={`chip ${
              error
                ? "border-rose-500/30 bg-rose-500/10 text-rose-300"
                : "border-emerald-500/25 bg-emerald-500/10 text-emerald-300"
            }`}
            title={error ? error.message : "API reachable"}
          >
            {error ? <WifiOff className="h-3 w-3" aria-hidden /> : <Wifi className="h-3 w-3" aria-hidden />}
            {error ? "API error" : "Connected"}
          </span>

          <span className="hidden text-[11px] text-slate-500 sm:inline">
            updated {lastUpdated ? timeOfDay(new Date(lastUpdated).toISOString()) : "—"}
          </span>

          <button
            type="button"
            onClick={() => void refresh()}
            disabled={refreshing}
            className="rounded-lg border border-white/10 p-1.5 text-slate-300 hover:bg-white/5 disabled:opacity-50"
            aria-label="Refresh data"
            title="Refresh now"
          >
            <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>

      <div className="border-t border-white/5 px-4 py-2.5 sm:px-6">
        <SimulationControls compact />
      </div>
    </header>
  );
}
