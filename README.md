# HAYI

An application agnostic agent harness. One class, `Agent`, is the whole API: it
resolves its own configuration — defaults, a JSON or YAML file, the environment,
then arguments — renders a prompt template the consumer owns, runs the agentic
loop inside an isolated session workspace — optionally a git checkout it can
commit, push and open a pull request from — and streams the result as blocks to
the terminal, to the web UI, or to any subscriber you attach.
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

The package is on GitHub and can be installed straight from the repository with
any PEP 508 installer (requires Python 3.12+):

```
uv add "agent @ git+https://github.com/iamhaymc/hayikit"
```

or with pip:

```
pip install "agent @ git+https://github.com/iamhaymc/hayikit"
```

You can pin to a tag or a commit:

```
pip install "agent @ git+https://github.com/iamhaymc/hayikit@v0.1.0"
pip install "agent @ git+https://github.com/iamhaymc/hayikit@<commit-sha>"
```

Note that the transformers dependency is itself pulled from GitHub, so the
first install may take a while.

To develop on hayikit itself:

```
git clone https://github.com/iamhaymc/hayikit && cd hayikit
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e .
```

## Run

```
export AGENT_API_KEY=...                # never commit it
python agent.py                         # chat in the terminal
python agent.py --input notes.md        # one shot run, streamed to the console
python agent.py --serve                 # REST API + websocket hub + chat UI
```

With no arguments the terminal opens: type a message and the answer streams
back, the session remembers the conversation, `/help` lists the slash commands
and `/exit` (or Ctrl-D) leaves. Ctrl-C cancels the run in flight and keeps the
prompt. The same class drives it, so a consumer can open one on its own agent
with `await Repl(my_agent).start()`.

`--input`, `--repl`, `--serve` and `--config` are the only command line
arguments; everything else is configuration.

## Configure

Settings are resolved in layers — **defaults → config file → `AGENT_*`
environment → constructor or CLI arguments** — so the later layer always wins.

A config file is `agent.json`, `agent.yaml` or `agent.yml`: named after
`agent.py`, looked for in the current directory first and next to `agent.py` as
a fallback. Point at another one with `--config path` or
`Agent(config_file="path")`, and switch the layer off entirely with
`Agent(config_file=False)`.

```yaml
name: scribe
model: some/leader-model
api_key: ${oc.env:AGENT_API_KEY}   # keep the secret in the environment
vision:                            # a group flattens onto vision_*
  model: some/vision-model
repo:
  url: owner/name
genre: noir                        # unknown keys are extras: config.genre
```

Files are read with [omegaconf](https://omegaconf.readthedocs.io), so `${...}`
interpolations to the environment and to other keys in the file work. The
equivalent environment is `AGENT_NAME`, `AGENT_MODEL`, `AGENT_VISION_MODEL`,
`AGENT_REPO_URL`, and so on:

```
AGENT_MODEL=some/leader-model \
AGENT_VISION_MODEL=some/vision-model \
AGENT_REPO_URL=owner/name AGENT_REPO_TOKEN=$TOKEN \
python agent.py --serve
```

In server mode the chat page is at `http://<host>:<port>/` (`127.0.0.1:8765` by
default). Slash commands (`/help`) manage sessions, models and the repository
from the terminal and the UI alike.

## Test

```
python -m unittest agent_test -v
```

## More

- [GUIDE.md](GUIDE.md) — the walkthrough of the implementation, with the full
  configuration, tool, command and protocol reference.
- [CHANGES.md](CHANGES.md) — what has been built and why it is built that way.
- [TODO.md](TODO.md) — what is still open.