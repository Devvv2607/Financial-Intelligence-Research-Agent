export default function Loading() {
  return (
    <main className="min-h-0 flex-1 overflow-y-auto" aria-busy="true" aria-label="Loading company detail">
      <div className="mx-auto max-w-7xl space-y-6 p-4 md:p-6">
        <div className="h-32 animate-pulse border-b border-border bg-surface-2" />
        <div className="h-40 animate-pulse border border-border bg-surface-2" />
        <div className="h-80 animate-pulse border border-border bg-surface-2" />
      </div>
    </main>
  );
}
