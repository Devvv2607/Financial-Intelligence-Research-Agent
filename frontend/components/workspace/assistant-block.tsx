"use client";

import { useEffect, useMemo, useState } from "react";

import { StatusChip } from "@/components/ui/status-chip";
import type { Connection } from "@/lib/use-run";
import { type RunState, statusLine } from "@/lib/run-state";

import { AnswerMarkdown, changedMarkers } from "./answer-markdown";
import { ClarificationCard } from "./clarification-card";

const MIN_VERIFY_MS = 400; // the "Verifying citations…" state never just flashes

/**
 * One assistant turn: status line, the answer (streamed preview, then the verified text),
 * the citation-check badge and, when paused, the clarification card.
 */
export function AssistantBlock({
  run,
  connection,
  onCite,
  onAnswer,
}: {
  run: RunState;
  connection: Connection;
  onCite: (n: number) => void;
  onAnswer: (answer: string) => void;
}) {
  // Hold "Verifying citations…" for at least MIN_VERIFY_MS before revealing the final text.
  // Times come from the reducer (event arrival), so render stays pure; the effect only
  // arms a timer for the remainder.
  const revealAt = run.verifyingSince === null ? 0 : run.verifyingSince + MIN_VERIFY_MS;
  const [clock, setClock] = useState(0);
  useEffect(() => {
    if (run.finalAt === null || run.finalAt >= revealAt) return;
    const t = setTimeout(() => setClock(revealAt), revealAt - run.finalAt);
    return () => clearTimeout(t);
  }, [run.finalAt, revealAt]);
  const showFinal =
    !!run.final && (run.finalAt === null || run.finalAt >= revealAt || clock >= revealAt);

  const changed = useMemo(
    () => (run.final && run.preview ? changedMarkers(run.preview, run.final.answer) : undefined),
    [run.final, run.preview],
  );
  const verifying = (run.phase === "verifying" || (run.final && !showFinal)) && run.preview !== "";
  const check = run.final?.check as { supported?: number; checked?: number } | null | undefined;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3 font-mono text-xs text-muted" aria-live="polite">
        {run.phase === "done" ? (
          <StatusChip status="done" label={statusLine(run)} />
        ) : run.phase === "error" ? (
          <StatusChip status="failed" label={run.error ?? "The run failed"} />
        ) : run.phase === "paused" ? (
          <StatusChip status="waiting" />
        ) : verifying ? (
          <StatusChip status="verifying" />
        ) : (
          <StatusChip status="running" label={statusLine(run)} />
        )}
        {connection === "reconnecting" && <span className="text-accent">Reconnecting…</span>}
        {connection === "polling" && <span className="text-accent">Connection lost; checking the run…</span>}
      </div>

      {showFinal && run.final ? (
        <div className={run.preview ? "fade-in" : undefined}>
          <AnswerMarkdown text={run.final.answer} changed={changed} onCite={onCite} />
        </div>
      ) : run.preview ? (
        <div className={verifying ? "opacity-60 transition-opacity" : undefined}>
          <AnswerMarkdown text={run.preview} onCite={onCite} />
        </div>
      ) : null}

      {showFinal && check?.checked ? (
        <div className="font-mono text-xs text-muted">
          citations {check.supported}/{check.checked} verified
        </div>
      ) : null}

      {run.phase === "paused" && run.clarification && (
        <ClarificationCard clarification={run.clarification} onAnswer={onAnswer} />
      )}
    </div>
  );
}
