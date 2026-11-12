"""The research graph (BUILD_SPEC 4.3). Where each LangGraph feature lives:

1. Typed StateGraph + reducers ........ agent/state.py (merge_keyed, evidence_reducer)
2. Conditional edges ................. _after_* routers below + route_after_dispatch/critic
3. Send fan-out ...................... nodes/planning.route_after_dispatch
4. Subgraphs ......................... agent/specialists.py (one compiled graph per agent)
5. interrupt() / Command(resume=) .... nodes/intake.ask_clarification; resume returns to
                                        the node that asked (_after_clarification)
6. Checkpointer ...................... build_graph(checkpointer=AsyncPostgresSaver), thread_id
7. Streaming ......................... get_stream_writer() events in nodes + subgraphs;
                                        synthesizer tokens via stream_mode="messages"
8. Bounded loops ..................... MAX_REPLANS, MAX_TOOL_CALLS, MAX_CLARIFICATIONS,
                                        MAX_DISPATCH_ROUNDS, RECURSION_LIMIT
9. Replay ............................ agent/__main__.py `replay` (checkpoint history)
"""

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent.deps import AgentDeps
from agent.nodes.answer import make_answer_nodes, route_after_critic
from agent.nodes.intake import make_intake_nodes
from agent.nodes.planning import make_planning_nodes, route_after_dispatch
from agent.specialists import build_specialist
from agent.state import ResearchState

RECURSION_LIMIT = 80
SPECIALISTS = ("filings", "financials", "news", "screener")


def _after_guardrail(state: ResearchState) -> str:
    return "finalize" if state.get("route") == "advice" else "entity_resolver"


def _after_resolver(state: ResearchState) -> str:
    clar = state.get("clarification")
    asking = clar is not None and clar.source == "entity_resolver" and clar.answer is None
    return "ask_clarification" if asking else "query_analyzer"


def _after_analyzer(state: ResearchState) -> str:
    if state.get("route") in ("advice", "uncovered", "error"):
        return "finalize"
    clar = state.get("clarification")
    if clar is not None and clar.source == "query_analyzer" and clar.answer is None:
        return "ask_clarification"
    return "planner"


def _after_clarification(state: ResearchState) -> str:
    clar = state.get("clarification")
    return clar.source if clar is not None else "query_analyzer"


def _after_planner(state: ResearchState) -> str:
    return "finalize" if state.get("route") == "error" else "dispatcher"


def build_graph(
    deps: AgentDeps, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> CompiledStateGraph:  # type: ignore[type-arg]
    g = StateGraph(ResearchState)
    for nodes in (make_intake_nodes(deps), make_planning_nodes(deps), make_answer_nodes(deps)):
        for name, fn in nodes.items():
            g.add_node(name, fn)
    for agent in SPECIALISTS:
        g.add_node(f"{agent}_agent", build_specialist(agent, deps))  # type: ignore[arg-type]

    g.add_edge(START, "start_turn")
    g.add_edge("start_turn", "guardrail")
    g.add_conditional_edges("guardrail", _after_guardrail, ["finalize", "entity_resolver"])
    g.add_conditional_edges(
        "entity_resolver", _after_resolver, ["ask_clarification", "query_analyzer"]
    )
    g.add_conditional_edges(
        "query_analyzer", _after_analyzer, ["finalize", "ask_clarification", "planner"]
    )
    g.add_conditional_edges(
        "ask_clarification", _after_clarification, ["entity_resolver", "query_analyzer"]
    )
    g.add_conditional_edges("planner", _after_planner, ["finalize", "dispatcher"])
    g.add_conditional_edges(
        "dispatcher", route_after_dispatch, [*(f"{a}_agent" for a in SPECIALISTS), "critic"]
    )
    for agent in SPECIALISTS:
        g.add_edge(f"{agent}_agent", "join")
    g.add_edge("join", "dispatcher")
    g.add_conditional_edges("critic", route_after_critic, ["planner", "synthesizer"])
    g.add_edge("synthesizer", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
