"use client";

import { AlertTriangle } from "lucide-react";

export default function Error({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <main className="flex min-h-0 flex-1 items-center justify-center p-6">
      <div className="max-w-md border border-border bg-surface p-6 text-center">
        <AlertTriangle className="mx-auto size-5 text-bad" aria-hidden />
        <h1 className="mt-3 text-lg font-semibold">Insights unavailable</h1>
        <p className="mt-2 text-sm text-muted">Risk and ingestion data could not be loaded from the API.</p>
        <button type="button" onClick={reset} className="mt-5 rounded-md border border-border px-3 py-2 text-sm hover:border-accent">
          Try again
        </button>
      </div>
    </main>
  );
}
