import { SystemStatus } from "@/components/system-status";

// Phase 0 placeholder for the research workspace: proves the web container can
// reach the API. Replaced by the full workspace in Phase 5.
export default function Home() {
  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col justify-center gap-8 px-4 py-16">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">Financial Research Agent</h1>
        <p className="text-muted">
          Plans, runs tools in parallel, checks its own work, asks when unsure, and cites
          everything.
        </p>
      </header>
      <SystemStatus />
    </main>
  );
}
