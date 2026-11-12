import { createParser } from "eventsource-parser";

import { API_URL, toError } from "./api";
import type { AgentEvent } from "./types";

export type OnEvent = (event: AgentEvent, id: number) => void;

/**
 * Read an SSE response from the API (EventSource can't POST, so this uses fetch).
 * Resolves when the server ends the stream; rejects on HTTP errors or a dropped
 * connection, so the caller can reconnect from the last id it saw.
 */
export async function readStream(
  path: string,
  init: RequestInit,
  onEvent: OnEvent,
  signal: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    signal,
    headers: { "content-type": "application/json", accept: "text/event-stream" },
  });
  if (!res.ok || !res.body) throw await toError(res);
  const parser = createParser({
    onEvent: (msg) => {
      if (!msg.data) return;
      onEvent(JSON.parse(msg.data) as AgentEvent, Number(msg.id ?? 0));
    },
  });
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return;
    parser.feed(value);
  }
}

export const startRun = (threadId: string, query: string, onEvent: OnEvent, signal: AbortSignal) =>
  readStream(
    `/api/threads/${threadId}/runs`,
    { method: "POST", body: JSON.stringify({ query }) },
    onEvent,
    signal,
  );

export const resumeRun = (
  threadId: string,
  answer: string,
  onEvent: OnEvent,
  signal: AbortSignal,
) =>
  readStream(
    `/api/threads/${threadId}/resume`,
    { method: "POST", body: JSON.stringify({ answer }) },
    onEvent,
    signal,
  );

/** Replay events after `after`, then follow live (reconnects and reloads mid-run). */
export const followRun = (
  threadId: string,
  after: number,
  runId: string | undefined,
  onEvent: OnEvent,
  signal: AbortSignal,
) => {
  const q = new URLSearchParams({ after: String(after) });
  if (runId) q.set("run_id", runId);
  return readStream(`/api/threads/${threadId}/events?${q}`, { method: "GET" }, onEvent, signal);
};
