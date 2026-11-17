# Recorded runs

Replay fixtures of **real live runs** of the agent (`source: "live"`), played by the demo's
example chips at zero tokens and always labeled "Recorded live run · <date> · not live".

Record one (spends about 15K agent tokens; ask the owner first):

    uv run python -m api.replay_recorder --api https://<api-host> --slug <slug> \
        --question "<question>" [--answer "<clarification answer>"] --yes

The recorder refuses to save a run that failed or a file that contains anything like an
API key or an IP address. Test fixtures recorded from the scripted runtime live in
`frontend/e2e/fixtures/replays/` and are labeled `scripted`, never `live`.
