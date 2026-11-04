# Financial Intelligence Research Agent

An agentic research system for financial questions, built with **LangGraph, Groq and
Weaviate** over SEC EDGAR filings and financial news. It plans, runs tools in parallel,
checks its own work, asks when it's unsure, and cites everything.

> **Status: Phase 0 (scaffold).** The stack, health checks and CI are in place; the
> ingestion pipeline, agent and UI land in later phases. The full README (architecture,
> LangGraph feature map, real pipeline counts and eval results) is written in Phase 7.

## Quick start

Prerequisites: Docker Desktop (WSL2 backend), [uv](https://docs.astral.sh/uv/), Node 22.

```powershell
Copy-Item .env.example .env      # then set GROQ_API_KEY in .env
.\make.ps1 install
.\make.ps1 up                    # postgres, weaviate, api, web
.\make.ps1 ready                 # expect "status":"ready" and HTTP 200
```

On Linux/macOS/Git Bash, use `cp` and `make <target>` instead.

- Web: http://localhost:3000 (shows live dependency status)
- API: http://localhost:8000/health · http://localhost:8000/ready · http://localhost:8000/docs

## Repository
See `CLAUDE.md` for layout and commands, `BUILD_SPEC.md` for the specification, and
`docs/DECISIONS.md` for every architectural decision and spec deviation.
