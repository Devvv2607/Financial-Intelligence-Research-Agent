export default function Loading() {
  return (
    <main className="min-h-0 flex-1 overflow-y-auto" aria-busy="true" aria-label="Loading companies">
      <div className="mx-auto max-w-7xl space-y-6 p-4 md:p-6">
        <div className="space-y-2 border-b border-border pb-5">
          <div className="h-3 w-32 animate-pulse rounded bg-surface-2" />
          <div className="h-8 w-40 animate-pulse rounded bg-surface-2" />
          <div className="h-4 w-56 animate-pulse rounded bg-surface-2" />
        </div>
        <div className="h-10 max-w-md animate-pulse rounded-md bg-surface-2" />
        <div className="h-96 animate-pulse rounded-md border border-border bg-surface" />
      </div>
    </main>
  );
}
