import type { GraphJSON } from "./types";

// /api/graph has 13 top-level nodes + 4 specialist subgraphs x 5 nodes. Shown raw, the four
// identical inner loops are 60% of the picture; collapsed, each specialist is one node
// (its loop shown on demand) and intake nodes form a group (design doc §3).

export const INTAKE = ["start_turn", "guardrail", "entity_resolver", "query_analyzer", "ask_clarification"];

export type CNode = { id: string; label: string; kind: "node" | "agent"; group: "intake" | null };
export type CEdge = { source: string; target: string; conditional: boolean; loop: boolean };
export type Collapsed = { nodes: CNode[]; edges: CEdge[]; inner: Record<string, GraphJSON> };

const LOOPS = new Set(["critic->planner", "join->dispatcher", "ask_clarification->entity_resolver", "ask_clarification->query_analyzer"]);

export function collapse(g: GraphJSON): Collapsed {
  const sub = (id: string) => g.nodes.find((n) => n.id === id)?.subgraph ?? null;
  const owner = (id: string) => sub(id) ?? id; // subgraph node -> its specialist
  const nodes = new Map<string, CNode>();
  const inner: Record<string, GraphJSON> = {};
  for (const n of g.nodes) {
    if (n.id === "__start__" || n.id === "__end__") continue;
    if (n.subgraph) {
      nodes.set(n.subgraph, { id: n.subgraph, label: agentLabel(n.subgraph), kind: "agent", group: null });
      (inner[n.subgraph] ??= { nodes: [], edges: [] }).nodes.push(n);
    } else {
      nodes.set(n.id, { id: n.id, label: n.label, kind: "node", group: INTAKE.includes(n.id) ? "intake" : null });
    }
  }
  const edges = new Map<string, CEdge>();
  for (const e of g.edges) {
    const s = owner(e.source);
    const t = owner(e.target);
    if (sub(e.source) && sub(e.source) === sub(e.target)) {
      inner[sub(e.source)!].edges.push(e);
      continue;
    }
    if (!nodes.has(s) || !nodes.has(t) || s === t) continue;
    const key = `${s}->${t}`;
    if (!edges.has(key)) edges.set(key, { source: s, target: t, conditional: e.conditional, loop: LOOPS.has(key) });
  }
  return { nodes: [...nodes.values()], edges: [...edges.values()], inner };
}

export function agentLabel(id: string): string {
  const name = id.replace(/_agent$/, "");
  return name.charAt(0).toUpperCase() + name.slice(1);
}
