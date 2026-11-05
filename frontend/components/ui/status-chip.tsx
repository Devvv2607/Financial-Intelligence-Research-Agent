import {
  AlertTriangle,
  CheckCircle2,
  Circle,
  Loader2,
  type LucideIcon,
  MessageCircleQuestion,
  MinusCircle,
  ShieldCheck,
  XCircle,
} from "lucide-react";

// The status vocabulary (design doc, "Status vocabulary"): every status is an icon, a
// label and a color, never color alone.
export type Status =
  | "pending"
  | "running"
  | "waiting"
  | "verifying"
  | "done"
  | "skipped"
  | "failed"
  | "unverified";

const STATUS: Record<Status, { icon: LucideIcon; label: string; className: string; spin?: boolean }> =
  {
    pending: { icon: Circle, label: "Pending", className: "text-faint" },
    running: { icon: Loader2, label: "Running", className: "text-accent", spin: true },
    waiting: { icon: MessageCircleQuestion, label: "Needs your answer", className: "text-paused" },
    verifying: { icon: ShieldCheck, label: "Verifying citations…", className: "text-accent live-pulse" },
    done: { icon: CheckCircle2, label: "Done", className: "text-ok" },
    skipped: { icon: MinusCircle, label: "Skipped", className: "text-muted" },
    failed: { icon: XCircle, label: "Failed", className: "text-bad" },
    unverified: { icon: AlertTriangle, label: "Unverified", className: "text-muted" },
  };

export function StatusChip({
  status,
  label,
  compact = false,
}: {
  status: Status;
  label?: string;
  compact?: boolean;
}) {
  const s = STATUS[status];
  const Icon = s.icon;
  return (
    <span
      className={`inline-flex items-center gap-1 text-xs font-medium ${s.className}`}
      title={compact ? (label ?? s.label) : undefined}
    >
      <Icon className={`size-3.5 shrink-0 ${s.spin ? "animate-spin" : ""}`} aria-hidden />
      {compact ? <span className="sr-only">{label ?? s.label}</span> : (label ?? s.label)}
    </span>
  );
}
