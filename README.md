# HAYI

An application agnostic agent harness. One class, `Agent`, is the whole API: it
resolves its own configuration, renders a prompt template the consumer owns,
runs the agentic loop inside an isolated session workspace — optionally a git
checkout it can commit, push and open a pull request from — and streams the
result as blocks to the console, to the web UI, or to any subscriber you attach.
Every tool call is streamed with it: its name, its redacted arguments, its
outcome and how long it took. A session remembers its conversation, so the next
run continues it and a client that reconnects gets the discussion back. Model
calls are bounded: a timeout per attempt, retries with jittered backoff on the
failures worth retrying, a named reason on the result when one fails, and the
tokens and cost of the run reported when it ends.

```python
import asyncio
from agent import Agent

TEMPLATE = """
Summarise the following notes as bullet points.

{{ config.input }}
"""

asyncio.run(Agent().run(TEMPLATE, input="notes.md"))
```

`input` is a file path or raw text. When it is a path the file is read and the
text is assigned to `config.input`, which is what the template injects.

## Embedding

Applications that already own their sessions, history and storage can use the
loop on its own. `Engine` is the inference and tool call loop with none of the
batteries: no store, no sessions, no conversation memory, no git checkouts, no
console and no web layer. It takes the model input verbatim, so the caller keeps
whatever history it likes, and returns the same `RunResult`.

```python
from agent import Engine

engine = Engine(model="some/model", api_key=..., env=False)

result = await engine.run(
    [
        {"role": "user", "content": "What did I ask before?"},
    ],
    tools=my_tools,             # or workspace="/some/dir" for the built-in ones
)
print(result.output, result.ok)
```

- `env=False` keeps the `AGENT_*` environment out of an embedded process.
- `tools=` replaces the tool list; `workspace=` (per run, or on the constructor)
  builds the sandboxed file and shell tools instead. With neither, the model runs
  with no tools at all.
- `engine.events.on(EventType.BLOCK_DELTA, handler)` streams tokens into the host
  application; nothing is printed unless you pass `console=True`.
- `session_id=` is an opaque correlation id echoed on every event and on the
  result — the engine itself keeps no session state.

`Agent` is this same class with the batteries bolted on, so anything overridden
or injected below works in both.

## Install

```
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
```

## Run

```
export AGENT_API_KEY=...                # never commit it
python agent.py --input notes.md        # one shot run, streamed to the console
python agent.py --serve                 # REST API + websocket hub + chat UI
```

`--input` and `--serve` are the only command line arguments; everything else is
configuration (`AGENT_*` environment variables or constructor arguments), for
example:

```
AGENT_MODEL=some/leader-model \
AGENT_VISION_MODEL=some/vision-model \
AGENT_REPO_URL=owner/name AGENT_REPO_TOKEN=$TOKEN \
python agent.py --serve
```

In server mode the chat page is at `http://<host>:<port>/` (`127.0.0.1:8765` by
default). Slash commands (`/help`) manage sessions, models and the repository
from both the CLI and the UI.

## Test

```
python -m unittest agent_test -v
```

## More

- [GUIDE.md](GUIDE.md) — the walkthrough of the implementation, with the full
  configuration, tool, command and protocol reference.
- [CHANGES.md](CHANGES.md) — what has been built and why it is built that way.
- [TODO.md](TODO.md) — what is still open.
- [`utils/storynu`](../storynu) — an example consumer: a prompt template and a
  small `Agent` subclass.
