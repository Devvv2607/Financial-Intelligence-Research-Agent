import { API_URL } from "./api";

// A free-tier API may be asleep or still starting: the first request can take 30-60 s.
export const SLOW_MS = 5_000; // no answer by then: say so, instead of a blank page
export const RETRY_MS = 5_000;

/** One health probe; false on timeout, network error or a non-2xx answer. */
export async function probeApi(timeoutMs: number, signal?: AbortSignal): Promise<boolean> {
  try {
    const res = await fetch(`${API_URL}/health`, {
      cache: "no-store",
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(timeoutMs)]) : AbortSignal.timeout(timeoutMs),
    });
    return res.ok;
  } catch {
    return false;
  }
}

type Deps = {
  probe: (timeoutMs: number) => Promise<boolean>;
  sleep: (ms: number) => Promise<void>;
  onWaking: (attempt: number) => void; // the API didn't answer within SLOW_MS
  aborted: () => boolean;
};

/**
 * Probe until the API answers. The first probe gets SLOW_MS; if it fails, report "waking"
 * and keep probing (each probe waits up to RETRY_MS, with RETRY_MS between failures).
 * Resolves true once the API answered, false if aborted.
 */
export async function waitForApi({ probe, sleep, onWaking, aborted }: Deps): Promise<boolean> {
  if (await probe(SLOW_MS)) return true;
  for (let attempt = 1; !aborted(); attempt++) {
    onWaking(attempt);
    if (await probe(RETRY_MS)) return true;
    await sleep(RETRY_MS);
  }
  return false;
}
