"use client";

import "@xyflow/react/dist/style.css";

import dagre from "@dagrejs/dagre";
import { useQuery } from "@tanstack/react-query";
import { Background, type Edge, Handle, type Node, type NodeProps, Position, ReactFlow } from "@xyflow/react";
import { CheckCircle2, Loader2, MessageCircleQuestion, XCircle } from "lucide-react";
import { useMemo, useState } from "react";

import { api } from "@/lib/api";
import { type CNode, collapse } from "@/lib/graph-collapse";
import type { NodeStatus, RunState } from "@/lib/run-state";

import { AGENT_COLOR } from "./plan-tab";

const W = 132;
const H = 40;

type Data = { node: CNode; status: NodeStatus | undefined; parallel: number; selected: boolean };

function GraphNode({ data }: NodeProps<Node<Data>>) {
  const { node, status, parallel, selected } = data;
  const color = node.kind === "agent" ? AGENT_COLOR[node.id.replace(/_agent$/, "")] : undefined;
  const Icon = status === "active" ? Loader2 : status === "done" ? CheckCircle2 : status === "paused" ? MessageCircleQuestion : status === "failed" ? XCircle : null;
  const tone =
    status === "active" ? "border-accent text-foreground live-pulse" :
    status === "done" ? "border-ok/60 text-foreground" :
    status === "paused" ? "border-paused text-foreground" :
    status === "failed" ? "border-bad text-foreground" : "border-border text-muted";
  return (
    <div
      className={`relative flex h-full items-center justify-center rounded-md border bg-surface px-2 text-center font-mono text-[11px] ${tone} ${node.group === "intake" ? "border-dashed" : ""} ${selected ? "ring-1 ring-link" : ""}`}
      style={color ? { borderLeft: `3px solid ${color}` } : undefined}
      title={`${node.id}${status ? ` · ${status}` : ""}`}
    >
      <Handle type="target" position={Position.Left} className="!opacity-0" />
      {node.label}
      {Icon && <Icon className={`absolute -right-1.5 -top-1.5 size-3.5 rounded-full bg-background ${status === "active" ? "animate-spin text-accent" : status === "done" ? "text-ok" : status === "paused" ? "text-paused" : "text-bad"}`} aria-label={status} />}
      {parallel > 1 && <span className="absolute -bottom-2 right-1 rounded bg-accent px-1 text-[10px] text-background">×{parallel}</span>}
      <Handle type="source" position={Position.Right} className="!opacity-0" />
    </div>
  );
}

const nodeTypes = { g: GraphNode };
const LOOP_LABEL: Record<string, string> = { "critic->planner": "replan", "join->dispatcher": "next wave" };

export function GraphTab({ run }: { run: RunState }) {
  const graph = useQuery({ queryKey: ["graph"], queryFn: api.getGraph, staleTime: Infinity });
  const [open, setOpen] = useState<string | null>(null);
  const collapsed = useMemo(() => (graph.data ? collapse(graph.data) : null), [graph.data]);
  const positions = useMemo(() => {
    if (!collapsed) return new Map<string, { x: number; y: number }>();
    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: "LR", nodesep: 18, ranksep: 36 });
    g.setDefaultEdgeLabel(() => ({}));
    for (const n of collapsed.nodes) g.setNode(n.id, { width: W, height: H });
    for (const e of collapsed.edges) if (!e.loop) g.setEdge(e.source, e.target); // loops don't shape the layout
    dagre.layout(g);
    return new Map(collapsed.nodes.map((n) => [n.id, { x: g.node(n.id).x - W / 2, y: g.node(n.id).y - H / 2 }]));
  }, [collapsed]);

  if (graph.isLoading) return <p className="p-4 text-sm text-muted">Loading the graph…</p>;
  if (graph.isError || !collapsed) return <p className="p-4 text-sm text-bad">Couldn&apos;t load the graph: {String(graph.error)}</p>;

  const nodes: Node<Data>[] = collapsed.nodes.map((n) => ({
    id: n.id,
    type: "g",
    position: positions.get(n.id) ?? { x: 0, y: 0 },
    style: { width: W, height: H },
    data: { node: n, status: run.nodes[n.id], parallel: run.activeSteps[n.id] ?? 0, selected: open === n.id },
  }));
  const edges: Edge[] = collapsed.edges.map((e) => ({
    id: `${e.source}->${e.target}`,
    source: e.source,
    target: e.target,
    type: e.loop ? "default" : "smoothstep",
    animated: run.nodes[e.source] === "done" && run.nodes[e.target] === "active",
    label: e.loop ? (LOOP_LABEL[`${e.source}->${e.target}`] ?? "resume") : undefined,
    style: { stroke: "var(--border)", strokeDasharray: e.conditional ? "4 3" : undefined },
    labelStyle: { fill: "var(--muted)", fontSize: 10 },
    labelBgStyle: { fill: "var(--background)" },
  }));
  const inner = open ? collapsed.inner[open] : null;

  return (
    <div className="flex h-full flex-col">
      <div className="min-h-[320px] flex-1">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          fitView
          proOptions={{ hideAttribution: true }}
          nodesDraggable={false}
          nodesConnectable={false}
          onNodeClick={(_, n) => setOpen(n.data.node.kind === "agent" ? (open === n.id ? null : n.id) : null)}
          colorMode="dark"
        >
          <Background color="var(--border)" gap={16} />
        </ReactFlow>
      </div>
      {inner && open && (
        <section className="border-t border-border p-3" aria-label={`${open} inner loop`}>
          <h3 className="mb-2 font-mono text-xs uppercase tracking-wide text-muted">{open}: inner loop</h3>
          <div className="flex flex-wrap items-center gap-1.5 font-mono text-xs">
            {["prepare", "direct", "llm", "tools", "finish"].map((n, i) => {
              const s = run.nodes[`${open}:${n}`];
              return (
                <span key={n} className="flex items-center gap-1.5">
                  {i > 0 && <span className="text-faint">{n === "tools" ? "⇄" : "→"}</span>}
                  <span className={`rounded border px-1.5 py-0.5 ${s === "active" ? "border-accent" : s === "done" ? "border-ok/60" : "border-border text-muted"}`}>
                    {n}{s ? ` · ${s}` : ""}
                  </span>
                </span>
              );
            })}
          </div>
        </section>
      )}
      <p className="border-t border-border px-3 py-2 text-[11px] text-muted">
        Spinner = running · check = done · question = waiting for you · dashed = conditional edge. Click a specialist to see its loop.
      </p>
    </div>
  );
}
