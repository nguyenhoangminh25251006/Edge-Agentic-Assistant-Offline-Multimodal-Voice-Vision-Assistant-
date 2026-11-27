import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { TopNav } from "./components/TopNav";
import { DiagnosticsPage } from "./pages/DiagnosticsPage";
import { EventLogsPage } from "./pages/EventLogsPage";
import { LiveOverviewPage } from "./pages/LiveOverviewPage";
import { SettingsPage } from "./pages/SettingsPage";
import { HmiProvider, useHmi } from "./store/HmiContext";

function Shell() {
  const { system } = useHmi();
  return (
    <div className="min-h-screen bg-canvas text-ink">
      <TopNav />
      {system === null ? (
        <div className="flex h-[calc(100vh-56px)] items-center justify-center font-mono text-xs text-slate-500">
          Loading station configuration...
        </div>
      ) : (
        <Routes>
          <Route path="/" element={<LiveOverviewPage />} />
          <Route path="/logs" element={<EventLogsPage />} />
          <Route path="/diagnostics" element={<DiagnosticsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      )}
    </div>
  );
}

export function App() {
  return (
    <BrowserRouter>
      <HmiProvider>
        <Shell />
      </HmiProvider>
    </BrowserRouter>
  );
}

export default App;
