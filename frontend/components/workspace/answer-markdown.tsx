"use client";

import { AlertTriangle } from "lucide-react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

const MARKER_RE = /\[(\d{1,3}|unverified)\]/g;

/** Citation markers in order of appearance: "3", "unverified", ... */
export function markers(text: string): string[] {
  return [...text.matchAll(MARKER_RE)].map((m) => m[1]);
}

/**
 * Markers that changed between the streamed preview and the verified answer, by
 * position: index -> what the preview had ("" if the preview had none there).
 */
export function changedMarkers(preview: string, final: string): Map<number, string> {
  const before = markers(preview);
  const out = new Map<number, string>();
  markers(final).forEach((m, i) => {
    if (before[i] !== m) out.set(i, before[i] ?? "");
  });
  return out;
}

/**
 * Markdown with citation chips. Markers become links ("#cite-3~0~5": source 3, marker 0,
 * was 5) that the link renderer turns into chips, so tables and lists keep working.
 */
export function AnswerMarkdown({
  text,
  changed,
  onCite,
}: {
  text: string;
  changed?: Map<number, string>;
  onCite: (n: number) => void;
}) {
  let i = 0;
  const linked = text.replace(MARKER_RE, (_, m: string) => {
    const idx = i++;
    return `[${m}](#cite-${m}~${idx}~${changed?.get(idx) ?? "="})`;
  });
  return (
    <div className="prose-answer">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => {
            const cite = /^#cite-(\d{1,3}|unverified)~(\d+)~(.*)$/.exec(href ?? "");
            if (!cite) {
              return (
                <a href={href} target="_blank" rel="noreferrer" className="text-link underline">
                  {children}
                </a>
              );
            }
            const [, m, , was] = cite;
            const moved = was !== "=";
            const tip = !moved
              ? undefined
              : m === "unverified"
                ? "No retrieved source supports this claim"
                : was
                  ? `Citation moved from [${was}] after verification`
                  : "Citation added after verification";
            if (m === "unverified") {
              return (
                <span
                  className={`mx-0.5 inline-flex items-center gap-0.5 rounded border border-border px-1 font-mono text-[11px] text-muted ${moved ? "marker-changed" : ""}`}
                  title={tip ?? "No retrieved source supports this claim"}
                >
                  <AlertTriangle className="size-3" aria-hidden />
                  unverified
                </span>
              );
            }
            return (
              <button
                type="button"
                onClick={() => onCite(Number(m))}
                className={`mx-0.5 rounded bg-surface-2 px-1 font-mono text-[11px] text-link hover:bg-border ${moved ? "marker-changed" : ""}`}
                title={tip ?? `Show source ${m}`}
                aria-label={`Source ${m}`}
              >
                {m}
              </button>
            );
          },
        }}
      >
        {linked}
      </Markdown>
    </div>
  );
}
