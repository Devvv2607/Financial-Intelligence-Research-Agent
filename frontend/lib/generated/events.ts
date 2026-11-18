/* Generated from contracts/agent-events.schema.json by npm run gen:types. Do not edit. */

export type AgentEvent =
  | RunStarted
  | Queued
  | PlanCreated
  | Replan
  | StepStarted
  | ToolCalled
  | EvidenceAdded
  | StepCompleted
  | CritiqueEvent
  | ClarificationNeeded
  | NodeEvent
  | Token
  | Final
  | ErrorEvent;
export type Event = "run_started";
export type Resumed = boolean;
export type RunId = string;
export type ThreadId = string;
export type Event1 = "queued";
export type Position = number;
export type Event2 = "plan_created";
export type Round = number;
export type Agent = "filings" | "financials" | "news" | "screener";
export type DependsOn = string[];
export type Entities = string[];
export type Goal = string;
export type Id = string;
/**
 * JSON object with the tool's arguments
 */
export type ArgsJson = string;
export type Tool = string;
/**
 * Exact calls to run without an LLM (direct mode)
 */
export type ToolCalls = ToolCallSpec[];
export type Steps = PlanStep[];
export type Event3 = "replan";
export type Round1 = number;
export type Steps1 = PlanStep[];
export type Agent1 = string;
export type Event4 = "step_started";
export type Mode = "direct" | "agentic";
export type StepId = string;
export type Event5 = "tool_called";
export type Evidence = number;
export type Ok = boolean;
export type Reused = boolean;
export type StepId1 = string;
export type Tool1 = string;
export type Event6 = "evidence_added";
export type CompareNote = string | null;
export type CompareStatus = ("new" | "removed" | "changed" | "unchanged") | null;
export type FiledOrPublished = string | null;
export type FiscalYear = number | null;
export type FormType = string | null;
export type LinkedSourceIds = string[];
export type Score = number | null;
export type Section = string | null;
/**
 * The exact text supporting a claim (<= ~600 chars)
 */
export type Snippet = string;
export type SourceId = string;
export type SourceType = "filing_chunk" | "risk_factor" | "8k" | "financial_fact" | "news" | "sql";
export type Ticker = string | null;
export type Title = string;
export type Url = string | null;
export type Sources = Evidence1[];
export type StepId2 = string;
export type DurationMs = number | null;
export type Error = string | null;
export type Event7 = "step_completed";
export type Mode1 = ("direct" | "agentic" | "none") | null;
export type Reason = string | null;
export type Status = "done" | "failed" | "skipped";
export type StepId3 = string;
export type Summary = string | null;
export type Event8 = "critique";
export type Gaps = string[];
export type Replanning = boolean;
export type Verdict = "ok" | "gaps";
export type Event9 = "clarification_needed";
export type Options = string[];
export type Question = string;
export type Source = ("entity_resolver" | "query_analyzer") | null;
export type Error1 = boolean;
export type Event10 = "node";
export type Interrupted = boolean;
export type Node = string;
export type Phase = "start" | "end";
export type StepId4 = string | null;
export type Event11 = "token";
export type Text = string;
export type Answer = string;
export type CitationCheck = {
  [k: string]: unknown;
} | null;
export type N = number;
export type SourceId1 = string;
export type Title1 = string;
export type Url1 = string | null;
export type Citations = Citation[];
export type Event12 = "final";
export type Code = "agent_error" | "internal";
export type Event13 = "error";
export type Message = string;

export interface RunStarted {
  event: Event;
  resumed: Resumed;
  run_id: RunId;
  thread_id: ThreadId;
}
/**
 * The run waits for the one live slot (api/gate.py); sent again whenever its place
 * changes. 1 = next. The first event that isn't `queued` means the run has started.
 */
export interface Queued {
  event: Event1;
  position: Position;
}
export interface PlanCreated {
  event: Event2;
  round: Round;
  steps: Steps;
}
export interface PlanStep {
  agent: Agent;
  depends_on?: DependsOn;
  entities?: Entities;
  goal: Goal;
  id: Id;
  tool_calls?: ToolCalls;
}
export interface ToolCallSpec {
  args_json: ArgsJson;
  tool: Tool;
}
export interface Replan {
  event: Event3;
  round: Round1;
  steps: Steps1;
}
export interface StepStarted {
  agent: Agent1;
  event: Event4;
  mode: Mode;
  step_id: StepId;
}
export interface ToolCalled {
  args: Args;
  event: Event5;
  evidence: Evidence;
  ok: Ok;
  reused: Reused;
  step_id: StepId1;
  tool: Tool1;
}
export interface Args {
  [k: string]: unknown;
}
export interface EvidenceAdded {
  event: Event6;
  sources: Sources;
  step_id: StepId2;
}
/**
 * One citable source. `source_id` is stable across runs (used to dedupe).
 */
export interface Evidence1 {
  compare_note?: CompareNote;
  compare_status?: CompareStatus;
  filed_or_published?: FiledOrPublished;
  fiscal_year?: FiscalYear;
  form_type?: FormType;
  linked_source_ids?: LinkedSourceIds;
  score?: Score;
  section?: Section;
  snippet: Snippet;
  source_id: SourceId;
  source_type: SourceType;
  ticker?: Ticker;
  title: Title;
  url?: Url;
}
export interface StepCompleted {
  duration_ms: DurationMs;
  error: Error;
  event: Event7;
  mode: Mode1;
  reason: Reason;
  status: Status;
  step_id: StepId3;
  summary: Summary;
}
export interface CritiqueEvent {
  event: Event8;
  gaps: Gaps;
  replanning: Replanning;
  verdict: Verdict;
}
export interface ClarificationNeeded {
  event: Event9;
  options: Options;
  question: Question;
  source: Source;
}
/**
 * A graph node started or ended. `node` matches an id from GET /api/graph; inside a
 * specialist it is "<agent>:<node>". `step_id` tells parallel specialist runs apart.
 */
export interface NodeEvent {
  error: Error1;
  event: Event10;
  interrupted: Interrupted;
  node: Node;
  phase: Phase;
  step_id: StepId4;
}
export interface Token {
  event: Event11;
  text: Text;
}
/**
 * `answer` cites sources as [1], [2], ... in order of first appearance; `renumbered`
 * maps the numbers the streamed draft (`token` events) used to those.
 */
export interface Final {
  answer: Answer;
  citation_check: CitationCheck;
  citations: Citations;
  event: Event12;
  renumbered: Renumbered;
}
export interface Citation {
  n: N;
  source_id: SourceId1;
  title: Title1;
  url: Url1;
}
export interface Renumbered {
  [k: string]: number;
}
export interface ErrorEvent {
  code: Code;
  event: Event13;
  message: Message;
}
