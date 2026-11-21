import { useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";

const TITLES: Record<string, { title: string; subtitle: string }> = {
  "/": {
    title: "Overview",
    subtitle: "Live system state from the Traffora-AI orchestrator",
  },
  "/map": {
    title: "City Traffic Map",
    subtitle: "SUMO network geometry · junction congestion · parking · emergency vehicles",
  },
  "/traffic": {
    title: "TrafficMind",
    subtitle: "Congestion analytics, junction status and signal decisions",
  },
  "/emergency": {
    title: "GreenCorridor",
    subtitle: "Emergency detection, priority requests and preemption outcomes",
  },
  "/parking": {
    title: "ParkSense",
    subtitle: "Parking occupancy, availability and recommendations",
  },
  "/orchestrator": {
    title: "Orchestrator",
    subtitle: "Agent health, decision log, events and safety validation",
  },
  "/analytics": {
    title: "Analytics",
    subtitle: "Measured metrics only — nothing estimated",
  },
};

export function Layout() {
  const [navOpen, setNavOpen] = useState(false);
  const location = useLocation();
  const meta = TITLES[location.pathname] ?? { title: "Traffora-AI", subtitle: "" };

  return (
    <div className="flex min-h-screen bg-ink-950">
      <Sidebar open={navOpen} onClose={() => setNavOpen(false)} />

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar title={meta.title} subtitle={meta.subtitle} onMenu={() => setNavOpen(true)} />

        <main className="flex-1 px-4 py-5 sm:px-6">
          <Outlet />
        </main>

        <footer className="border-t border-white/5 px-4 py-3 text-[11px] text-slate-600 sm:px-6">
          Traffora-AI · multi-agent urban mobility · emergency preemption P1, traffic optimisation
          P2, parking advice P3 · every signal action passes an independent safety validator
        </footer>
      </div>
    </div>
  );
}
