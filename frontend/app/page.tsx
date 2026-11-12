import { Suspense } from "react";

import { Workspace } from "@/components/workspace/workspace";

// The workspace reads ?thread= (a request-time value), so under Cache Components it
// renders inside a Suspense boundary; the fallback is the static shell.
export default function Home() {
  return (
    <Suspense fallback={<p className="p-6 text-sm text-muted">Loading the workspace…</p>}>
      <Workspace />
    </Suspense>
  );
}
