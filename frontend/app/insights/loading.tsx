export default function Loading() {
  return (
    <main className="min-h-0 flex-1 overflow-y-auto" aria-busy="true" aria-label="Loading insights">
      <div className="mx-auto max-w-7xl space-y-6 p-4 md:p-6">
        <div className="space-y-2 border-b border-border pb-5">
          <div className="h-3 w-40 animate-pulse rounded bg-surface-2" />
          <div className="h-8 w-32 animate-pulse rounded bg-surface-2" />
          <div className="h-4 w-72 animate-pulse rounded bg-surface-2" />
        </div>
        <div className="h-96 animate-pulse rounded-md border border-border bg-surface" />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {Array.from({ length: 7 }, (_, index) => <div key={index} className="h-28 animate-pulse border border-border bg-surface" />)}
        </div>
      </div>
    </main>
  );
}
