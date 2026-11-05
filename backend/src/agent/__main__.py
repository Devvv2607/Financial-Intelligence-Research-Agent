"""CLI for the research agent (Postgres checkpointer, agent model only).

    python -m agent ask "Compare NVIDIA and AMD risk factors" [--thread T] [--answer "..."]
    python -m agent resume --thread T "JPMorgan, credit risk"
    python -m agent history --thread T
    python -m agent replay --thread T --checkpoint CHECKPOINT_ID

`ask` streams the run: plan, each step (direct/agentic), tool calls, critique, and the
answer; then prints tokens per node from the ledger. A clarification interrupt is answered
interactively, with --answer, or later with `resume`.
"""

import argparse
import asyncio
import sys
import uuid
from functools import partial
from typing import Any

from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from agent.checkpoint import postgres_saver
from agent.deps import AgentDeps
from agent.graph import RECURSION_LIMIT, build_graph
from agent.llm import GroqAgentLLM
from agent.runlog import make_run_recorder, node_usage
from agent.tools import build_context, call_tool, close_context
from agent.tools.common import Company, fiscal_periods, resolve_tickers
from core import aio
from core.config import Settings, get_settings
from core.db.llm_store import PostgresLLMStore
from core.logging import configure_logging


def _print_event(e: dict[str, Any]) -> None:
    kind = e.get("event")
    if kind in ("plan_created", "replan"):
        title = "PLAN" if kind == "plan_created" else f"REPLAN (round {e['round']})"
        print(f"\n== {title}")
        for s in e["steps"]:
            deps = f" after {', '.join(s['depends_on'])}" if s["depends_on"] else ""
            mode = "direct" if s["tool_calls"] else "agentic"
            calls = "; ".join(f"{c['tool']}({c['args_json']})" for c in s["tool_calls"])
            print(
                f"  {s['id']} [{s['agent']}, {mode}]{deps}: {s['goal']}"
                + (f"\n      -> {calls}" if calls else "")
            )
    elif kind == "step_started":
        print(f"  > {e['step_id']} started ({e['agent']}, {e['mode']})")
    elif kind == "tool_called":
        print(f"    . {e['step_id']} {e['tool']}({e['args']}) ok={e['ok']} sources={e['evidence']}")
    elif kind == "step_completed":
        extra = f" - {e['reason']}" if e.get("reason") else ""
        print(f"  < {e['step_id']} {e['status']} ({e.get('mode', 'none')}){extra}")
    elif kind == "critique":
        print(
            f"\n== CRITIC: {e['verdict']}"
            + (f" | gaps: {e['gaps']}" if e["gaps"] else "")
            + (" -> replanning" if e["replanning"] else "")
        )
    elif kind == "clarification_needed":
        print(f"\n== CLARIFICATION: {e['question']}")
        for i, o in enumerate(e["options"], start=1):
            print(f"  {i}. {o}")


async def _stream(graph: Any, payload: Any, config: RunnableConfig) -> str:
    """Run until the end or an interrupt; returns the answer text that streamed live."""
    streamed = ""
    async for _ns, mode, chunk in graph.astream(
        payload, config, stream_mode=["updates", "custom", "messages"], subgraphs=True
    ):
        if mode == "custom" and isinstance(chunk, dict):
            _print_event(chunk)
        elif mode == "messages":
            msg, meta = chunk
            node = meta.get("langgraph_node")
            if node == "synthesizer" and isinstance(msg, AIMessageChunk) and msg.content:
                if not streamed:
                    print("\n== ANSWER\n")
                streamed += str(msg.content)
                print(msg.content, end="", flush=True)
    return streamed


async def _report(
    graph: Any, config: RunnableConfig, settings: Settings, engine: Any, streamed: str
) -> None:
    state = await graph.aget_state(config)
    values = state.values
    if state.interrupts:
        return
    answer = values.get("answer") or ""
    body, _, footer = answer.rpartition("\n\n---")
    if streamed and body.strip() == streamed.strip():
        print("\n\n---" + footer)
    elif streamed:  # finalize changed it (citations normalised/removed): show the checked text
        print("\n\n== ANSWER (citations checked)\n\n" + answer)
    else:
        print("\n== ANSWER\n\n" + answer)
    results = values.get("step_results", {})
    if results:
        print("\n== STEPS")
        for s in values.get("plan", []):
            r = results.get(s.id)
            if r is None:
                continue
            tools = ", ".join(t.tool for t in r.tool_calls) or "-"
            why = f" | {r.skip_reason or r.error}" if r.status != "done" else ""
            print(f"  {s.id:6} {s.agent:10} {r.mode:8} {r.status:8} tools: {tools}{why}")
    run_id = values.get("run_id")
    if run_id:
        usage = await node_usage(engine, run_id)
        print("\n== TOKENS (counted = prompt - cached + completion)")
        total = 0
        for node, u in sorted(usage.items(), key=lambda kv: -kv[1]["counted"]):
            total += u["counted"]
            print(
                f"  {node:18} {u['counted']:>7,}  calls={u['calls']} reasoning={u['reasoning']:,}"
                f" cache_hits={u['cache_hits']} (saved {u['saved']:,})"
            )
        thread = config["configurable"]["thread_id"]
        print(f"  {'TOTAL':18} {total:>7,}   run {run_id} | thread {thread}")
    check = values.get("citation_check")
    if check:
        print(
            f"\n== CITATION CHECK: precision {check['precision']} ({check['supported']}"
            f"/{check['checked']} supported; {check['reattached']} re-attached, "
            f"{check['dropped']} dropped, {check['unverified_claims']} claims unverified)"
        )
    if values.get("citations"):
        print("\n== SOURCES")
        for c in values["citations"]:
            print(f"  [{c.n}] {c.title} - {c.url}")


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    ctx = await build_context(settings)
    llm = GroqAgentLLM(settings, PostgresLLMStore(ctx.engine))

    async def companies() -> list[Company]:
        return (await resolve_tickers(ctx, None))[0]

    deps = AgentDeps(
        llm=llm,
        call_tool=partial(call_tool, ctx),
        list_companies=companies,
        record_run=make_run_recorder(ctx.engine, settings),
        fiscal_periods=partial(fiscal_periods, ctx),
        embed=ctx.embedder.embed if ctx.embedder else None,
    )
    try:
        async with postgres_saver(settings.database_url) as saver:
            graph = build_graph(deps, saver)
            thread = args.thread or str(uuid.uuid4())
            config: RunnableConfig = {
                "configurable": {"thread_id": thread},
                "recursion_limit": RECURSION_LIMIT,
            }
            if args.command == "history":
                async for snap in graph.aget_state_history(config):
                    cid = snap.config["configurable"]["checkpoint_id"]
                    step = snap.metadata.get("step") if snap.metadata else None
                    print(
                        f"step {step:>3}  {cid}  next={list(snap.next)}  "
                        f"turn={snap.values.get('turn')}"
                    )
                return 0
            if args.command == "replay":
                config["configurable"]["checkpoint_id"] = args.checkpoint
                snap = await graph.aget_state(config)
                config["configurable"]["agent_run_id"] = snap.values.get("run_id") or str(
                    uuid.uuid4()
                )
                streamed = await _stream(graph, None, config)
                await _report(graph, config, settings, ctx.engine, streamed)
                return 0
            if args.command == "ask":
                config["configurable"]["agent_run_id"] = str(uuid.uuid4())
                payload: Any = {"messages": [HumanMessage(content=args.question)]}
            else:  # resume: same run id as the paused question
                snap = await graph.aget_state(config)
                if not snap.interrupts:
                    print("Nothing to resume on this thread.")
                    return 1
                config["configurable"]["agent_run_id"] = snap.values.get("run_id")
                payload = Command(resume=args.answer_text)
            print(f"thread {thread}")
            streamed = await _stream(graph, payload, config)
            while True:
                snap = await graph.aget_state(config)
                if not snap.interrupts:
                    break
                answer = args.answer if args.command == "ask" else None
                if answer is None and sys.stdin.isatty():
                    try:
                        answer = (await asyncio.to_thread(input, "\nYour answer: ")).strip()
                    except EOFError:  # Windows reports NUL as a tty: treat as no answer
                        answer = None
                if not answer:
                    print(f'\nPaused. Answer with: python -m agent resume --thread {thread} "..."')
                    return 0
                args.answer = None  # use a scripted answer once
                print(f"(answer: {answer})")
                streamed = await _stream(graph, Command(resume=answer), config)
            await _report(graph, config, settings, ctx.engine, streamed)
    finally:
        await llm.aclose()
        await close_context(ctx)
    return 0


def cli() -> None:
    parser = argparse.ArgumentParser(prog="python -m agent")
    sub = parser.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--thread")
    ask.add_argument("--answer", help="answer to use if the agent asks a clarification")
    res = sub.add_parser("resume")
    res.add_argument("--thread", required=True)
    res.add_argument("answer_text")
    hist = sub.add_parser("history")
    hist.add_argument("--thread", required=True)
    rep = sub.add_parser("replay")
    rep.add_argument("--thread", required=True)
    rep.add_argument("--checkpoint", required=True)
    configure_logging("WARNING")
    sys.exit(aio.run(main(parser.parse_args())))


if __name__ == "__main__":
    cli()
