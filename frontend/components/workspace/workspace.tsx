"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import { useRun } from "@/lib/use-run";

import { AnswerMarkdown } from "./answer-markdown";
import { AssistantBlock } from "./assistant-block";
import { Composer, ExampleChips } from "./composer";
import { RunPanel, type Tab } from "./run-panel";
import { ThreadList } from "./thread-list";

/** Research workspace: threads | conversation | run panel (Plan · Graph · Sources). */
export function Workspace() {
  const router = useRouter();
  const threadId = useSearchParams().get("thread");
  const { run, connection, failure, start, resume, load, reset } = useRun();
  const [pending, setPending] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("plan");
  const [highlight, setHighlight] = useState<number | null>(null);
  const [mobile, setMobile] = useState<"chat" | "run">("chat");
  const opened = useRef<string | null>(null); // the thread this view already drives

  const detail = useQuery({
    queryKey: ["thread", threadId],
    queryFn: () => api.getThread(threadId!),
    enabled: !!threadId,
  });

  useEffect(() => {
    if (threadId === opened.current) return;
    opened.current = threadId;
    setPending(null);
    setHighlight(null);
    if (threadId) void load(threadId);
    else reset();
  }, [threadId, load, reset]);

  const busy = !!run.runId && !["done", "error", "paused"].includes(run.phase);
  const ask = async (q: string) => {
    let id = threadId;
    if (!id) {
      id = (await api.createThread()).id;
      opened.current = id; // don't reload the thread we are about to drive
      router.push(`/?thread=${id}`);
    }
    setPending(q);
    setTab("plan");
    void start(q, id);
  };
  const cite = (n: number) => {
    setHighlight(n);
    setTab("sources");
    setMobile("run");
  };

  // Past turns come from the thread; the latest assistant turn is the live run.
  const messages = detail.data?.messages ?? [];
  const lastAssistant = messages.findLastIndex((m) => m.role === "assistant");
  const showRun = !!run.runId || run.phase !== "starting";
  const history =
    showRun && run.final && lastAssistant >= 0 && messages[lastAssistant].content === run.final.answer
      ? messages.slice(0, lastAssistant)
      : messages;
  const lastUser = [...history].reverse().find((m) => m.role === "user")?.content;
  const empty = !threadId && !pending;

  return (
    <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[248px_minmax(0,1fr)_420px]">
      <div className="hidden min-h-0 lg:flex lg:flex-col">
        <ThreadList current={threadId} onSelect={(id) => router.push(`/?thread=${id}`)} onNew={() => router.push("/")} />
      </div>

      <main className={`min-h-0 flex-col ${mobile === "chat" ? "flex" : "hidden lg:flex"}`}>
        <div className="flex shrink-0 gap-2 border-b border-border p-2 text-xs lg:hidden">
          <button type="button" className="rounded border border-border px-2 py-1" onClick={() => router.push("/")}>New</button>
          <button type="button" className="ml-auto rounded border border-border px-2 py-1" onClick={() => setMobile("run")}>Plan · Graph · Sources</button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-4">
          {empty ? (
            <ExampleChips onPick={ask} />
          ) : (
            <div className="mx-auto max-w-[760px] space-y-5 py-6">
              {detail.isError && <p className="text-sm text-bad">Couldn&apos;t load this thread.</p>}
              {history.map((m, i) =>
                m.role === "user" ? (
                  <p key={i} className="rounded-md bg-surface-2 px-3 py-2 text-sm">{m.content}</p>
                ) : (
                  <div key={i} className="text-muted"><AnswerMarkdown text={m.content} onCite={() => undefined} /></div>
                ),
              )}
              {pending && pending !== lastUser && (
                <p className="rounded-md bg-surface-2 px-3 py-2 text-sm">{pending}</p>
              )}
              {showRun && (
                <AssistantBlock run={run} connection={connection} onCite={cite} onAnswer={(a) => void resume(a)} />
              )}
            </div>
          )}
        </div>
        <Composer
          disabled={busy || run.phase === "paused"}
          hint={failure ?? (run.phase === "paused" ? "Answer the question above to continue." : null)}
          onSubmit={ask}
        />
      </main>

      <div className={`min-h-0 flex-col ${mobile === "run" ? "flex" : "hidden lg:flex"}`}>
        <button type="button" className="border-b border-border p-2 text-left text-xs lg:hidden" onClick={() => setMobile("chat")}>← Back to the conversation</button>
        <RunPanel run={run} tab={tab} onTab={setTab} highlight={highlight} />
      </div>
    </div>
  );
}
