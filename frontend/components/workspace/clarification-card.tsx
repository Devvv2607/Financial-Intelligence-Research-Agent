"use client";

import { MessageCircleQuestion } from "lucide-react";
import { useState } from "react";

/** The agent paused to ask; answering calls /resume and the same run continues. */
export function ClarificationCard({
  clarification,
  onAnswer,
}: {
  clarification: { question: string; options: string[] };
  onAnswer: (answer: string) => void;
}) {
  const [text, setText] = useState("");
  const [sent, setSent] = useState(false);
  const send = (answer: string) => {
    if (!answer.trim() || sent) return;
    setSent(true);
    onAnswer(answer.trim());
  };
  return (
    <section
      className="rounded-md border border-border border-l-2 border-l-paused bg-surface p-3"
      aria-label="Clarification needed"
    >
      <p className="mb-2 flex items-center gap-2 text-sm font-medium">
        <MessageCircleQuestion className="size-4 text-paused" aria-hidden />
        {clarification.question}
      </p>
      <div className="mb-2 flex flex-wrap gap-2">
        {clarification.options.map((o) => (
          <button
            key={o}
            type="button"
            disabled={sent}
            onClick={() => send(o)}
            className="rounded border border-border bg-surface-2 px-2 py-1 text-sm hover:border-paused disabled:opacity-50"
          >
            {o}
          </button>
        ))}
      </div>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          send(text);
        }}
      >
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={sent}
          placeholder="Or type your own answer"
          maxLength={500}
          className="min-w-0 flex-1 rounded border border-border bg-background px-2 py-1 text-sm"
        />
        <button
          type="submit"
          disabled={sent || !text.trim()}
          className="rounded bg-paused px-3 py-1 text-sm font-medium text-background disabled:opacity-50"
        >
          Answer
        </button>
      </form>
    </section>
  );
}
