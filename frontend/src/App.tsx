import { Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Overview } from "./pages/Overview";
import { CityMap } from "./pages/CityMap";
import { TrafficMind } from "./pages/TrafficMind";
import { GreenCorridor } from "./pages/GreenCorridor";
import { ParkSense } from "./pages/ParkSense";
import { Orchestrator } from "./pages/Orchestrator";
import { Analytics } from "./pages/Analytics";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Overview />} />
        <Route path="/map" element={<CityMap />} />
        <Route path="/traffic" element={<TrafficMind />} />
        <Route path="/emergency" element={<GreenCorridor />} />
        <Route path="/parking" element={<ParkSense />} />
        <Route path="/orchestrator" element={<Orchestrator />} />
        <Route path="/analytics" element={<Analytics />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
