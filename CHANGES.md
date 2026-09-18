# agent — Changes

The record of what has been built and why it is built that way: the constraints
the harness was designed against, the reasoning behind each subsystem, and the
defects that were worth understanding rather than merely patching. It is written
for whoever picks the library up next, so an entry is kept while it explains a
decision that is still load-bearing and dropped when the code has stopped
depending on it.

Three other files carry the rest. [`README.md`](README.md) is the short version —
what the harness is and how to install, run and test it. [`GUIDE.md`](GUIDE.md)
is the walkthrough of the implementation, subsystem by subsystem, with the
configuration, protocol and extension reference. [`TODO.md`](TODO.md) is what is
left, in one flat list.

There are no releases yet, so this is grouped by subsystem rather than by
version, with the phase log and the fixes at the end.

---

## Constraints and design intent

### Goals

- **Application agnostic.** The harness knows nothing about any product built on
  it. A consumer owns a prompt template and, at most, a subclass; everything
  else is configuration.
- **One class is the API.** `Agent` resolves its own configuration, renders the
  template, runs the agentic loop and streams the result. `await Agent().run(t)`
  is a complete program.
- **The loop is usable alone.** The batteries are additive, not structural:
  `Engine` is the inference and tool call loop with no storage, sessions,
  memory, repository, console or web, and `Agent` is that class with those
  bolted on. An application that already has its own infrastructure embeds
  `Engine` and pays for nothing else.
- **Everything is replaceable.** Every subsystem is either an injectable
  collaborator (`store=`, `cache=`, `tools=`, …), an overridable method
  (`build_model`, `build_prompt`, `stream`, …) or a registry entry (tools,
  commands, event subscribers). No behaviour is reachable only by patching.
- **A run leaves no trace.** Work happens inside a session that owns a temporary
  directory; closing or expiring the session removes the directory, its database
  record and its cache entry.
- **One implementation, every front end.** The same `Agent` drives a one shot
  console run, the terminal repl and the web UI, because printing and streaming
  are event subscribers rather than code paths inside the loop, and because the
  repl and the web server are both clients of the same command registry.
- **Secrets never leak.** Credentials are masked in every serialization, banner
  and command output, and are never written to disk.

### Non-goals

- A framework. There is no plugin discovery, no dependency injection container
  and no configuration DSL: a dataclass, an event bus and two registries.
- A hosted product. There is no user model and no multi-tenancy; conversations
  are remembered per session, not per person, and the harness is a library
  first.
- A model abstraction layer. The `openai-agents` SDK is the loop; the harness
  supplies configuration, tools, sessions and transport around it.

### Constraints that shaped the code

- **Chat Completions backends only.** The hosted tools of `openai-agents`
  (`ApplyPatchTool`, `ShellTool`, …) require the OpenAI Responses API.
  OpenRouter and most other providers speak Chat Completions, which supports
  plain function tools only — so the file, shell, image and patch tools are
  implemented here rather than taken from the SDK.
- **Async end to end.** The web layer, the SDK stream and the tools all live on
  one event loop, so every blocking primitive (file IO, `git`, subprocesses)
  either runs in a thread or uses an async subprocess.
- **A single flat module.** `agent.py` is one file with commented subsystem
  sections. It keeps the import graph trivial, the library copyable into another
  tree and the whole surface readable in one pass.

---

## Configuration

`AgentConfig` is a dataclass resolved in layers: dataclass defaults, then a JSON
or YAML file, then `AGENT_*` environment variables, then whatever the consumer or
the CLI passes in. Layering in that order is what lets the same object serve a
library caller, a shell and a deployment without any of them knowing about the
others.

Values are coerced from the environment against the field annotations, so
`AGENT_PORT` becomes an `int` and `AGENT_THEME` a `Path` without a schema.
Unknown keys are not errors: they land in `extras` and are reachable from a
template as `config.<name>`, which is how an application adds its own values
without subclassing the config.

`input` is deliberately overloaded. A caller passes a file path or raw text and
the harness decides: short single-line values that name an existing file are
read, everything else is used as is. That is the whole reason the CLI needs only
`--input`.

Secrets are recognized by field name (`*_api_key`, `*_token`, `*secret*`, …) and
masked by `to_dict()` and by the console banner, so a config can be logged,
served over the REST API or printed without redacting it by hand at each site.
An empty `api_key` is a valid state, not a missing one: endpoints that need no
auth are configured by leaving the key unset.

### Config files

The environment is a poor place to keep two dozen settings: it is flat, it is
untyped, and a deployment ends up with a wrapper script whose only job is to
export things. A file layer fixes that without becoming a framework.

- **Named after the module.** `agent.json`, `agent.yaml` or `agent.yml` — the
  stem of `agent.py`. A consumer that renames the module renames its config with
  it, and nothing needs to be configured to find the configuration.
- **The current directory first, the module directory as a fallback.** An
  installed harness ships whatever defaults it likes beside itself; the
  application that runs it overrides them from where it is run. The first file
  that exists wins, because merging two files invites the question of which one
  set what.
- **Below the environment.** A file is checked in, the environment is not, so
  `AGENT_API_KEY` must still beat a value in the file — and a credential that
  belongs nowhere near a repository can be pulled in explicitly with
  `${oc.env:AGENT_API_KEY}` instead.
- **omegaconf, not a schema.** It reads both formats and resolves `${...}`
  interpolations to the environment and to other keys, which is what makes one
  file serve several environments. It stays optional at runtime: JSON is read by
  the standard library and YAML falls back to `pyyaml`, so a stripped install
  still starts.
- **Nested groups, flattened carefully.** `vision: {model: v}` becomes
  `vision_model` only when every key of the group names a real field. Anything
  else is left alone and lands in `extras`, so an application's own nested values
  survive intact and a typo never silently invents a setting.
- **Off is a value.** `config_file=False` keeps an embedded engine from reading a
  stray `agent.yaml` in the working directory of its host, in the same spirit as
  `env=False`; a named file that does not exist raises, because the caller asked
  for that file by name.

## Events

Every observable side effect of a run — printing, streaming to the browser,
logging — is a subscriber on `EventBus` rather than a branch inside the loop.
This is what makes the console renderer deletable (`console=False`), the web
server a pure bridge and telemetry a three-line addition.

A failing subscriber is caught and reported, never propagated: a broken renderer
must not be able to kill a run. Handlers may be sync or async, so the bus is
usable from a thread-shaped consumer without wrapping.

Tool calls are part of that stream rather than a hint that something happened.
`tool.start` once carried nothing but `kind`, so a console or a browser could
show that the model had paused without ever showing what it ran; the loop now
publishes the tool name, the arguments, the outcome, the result and the duration,
and keeps the same records on `RunResult.tools` for a caller that audits a run
afterwards. The arguments come from the run item events of the SDK instead of the
raw argument deltas, because that is where a complete, already assembled call and
its matching output are available. Everything published is passed through
`redact()` first: a run is worth watching only if watching it cannot leak the
credentials the model was handed, and a transcript is worth keeping only if one
tool that returns a megabyte cannot flood it.

## Prompt

The consumer owns the template; the harness only renders it. Jinja runs
sandboxed with `StrictUndefined`, so a typo in a template fails loudly instead of
silently emitting an empty prompt, and a template cannot reach into the host
process. `config` is the single object exposed to the template, which keeps the
contract between application and harness to one name.

## Storage and cache

`Store` and `Cache` are protocols with a SQLite and an in-memory LRU
implementation. The split exists because the two have different lifetimes: the
store is durable and namespaced (sessions today, transcripts tomorrow), the
cache is a bounded, optionally expiring accelerator. Both are constructor
arguments, so Redis or Postgres is a substitution and not a fork.

## Sessions

A session owns a temporary directory and every tool is scoped to it. Isolation
is the point: concurrent runs cannot see each other's files, and a crash cannot
leave content behind on the host. Expiry is checked on access and on every run,
so an abandoned browser tab cannot pin a workspace forever; `keep_workspace` is
the escape hatch for debugging.

`workspace_seed` copies an application content directory into each new workspace,
which is how a consumer ships data to the model without teaching the harness
about it.

Sessions used to live only in a process dictionary, so a restart orphaned every
workspace and forgot every id while the store still held the record. Three
changes closed that gap:

- **The store is the source of truth.** A manager rehydrates the live sessions
  from the store when it is built, and a record that cannot be read, has expired
  or has lost its directory is reconciled away rather than adopted — a restart
  either returns the same session or none at all, never a broken one.
- **Nothing is left behind.** The directories under the workspace root that no
  live session owns are reaped, restricted to a configured root and to the
  manager's own prefix so a shared temporary directory can never be swept up by
  it.
- **Expiry on a timer, a lease on access.** `session_sweep_interval` runs
  expiry and reaping in the background instead of waiting for someone to ask for
  a session, and access renews the lease so a busy session is not expired under
  a client while an abandoned one still runs out.

Durability is opt-in (`session_durable`) because the default promise of the
harness is that a crash leaves no content on the host: with it set a shutdown
keeps the workspaces and their records, without it a shutdown still wipes them.

## Memory

A run used to be one shot: the prompt was rendered, the loop ran, the blocks were
streamed and nothing survived. That was defensible for the CLI and a lie in the
chat UI, where a second message started from zero. A session now owns a
transcript.

Three decisions shaped it:

- **The store, not the process.** Transcripts are persisted through the same
  namespaced `Store` as sessions, so they outlive a reconnect and can outlive a
  restart; the in-process dictionary is only a read-through cache.
- **What is remembered is the exchange, not the prompt.** Replaying the rendered
  template on every turn would repeat the consumer's instructions verbatim and
  spend the context window on them, so the default records the input the user
  wrote and the output of the model. `Agent.remember()` is the hook for a
  consumer that wants another rule, and a failed or empty run is not recorded at
  all so an error cannot poison the next turn.
- **A budget with a fallback, not a token count.** Trimming is by turn count and
  character count because the harness talks to many providers and has no
  trustworthy tokenizer for them; both budgets are configurable, `0` disables
  either, and the newest turn always survives so a small budget cannot erase the
  conversation. When `memory_summary` is set, what falls out is compressed by the
  `summary` model role (falling back to the leader) into one system turn instead
  of being dropped — opt-in, because it costs a model call, and best effort,
  because a failing summarizer must cost the summary rather than the run.

The web layer replays the transcript in the `hello` reply, which is what makes a
reload or a reconnect restore the discussion instead of showing an empty page
attached to a model that remembers. `/forget` clears a transcript, and closing a
session forgets it along with its workspace.

## Resilience

Two failures used to end a run with nothing useful to say: a provider that stops
answering, which hung the caller until it gave up, and a provider that answers
with a refusal, which arrived as a string a consumer had to parse. Both are now
bounded and named.

Naming came first, because retrying is a decision and a decision needs a
category. `ErrorKind` is that category and `classify_error()` assigns it by
exception class name, then by HTTP status, then by whether it is an `OSError` —
in that order, and by *name* rather than by class, so the harness classifies an
`openai` error, an `httpx` error or a provider SDK nobody has written yet
without importing any of them. `ErrorKind.TRANSIENT` is the subset another
identical attempt may get past, which is what `RetryPolicy` consults; everything
else is a decision of the provider and is reported at once. An unrecognized
failure classifies as `internal` and is re-raised unchanged rather than wrapped
in a `ModelError`, because a defect in the harness dressed up as a provider
fault is worse than no classification at all.

Backoff is exponential with jitter, and the jitter is the point: clients that a
rate limit failed together will otherwise come back together and reproduce it.
Retrying is bounded by attempts rather than by a deadline so that a caller can
reason about the worst case from the configuration alone.

The leader and the specialists are shaped differently, so they are bounded
differently. A specialist completion is one request and a timeout around the
whole call is right. The leader's stream is a long lived read whose total
duration is legitimately unbounded — that is what an agentic loop is — so the
same setting becomes a stall guard on the wait for the *next* event instead.
That guard has to know about tools: the SDK runs a tool call inside the stream,
so the budget is extended by `shell_timeout` while a call is outstanding, or a
slow shell command would be indistinguishable from a dead provider. And a stream
is only restarted while it has produced nothing: once a block or a tool call
exists, a retry would replay work the consumer has already seen and re-run tools
that already had effects, so the failure is reported instead. That rule, not the
error kind, is what makes retrying the leader safe.

Retrying in two places at once is worse than retrying in neither: the `openai`
client retries twice on its own by default, so the first end to end test of this
work showed a 503 disappearing with nothing on the bus and nothing in the
accounting. The client is now built with `max_retries=0`. A retry that
the harness cannot see cannot be reported, jittered by the policy that is
supposed to own the decision, or counted against the run.

Accounting was folded into the same change because tokens are only knowable
where the calls are made, and a run makes them in several places: the leader's
stream, a tool delegating to the vision model, the summariser compressing what
fell out of memory. Threading a sink through all of them would have put an
accounting parameter on half the extension points, so the run publishes its
`Usage` in a context variable for the length of `running()` and every call
underneath finds it there — and two concurrent runs never collide, because each
run is its own task. A failed attempt is accounted too: the tokens were spent
whether or not the answer arrived. Pricing is one rate pair per deployment
(`cost_input`, `cost_output`, per million tokens) rather than a table per model,
which covers the common case and leaves the rest to an override of `price`.

## Models

A run has one **leader** model that drives the agentic loop and, optionally,
specialists that tools delegate to. The first specialist is **vision**, added
because the strongest text leaders are frequently blind: with `vision_model`
unset the leader is assumed multimodal and gets `view_image`, which returns a
base64 data URL; with it set the leader gets `describe_image` instead and the
image never enters its context — the specialist answers in a single completion,
without tools and without a loop, and only its text comes back.

`ModelSpec` is the resolved identity of a role and `ModelPool` keeps one client
per endpoint and one SDK model per name, shared across roles, sessions and runs,
so an additional role costs no additional connection. Specialist endpoints and
credentials fall back to the leader's, which makes a second model on the same
provider a single setting. Adding a role is therefore three steps: add the
fields, resolve with `config.model_spec(role)`, call the pool.

The API key is optional. Endpoints that need no auth — a local Ollama, a
forwarding proxy — are configured with an empty key, and the pool hands the SDK
a placeholder rather than letting it refuse to construct a client over a
credential it would never send. A set `OPENAI_API_KEY` still wins, so the SDK's
own environment fallback keeps working.

## Tools

`Workspace` holds the sandboxed primitives and `ToolRegistry` exposes them to the
model. Keeping them apart means the security-critical part — path resolution,
size limits, shell timeouts — is plain synchronous code that can be unit tested
without an SDK, a model or a network.

Paths are resolved through `Path.resolve()` and rejected unless they stay inside
the root, which also settles symlinks: a link pointing outside the workspace
resolves outside it and is refused. Reads are capped, empty files are rejected
before they become malformed data URLs, and shell commands run with an explicit
timeout and are killed when they exceed it, because an unbounded subprocess is
an unbounded hang of the whole event loop.

Every tool is wrapped by one shared guard that turns workspace and repository
errors into text the model can read and act on. A tool that raises ends the run;
a tool that returns `Error: …` lets the model correct itself. The guard preserves
the wrapped signature, since the SDK derives the tool schema from it.

## Repository

A session may be backed by a git checkout, which turns the harness from a text
generator into something that can finish a piece of work. Setting `repo_url` is
enough: on first use the repository is cloned into the session workspace and a
branch of its own (`<prefix>/<session id>`) is checked out from the base branch.

The clone is an ordinary directory inside the workspace, so every existing file
and shell tool already works on it and it is wiped with the session — no second
sandbox, no second path model. `RepoSpec` normalizes `owner/name`, HTTPS, SSH and
filesystem URLs onto one identity so the same configuration works against
github.com, an Enterprise host and a local fixture (which is what makes the git
tests hermetic).

Credential handling is the delicate part and is deliberately narrow: the token is
embedded only in the argument list of the git invocations that need it, the
stored remote is rewritten to the clean URL immediately after cloning, and every
command output and API error is masked before it is surfaced. Tokens shorter
than eight characters are not masked, because replacing a one-character string
would shred unrelated output without protecting anything.

A clone failure is reported and the run continues without a checkout: a
repository is an enhancement of a session, never a precondition for it.

The five git tools (`git_status`, `git_commit`, `git_push`, `open_pull_request`,
`publish_work`) exist as one set with a matching set of slash commands so that a
human and a model drive the same operations through the same code.

## Core

The core was one class until an embedding consumer needed the loop without the
harness: `Agent.__init__` opened a SQLite database and a session manager (and so
a temporary directory) before it could run anything, which is exactly what an
application with its own sessions and history does not want. Splitting it was
cheaper than making each battery optional — `Engine` holds the pieces the loop
genuinely needs (config, events, prompts, tools, models) and `Agent` adds the
rest, so neither class carries a flag for the other's behaviour.

Two extractions made the split clean. `running()` is an async context manager
around the body of a run, so the `agent.start` / `agent.error` / `agent.end`
lifecycle and the "report failures on the result, re-raise cancellation" rule
are written once and shared by both `run()` methods. `turn()` is one pass of the
loop (workspace, tools, SDK agent, stream), so `Agent.run()` differs from
`Engine.run()` only where it should: acquiring a session, rendering a template
and replaying what the session remembers. `stream()` lost its `Session` argument
in the process — it now takes the `RunResult`, whose `session_id` is only a
correlation id, which is what let the loop stop knowing about sessions at all.

## Console

The default renderer is only a subscriber. It prints a banner from
`config.banner_items()`, streams blocks with per-kind styling, and disables
colour when the stream is not a TTY. Consumers subclass it, replace it or drop it
and subscribe their own; nothing in the loop knows it exists.

## Commands

Slash commands power everything the discussion itself does not: session
lifecycle, model and vision switching, and every git operation. One registry
serves the repl and the browser alike, so a command written once is available in
both, and the client discovers the list at handshake time instead of hard coding
it.

## Repl

Running the module with no arguments used to be an error message. It now opens a
terminal, because the shortest path from an installed harness to a conversation
should not be a flag, a browser or a Python file — and because a one shot
`--input` run cannot show the thing the harness is actually built around, a
session that remembers what was said.

`Repl` takes an agent instance and nothing else, which keeps it a client rather
than a mode: it dispatches slash commands to the agent's registry exactly as the
web client does, hands everything else to `agent.run()` and lets the existing
`ConsoleRenderer` do the printing. A consumer opens one on its own subclass with
`await Repl(my_agent).start()`, and `Agent.repl()` is the seam for swapping the
class.

Two details are worth keeping:

- **The terminal must not own the loop.** Lines are read on one dedicated thread
  that is asked for a line at a time, so timers, the session sweeper and a
  streaming run keep running while the prompt waits. One thread, not one per
  line, so a Ctrl-C at the prompt cannot leave two readers racing for the next
  line — and a daemon thread, so a blocked `input()` cannot hold up exit.
- **Ctrl-C cancels the run, not the process.** The interrupt is handled on the
  loop where the platform has a loop handler and through `signal` otherwise; at
  an idle prompt it prints a hint rather than killing a session the user is in
  the middle of. Leaving is `/exit`, `/quit` or Ctrl-D.

The banner is printed once instead of once per run (`ConsoleRenderer.banners`),
which is the only change the repl needed in the console renderer.

## Web layer

REST is read-only state (`/api/health`, `/api/config`, `/api/commands`,
`/api/sessions`, `/api/theme`); everything that changes state travels over the
websocket. One ordered channel per client removes the interleaving problem
between a POST and a stream, and makes cancellation a message rather than a
second endpoint with its own auth story.

Connections subscribe to the channel of their session, and the server is a pure
bridge: it subscribes to the bus, filters events to JSON-safe values and
broadcasts them. `agent.start` is not forwarded, since it carries the resolved
config object.

Attachments are uploaded as data URLs on the prompt message, size-capped, stored
under `attachments/` in the session workspace with sanitized names, and appended
to the prompt as a file listing — so an attachment is just a workspace file the
model can open with the tools it already has.

## Web client

Blocks are content subscribers, not a transcript: a block is created on first
sight of its id and may be updated concurrently and out of order, which is what
allows reasoning, output and tool activity to stream in parallel without the page
having to model turn order.

Markdown is rendered from fully escaped text, so untrusted model output cannot
inject HTML, and URLs are allow-listed (`http(s)`, `mailto`, relative paths and
`data:` for media only) before they become links or players. Images, video and
audio are recognized by extension or data URL and rendered inline.

The UI is mobile first, centred, and shrinks content before wrapping it. It
disables itself while disconnected and reconnects with exponential backoff, so a
dropped socket degrades visibly instead of silently swallowing input.

**One mark says everything about state.** The page used to carry a word for a
title, a second word for the connection and a coloured dot beside it — three
things saying what one shape can. Now a single SVG in the top left is the title
and the status light: its colour, its ring and its motion carry connecting,
connected, working and disconnected. Status that lives in the icon cannot drift
out of sync with the icon, costs no width on a phone, and reads before it is
read. The words stay for a screen reader and in the tooltip, because a shape is
not an accessible name.

**Theming has to survive a theme that names half a palette.** A VS Code theme
defines whatever colours its author cared about, so mapping one key to one
variable left the rest on the built-in dark values — which is how a light theme
used to produce light panels with dark everything else. Two things fix it: each
variable now takes the first of several candidate keys the file actually
defines, and the stylesheet carries a complete fallback palette per theme type,
switched by `data-theme-type` on `<html>`. A theme sets what it knows; the rest
already matches.

**Icons are inline with the code.** A modern icon pack as a dependency means a
font, a build step or a network fetch for a page whose whole point is that it is
three static files. The set is a table of SVG path strings: it inherits
`currentColor`, scales with the box it sits in, themes itself for free, and adds
nothing to load.

**A block is a container, not a paragraph.** Every block has a head that
collapses it (chevron, kind icon, label) and a button group that copies its
text, so a long tool transcript or a long reasoning pass can be folded away
without losing what the model actually said. Clipboard writes fall back to a
hidden selection where the async API is barred, which is every deployment not on
`localhost` or TLS.

**Rendering is coalesced onto a frame.** Re-parsing the whole markdown of a
block on every delta is quadratic and, at the rate a provider emits tokens,
spends more time rendering than painting. `append` marks the body dirty and the
paint happens on the next animation frame, so the cost is one render per frame
regardless of token rate and the text still appears as it arrives.

## End to end capture

`agent_e2e.py` exists because the page was the one subsystem nothing looked at.
Every other part is asserted by `agent_test.py`, while the client was only ever
checked by a person opening a browser — so the two defects below sat in plain
sight.

It serves the real web layer on a background thread and drives it with
Playwright, replacing only the three seams that reach a provider
(`build_tools`, `build_sdk_agent`, `stream`); sessions, memory, the event bridge,
the hub, the markup, the styles and the client are all the shipped ones, so a
capture is a test of the page rather than of a mock of it. The scripted run parks
itself in the middle of its answer until the capture releases it, because the
alternative — sleeping and hoping — photographs a different frame every time.
Nine images (three moments across a desktop, a phone in portrait and a phone in
landscape) are written to `assets/` and shown in [`GUIDE.md`](GUIDE.md), which
makes a layout regression something a reader can see in a diff.

The scripted run publishes a reasoning block before it calls its tool, so the
capture covers the three kinds of block the page draws rather than two, and the
middle moment is asserted as well as photographed: the reasoning must be on the
page and the answer must be there *and* incomplete. A client that buffered a
block until it ended would still take a plausible looking screenshot; it would
not pass that assertion.

## Consumer port: storynu

`utils/storynu` was rewritten onto the harness to prove the boundary: it owns
`story_prompt.md` (a Jinja template that injects `config.input`) and `story.py`,
which sets the model, the instructions, story specific config values and an
exporter that copies the finished story out of the session workspace. Everything
else it used to carry — argument parsing, streaming, tools, sandboxing — is the
harness.

---

## Phase log

1. Flat package scaffold: `pyproject.toml`, the subsystem layout of `agent.py`
   and the placeholder page assets.
2. Config subsystem: `AgentConfig` with layered resolution, input resolution,
   secret masking and validation.
3. Event subsystem: typed `Event`/`EventBus` with sync and async subscribers.
4. Prompt subsystem: sandboxed, strict Jinja rendering of a consumer template.
5. Storage subsystem: `Store` + `SqliteStore` and `Cache` + `LruCache`.
6. Session subsystem: isolated workspaces, expiry and guaranteed cleanup.
7. Tool subsystem: workspace scoped file/shell/image/patch tools and a registry.
8. Core `Agent`: construction from config, `run()`, block streaming,
   cancellation and overridable hooks.
9. Console renderer: banner and block aware streaming printer, as a subscriber.
10. CLI layer: `main()` with `--input` and `--serve`.
11. Web layer: REST plus a websocket hub bridging block events to clients.
12. Web client: chat UI with out-of-order blocks, markdown and media preview,
    attachments, connection state and a mobile first layout.
13. Slash commands: one registry shared by the CLI and the UI.
14. VS Code theme support mapped onto CSS custom properties.
15. Unit tests over config, prompts, events, storage, cache, sessions, tool path
    scoping, command parsing and the protocol codec.
16. Ported `storynu` onto the harness.
17. Fixed the defects found while porting (below).
18. `README.md`: overview, subsystem map, extension points and usage.
19. Multi-model support: `ModelSpec`/`ModelPool` and the vision specialist.
20. Repository support: `repo_*` config, `RepoSpec`/`RepoManager`/`Checkout`,
    session branches, the five git tools and the matching slash commands.
21. Documentation split: this file, [`GUIDE.md`](GUIDE.md), a README reduced to
    a summary and a quickstart, and a TODO of what is still open.
22. Conversation memory: `Turn`/`Transcript`/`ConversationMemory`, replay into
    the model and into the client, budgeted trimming with optional summarising,
    and `/forget`.
23. Durable sessions: rehydration from the store, reconciliation and reaping of
    unowned workspaces, a background expiry sweeper and `session_durable`.
24. Embeddable core: the loop split out as `Engine` (config, events, prompts,
    tools, models) with `Agent` subclassing it for the batteries, a shared
    `running()` lifecycle and `turn()`, model input taken verbatim, tools from
    the caller or from an optional workspace, and `env=False` for hosts that do
    not want the `AGENT_*` environment read.
25. Resilience of model calls: the `ErrorKind` taxonomy and `classify_error`,
    `RetryPolicy` (per-attempt timeouts, bounded retries, exponential backoff
    with jitter), the stall guard around the leader's stream, `model.retry` on
    the bus, and `Usage` accounting with pricing on `agent.end`.
26. Terminal repl and config files: `Repl` as the default entry point of
    `agent.py`, `--repl` and `--config` beside `--input` and `--serve`, the JSON
    and YAML file layer between the defaults and the environment
    (`find_config_file`, `read_config_file`, `AgentConfig.with_file`), and a
    template that the config file can name.
27. End to end capture: `agent_e2e.py`, a scripted agent behind the real server,
    a Playwright pass over desktop and phone viewports, and the screenshots in
    `assets/` that `GUIDE.md` shows.

## Fixes worth remembering

- **Hard coded credentials.** The pre-harness code carried an API key in the
  source. Credentials are now config fields, recognized by name and masked
  everywhere they are serialized.
- **Blocking IO on the event loop.** File reads, writes and subprocesses ran
  inline in async tools and stalled streaming. Synchronous primitives now run in
  a thread and shells use `asyncio` subprocesses.
- **Unbounded shells.** A command with no timeout hung the loop with no way back.
  Shell execution has an explicit timeout, kills the process on expiry and
  reports it as text.
- **Path handling duplicated per tool.** Each tool resolved and validated its own
  paths, so escapes depended on which tool was called. `Workspace.resolve()` is
  now the only path gate.
- **Output paths escaping the sandbox.** Results were written wherever the caller
  asked; export is now an explicit consumer step out of the session workspace.
- **Tool errors ending runs.** Exceptions from tools aborted the loop instead of
  informing the model. One shared guard turns them into `Error: …` text, and it
  is shared by the file and repository tools so both behave identically.
- **Model calls with no bound.** A stalled provider hung a run until the client
  gave up and a rate limit ended it outright. Every call is now made under a
  `RetryPolicy`: a timeout per attempt (a stall guard for the leader's stream),
  bounded retries with jittered backoff on transient kinds only, and a stream
  that is only restarted while it has produced nothing.
- **Tokens reaching disk.** An authenticated clone URL persists in `.git/config`
  by default; the remote is rewritten to the clean URL right after cloning and
  all git output is masked.
- **A missing key refused to start keyless endpoints.** The pool passed no key
  when none was configured and relied on the SDK's `OPENAI_API_KEY` fallback —
  but when that lookup also found nothing, the SDK refused to construct a client
  and a local Ollama could not be reached over a credential it never asks for.
  An empty key now falls back to a placeholder, with a set `OPENAI_API_KEY`
  still winning.
- **Two content types on every asset.** The response header map appends rather
  than overwrites, so setting `Content-Type` on top of the `text/plain` the
  transport had already written sent both — and the browser believed the first.
  The stylesheet was fetched and ignored on every load, and the page had been
  rendering unstyled since the web layer was written. `WebServer.retype()` now
  clears the header before setting it.
- **A hidden button that stayed on screen.** `.icon-button { display: grid }`
  outranks the browser rule behind the `hidden` attribute, so hiding send and
  showing stop did neither: both sat in the composer through every run. A single
  `[hidden] { display: none !important }` rule restores the attribute.
