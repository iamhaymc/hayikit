"""An application agnostic agent harness.

The module is organized as a set of small, compartmentalized subsystems that can
each be replaced by a consumer:

| Subsystem | Entry points                                     |
| --------- | ------------------------------------------------ |
| config    | `AgentConfig`                                    |
| events    | `Event`, `EventBus`, `EventType`                 |
| prompt    | `PromptRenderer`                                 |
| storage   | `Store`, `SqliteStore`, `Cache`, `LruCache`      |
| sessions  | `Session`, `SessionManager`                      |
| memory    | `Turn`, `Transcript`, `ConversationMemory`       |
| repository| `RepoSpec`, `RepoManager`, `Checkout`            |
| resilience| `ErrorKind`, `RetryPolicy`, `Usage`              |
| models    | `ModelSpec`, `ModelPool`                         |
| tools     | `Workspace`, `ToolRegistry`                      |
| console   | `ConsoleRenderer`                                |
| commands  | `CommandRegistry`                                |
| core      | `Engine`, `Agent`, `RunResult`                   |
| repl      | `Repl`                                           |
| web       | `Hub`, `WebServer`                               |
| cli       | `main`                                           |

In the simplest case a consumer owns a prompt template and does no more than::

    import asyncio, agent
    asyncio.run(agent.Agent().run(MY_TEMPLATE))

An application that already has its own sessions, history and storage embeds
the loop on its own instead, with none of the batteries above::

    engine = agent.Engine(api_key=..., model=..., env=False)
    result = await engine.run(my_messages, tools=my_tools)

The module also works as a data driven application. With no arguments it opens
an interactive terminal; ``--input`` performs one run and ``--serve`` starts the
web layer::

    python agent.py                     # chat in the terminal
    python agent.py --input notes.md    # one run, streamed to the console
    python agent.py --serve             # REST API, websocket hub and chat UI

Configuration is read from ``agent.json``, ``agent.yaml`` or ``agent.yml`` — the
current directory first, then next to this file — before the ``AGENT_*``
environment; see `find_config_file` and `main`.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import contextvars
import functools
import inspect
import json
import mimetypes
import os
import queue
import random
import re
import shutil
import signal
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Protocol,
    runtime_checkable,
)

__all__ = [
    "Agent",
    "AgentConfig",
    "Block",
    "Cache",
    "Checkout",
    "CommandRegistry",
    "ConsoleRenderer",
    "ConversationMemory",
    "Engine",
    "ErrorKind",
    "Event",
    "EventBus",
    "EventType",
    "Hub",
    "LruCache",
    "ModelError",
    "ModelPool",
    "ModelSpec",
    "PromptRenderer",
    "RepoError",
    "RepoManager",
    "RepoSpec",
    "Repl",
    "RetryPolicy",
    "RunResult",
    "Session",
    "SessionManager",
    "SqliteStore",
    "Store",
    "ToolCall",
    "ToolRegistry",
    "Transcript",
    "Turn",
    "Usage",
    "WebServer",
    "Workspace",
    "find_config_file",
    "main",
    "read_config_file",
]

ROOT = Path(__file__).resolve().parent
STEM = Path(__file__).stem

Handler = Callable[["Event"], Any]

# ---------------------------------------------------------------------------
# Config
#
# Values are resolved in layers: dataclass defaults, then a JSON or YAML config
# file, then the environment (``AGENT_*``), then whatever the consumer or the
# CLI passes in.
# ---------------------------------------------------------------------------

ENV_PREFIX = "AGENT_"

DEFAULT_API_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash-0731"
DEFAULT_INSTRUCTIONS = "You are a helpful, autonomous assistant."
DEFAULT_VISION_INSTRUCTIONS = (
    "You describe images precisely and factually for another agent that cannot "
    "see them. Answer the question when one is given, otherwise describe the "
    "image: its subject, layout, text and anything notable."
)

DEFAULT_SUMMARY_INSTRUCTIONS = (
    "You compress a conversation for another agent that has forgotten it. "
    "Rewrite the exchange below as a compact, factual briefing in the third "
    "person: decisions, facts, names, files, open questions. No preamble."
)

#: Model roles the harness knows about. `leader` drives the agentic loop; the
#: others are specialists delegated to from tools or from the harness itself.
LEADER_ROLE = "leader"
VISION_ROLE = "vision"
SUMMARY_ROLE = "summary"

SECRET_KEYS = ("api_key", "key", "token", "secret", "password")


def is_secret_key(name: str) -> bool:
    """True when a config field name denotes a credential (``*_api_key``, ...)."""
    lowered = name.lower()
    return any(lowered == key or lowered.endswith(f"_{key}") for key in SECRET_KEYS)


def mask_secret(value: str) -> str:
    """Mask a secret so only its head and tail remain visible."""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:8]}...{value[-4:]}"


def _field_kind(annotation: str) -> Any:
    """Pick the runtime type an environment string should be coerced into."""
    for name, kind in (("bool", bool), ("int", int), ("float", float), ("Path", Path)):
        if name in annotation:
            return kind
    return str


def _coerce(value: str, kind: Any) -> Any:
    """Coerce an environment string into the type of a config field."""
    if kind is bool:
        return value.strip().lower() in ("1", "true", "yes", "on")
    if kind is int:
        return int(value)
    if kind is float:
        return float(value)
    if kind is Path:
        return Path(value).expanduser()
    return value


def _coerce_value(value: Any, kind: Any) -> Any:
    """Coerce a value parsed out of a config file into a config field type."""
    if isinstance(value, str):
        return _coerce(value, kind)
    if kind is Path and isinstance(value, Path):
        return value.expanduser()
    if kind is bool and isinstance(value, (int, float)):
        return bool(value)
    if kind is int and isinstance(value, float) and value.is_integer():
        return int(value)
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


# Config files. A file named after this module (``agent.json``, ``agent.yaml``
# or ``agent.yml``) is the layer between the dataclass defaults and the
# environment. The current directory wins over the directory this module lives
# in, so a consumer overrides the defaults shipped beside the harness.

CONFIG_SUFFIXES = (".json", ".yaml", ".yml")


def config_directories(directories: Iterable[Path | str] | None = None) -> list[Path]:
    """The directories a config file is looked for in, nearest first."""
    raw = [Path.cwd(), ROOT] if directories is None else [Path(d) for d in directories]
    seen: set[str] = set()
    out: list[Path] = []
    for directory in raw:
        resolved = Path(directory).expanduser()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        out.append(resolved)
    return out


def config_candidates(
    stem: str = "", directories: Iterable[Path | str] | None = None
) -> list[Path]:
    """Every config file path that is looked at, in order of precedence."""
    stem = stem or STEM
    return [
        directory / f"{stem}{suffix}"
        for directory in config_directories(directories)
        for suffix in CONFIG_SUFFIXES
    ]


def find_config_file(
    stem: str = "", directories: Iterable[Path | str] | None = None
) -> Path | None:
    """The first config file that exists, or ``None`` when there is none."""
    for path in config_candidates(stem, directories):
        if path.is_file():
            return path
    return None


def read_config_file(path: Path | str) -> dict[str, Any]:
    """Read a JSON or YAML config file into a mapping.

    Parsed with `omegaconf` when it is installed, which also resolves
    interpolations such as ``api_key: ${oc.env:AGENT_API_KEY}`` and
    ``model: ${defaults.model}``. Without it JSON still works and YAML falls
    back to `pyyaml`.
    """
    file = Path(path).expanduser()
    text = file.read_text(encoding="utf-8")
    data = json.loads(text) if file.suffix.lower() == ".json" else _parse_yaml(text)
    data = _resolve_interpolations(data)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{file} must contain a mapping of settings")
    return data


def _parse_yaml(text: str) -> Any:
    """Parse YAML with whichever of omegaconf or pyyaml is installed."""
    with contextlib.suppress(ImportError):
        from omegaconf import OmegaConf

        return OmegaConf.create(text)
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError(
            "reading a YAML config file needs the 'omegaconf' (or 'pyyaml') package"
        ) from exc
    return yaml.safe_load(text)


def _resolve_interpolations(data: Any) -> Any:
    """Resolve ``${...}`` references and return plain Python containers."""
    with contextlib.suppress(ImportError):
        from omegaconf import OmegaConf

        if not OmegaConf.is_config(data):
            if not isinstance(data, (dict, list)):
                return data
            data = OmegaConf.create(data)
        return OmegaConf.to_container(data, resolve=True)
    return data


def flatten_config(data: dict[str, Any], known: Iterable[str]) -> dict[str, Any]:
    """Flatten the nested groups a config file may use onto field names.

    ``vision: {model: v}`` becomes ``vision_model``, but only when every key of
    the group names a real field; anything else is left alone and ends up in
    `AgentConfig.extras`. A nested ``extras`` mapping is folded in as is.
    """
    fields_ = set(known)
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key == "extras":
            if isinstance(value, dict):
                out.update(value)
            continue
        if (
            isinstance(value, dict)
            and value
            and all(f"{key}_{sub}" in fields_ for sub in value)
        ):
            out.update({f"{key}_{sub}": item for sub, item in value.items()})
            continue
        out[key] = value
    return out


@dataclass(frozen=True)
class ModelSpec:
    """An addressable model: which model, on which endpoint, with which key.

    Roles (`leader`, `vision`, ...) resolve to one of these, and a `ModelPool`
    reuses a single client per endpoint so several roles cost one connection.
    """

    role: str
    name: str
    api_url: str
    api_key: str = ""

    @property
    def endpoint(self) -> tuple[str, str]:
        return (self.api_url, self.api_key)


@dataclass
class AgentConfig:
    """Everything an `Agent` needs to run. Consumers may subclass or extend it.

    Application specific values live in `extras` and are reachable from prompt
    templates as ``config.extras.<name>`` or ``config.<name>``.
    """

    name: str = "agent"
    instructions: str = DEFAULT_INSTRUCTIONS
    model: str = DEFAULT_MODEL
    api_url: str = DEFAULT_API_URL
    api_key: str = ""
    max_turns: int = 100
    # Resilience of model calls. Every request to a provider is bounded by a
    # timeout and tried again on a transient failure (a timeout, a rate limit, a
    # connection error or a 5xx) with exponential backoff and jitter. For the
    # leader the timeout is a stall guard — the longest the stream may say
    # nothing, extended by `shell_timeout` while a tool call is outstanding —
    # and its stream is only restarted while it has produced nothing.
    request_timeout: float = 120.0
    retry_attempts: int = 3
    retry_backoff: float = 0.5
    retry_max_backoff: float = 30.0
    retry_jitter: float = 0.5
    # What a million input / output tokens cost, in whatever currency the
    # provider bills. Left at zero, `agent.end` reports tokens without a price.
    cost_input: float = 0.0
    cost_output: float = 0.0
    # Vision specialist. When `vision_model` is set the leader model never sees
    # image data: image tools delegate to this model and return its answer as
    # text, so a leader without vision can still work with images. The endpoint
    # and credential fall back to the leader's when left empty.
    vision_model: str = ""
    vision_api_url: str = ""
    vision_api_key: str = ""
    vision_instructions: str = DEFAULT_VISION_INSTRUCTIONS
    vision_max_tokens: int = 1024
    # Conversation memory. Every run appends the exchange to the transcript of
    # its session, and the transcript is replayed to the model on the next run
    # until it exceeds one of the budgets below. When `memory_summary` is set
    # the turns that fall out are compressed by the `summary` role instead of
    # being dropped; that role falls back to the leader when it is unset.
    memory_enabled: bool = True
    memory_max_turns: int = 40
    memory_max_chars: int = 24000
    memory_summary: bool = False
    summary_model: str = ""
    summary_api_url: str = ""
    summary_api_key: str = ""
    summary_instructions: str = DEFAULT_SUMMARY_INSTRUCTIONS
    summary_max_tokens: int = 512
    # Input as given on the command line (a file path or raw text) and the raw
    # text it resolves to. Templates read `config.input`.
    input_source: str = ""
    input: str = ""
    # Prompt template used by the data driven application: a file path or the
    # template itself. Left empty, `load_template` looks for `agent_prompt.md`.
    template: str = ""
    # Workspaces / sessions
    workspace_root: Path | None = None
    # Directory copied into every new session workspace (application content).
    workspace_seed: Path | None = None
    session_ttl: int = 3600
    # How often (in seconds) live sessions are swept for expiry and the
    # workspace root for directories without an owner. ``0`` disables the timer
    # and leaves expiry to be noticed on access.
    session_sweep_interval: int = 60
    # Durable sessions: workspaces and store records survive a shutdown and are
    # rehydrated on the next start instead of being wiped with the process.
    session_durable: bool = False
    keep_workspace: bool = False
    # Default git repository. When `repo_url` is set every new session clones it
    # into the workspace and checks out a session branch, and the agent can
    # commit, push and open a pull request from that checkout.
    repo_url: str = ""
    repo_branch: str = ""
    repo_token: str = ""
    repo_remote: str = "origin"
    repo_branch_prefix: str = "agent"
    repo_dir: str = ""
    repo_author_name: str = ""
    repo_author_email: str = ""
    repo_depth: int = 1
    repo_clone: bool = True
    repo_timeout: int = 300
    # Storage
    db_path: Path | None = None
    cache_size: int = 256
    # Tools
    shell_timeout: int = 120
    shell_enabled: bool = True
    # Web
    host: str = "127.0.0.1"
    port: int = 8765
    theme: Path | None = None
    # Console
    color: bool | None = None
    quiet: bool = False
    # Free form application values
    extras: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        # Only called when normal attribute lookup fails, which keeps template
        # access to application values (`config.foo`) working.
        if name.startswith("_"):
            raise AttributeError(name)
        extras = self.__dict__.get("extras") or {}
        if name in extras:
            return extras[name]
        raise AttributeError(name)

    def merge(self, **overrides: Any) -> "AgentConfig":
        """Return a copy with non-``None`` overrides applied."""
        known = {f.name for f in fields(self)}
        values = {k: v for k, v in overrides.items() if k in known and v is not None}
        extras = dict(self.extras)
        extras.update(
            {k: v for k, v in overrides.items() if k not in known and v is not None}
        )
        return replace(self, extras=extras, **values)

    def with_env(self, environ: dict[str, str] | None = None) -> "AgentConfig":
        """Return a copy with ``AGENT_<FIELD>`` environment overrides applied."""
        environ = os.environ if environ is None else environ
        overrides: dict[str, Any] = {}
        for f in fields(self):
            if f.name == "extras":
                continue
            raw = environ.get(f"{ENV_PREFIX}{f.name.upper()}")
            if raw is None or raw == "":
                continue
            with contextlib.suppress(ValueError):
                overrides[f.name] = _coerce(raw, _field_kind(str(f.type)))
        return self.merge(**overrides)

    def with_data(self, data: dict[str, Any]) -> "AgentConfig":
        """Return a copy with a parsed mapping applied, coerced to field types.

        Values arrive from a config file, so they are already typed; strings are
        coerced the way the environment layer coerces them, unknown keys go to
        `extras` and a ``null`` leaves the default in place.
        """
        known = {f.name: f for f in fields(self)}
        overrides: dict[str, Any] = {}
        for key, value in flatten_config(data, known).items():
            declared = known.get(key)
            if declared is None:
                overrides[key] = value
                continue
            with contextlib.suppress(ValueError, TypeError):
                overrides[key] = _coerce_value(value, _field_kind(str(declared.type)))
        return self.merge(**overrides)

    def with_file(self, path: Path | str | None = None) -> "AgentConfig":
        """Return a copy with the values of a JSON or YAML config file applied.

        Without a path the file is the one `find_config_file` locates:
        ``agent.json``, ``agent.yaml`` or ``agent.yml``, taken from the current
        directory first and from the directory of ``agent.py`` as a fallback.
        Missing files are not an error when nothing was asked for by name.
        """
        file = Path(path).expanduser() if path else find_config_file()
        if file is None:
            return self
        return self.with_data(read_config_file(file))

    def model_spec(self, role: str = LEADER_ROLE) -> ModelSpec | None:
        """Resolve `role` to a `ModelSpec`, or ``None`` when it is not configured.

        Specialist roles inherit the leader's endpoint and credential unless
        they declare their own, so a second model usually costs one setting.
        """
        if role == LEADER_ROLE:
            return ModelSpec(LEADER_ROLE, self.model, self.api_url, self.api_key)
        name = getattr(self, f"{role}_model", "") or ""
        if not name:
            return None
        return ModelSpec(
            role,
            name,
            getattr(self, f"{role}_api_url", "") or self.api_url,
            getattr(self, f"{role}_api_key", "") or self.api_key,
        )

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        """Serialize the config, masking secrets by default."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, Path):
                value = str(value)
            if redact and isinstance(value, str) and is_secret_key(f.name):
                value = mask_secret(value)
            out[f.name] = value
        return out

    def banner_items(self) -> list[tuple[str, str]]:
        """Key/value rows describing the config, used by `ConsoleRenderer`."""
        items = [
            ("Name", self.name),
            ("Model", self.model),
            ("API URL", self.api_url),
            ("API Key", mask_secret(self.api_key) or "(unset)"),
        ]
        vision = self.model_spec(VISION_ROLE)
        if vision is not None:
            items.append(("Vision Model", vision.name))
        if self.repo_url:
            items.append(("Repository", self.repo_url))
        items.append(("Input", self.input_source or "(inline)"))
        return items


def resolve_input(raw: str) -> str:
    """Return input text, reading it from a file when `raw` is a file path."""
    if not raw:
        return ""
    candidate = raw.strip()
    if len(candidate) < 4096 and "\n" not in candidate:
        path = Path(candidate).expanduser()
        with contextlib.suppress(OSError, ValueError):
            if path.is_file():
                return path.read_text(encoding="utf-8")
    return raw


# ---------------------------------------------------------------------------
# Events
#
# Every observable side effect of a run (printing, streaming, persistence) is a
# subscriber of this bus, so consumers hook in instead of patching the harness.
# ---------------------------------------------------------------------------


class EventType:
    """Well known event names. Any string is a valid event name."""

    AGENT_START = "agent.start"
    AGENT_END = "agent.end"
    AGENT_ERROR = "agent.error"
    BLOCK_START = "block.start"
    BLOCK_DELTA = "block.delta"
    BLOCK_END = "block.end"
    TOOL_START = "tool.start"
    TOOL_END = "tool.end"
    MODEL_RETRY = "model.retry"
    SESSION_OPEN = "session.open"
    SESSION_CLOSE = "session.close"
    LOG = "log"

    ALL = "*"


@dataclass(slots=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    time: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "data": self.data,
            "session": self.session_id,
            "time": self.time,
        }


class EventBus:
    """A tiny publish/subscribe bus supporting sync and async subscribers."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}

    def on(self, event_type: str, handler: Handler) -> Callable[[], None]:
        """Subscribe to an event type (or `EventType.ALL`). Returns an unsubscriber."""
        self._handlers.setdefault(event_type, []).append(handler)
        return lambda: self.off(event_type, handler)

    def off(self, event_type: str, handler: Handler) -> None:
        handlers = self._handlers.get(event_type)
        if handlers and handler in handlers:
            handlers.remove(handler)

    def clear(self) -> None:
        self._handlers.clear()

    def subscribers(self, event_type: str) -> list[Handler]:
        return [*self._handlers.get(event_type, []), *self._handlers.get(EventType.ALL, [])]

    async def emit(self, event: Event) -> None:
        """Deliver an event; a failing subscriber never breaks the run."""
        for handler in self.subscribers(event.type):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[events] subscriber error: {exc!r}", file=sys.stderr)

    async def publish(self, event_type: str, /, session_id: str | None = None, **data: Any) -> Event:
        event = Event(type=event_type, data=data, session_id=session_id)
        await self.emit(event)
        return event


# ---------------------------------------------------------------------------
# Prompt
#
# The consumer owns the template; the harness renders it against the config it
# resolved for itself. `config.input` holds the resolved input text.
# ---------------------------------------------------------------------------


class PromptRenderer:
    """Renders Jinja templates against a config object."""

    def __init__(
        self,
        search_path: Path | str | None = None,
        *,
        strict: bool = True,
        sandboxed: bool = True,
    ) -> None:
        from jinja2 import StrictUndefined, Undefined
        from jinja2 import ChoiceLoader, FileSystemLoader
        from jinja2.sandbox import SandboxedEnvironment
        from jinja2 import Environment

        loaders = []
        if search_path is not None:
            loaders.append(FileSystemLoader(str(search_path)))
        env_cls = SandboxedEnvironment if sandboxed else Environment
        self.env = env_cls(
            loader=ChoiceLoader(loaders) if loaders else None,
            undefined=StrictUndefined if strict else Undefined,
            keep_trailing_newline=True,
            autoescape=False,
        )

    def render(self, template: str, config: Any = None, **context: Any) -> str:
        """Render a template string. `config` is exposed as ``config``."""
        return self.env.from_string(template).render(config=config, **context).strip()

    def render_file(self, path: Path | str, config: Any = None, **context: Any) -> str:
        """Render a template file from disk."""
        text = Path(path).expanduser().read_text(encoding="utf-8")
        return self.render(text, config, **context)


# ---------------------------------------------------------------------------
# Storage
#
# A namespaced key/value `Store` (SQLite by default) and a `Cache` (in-memory
# LRU by default). Both are protocols so other backends can be dropped in.
# ---------------------------------------------------------------------------


@runtime_checkable
class Store(Protocol):
    """Durable namespaced key/value storage."""

    def get(self, namespace: str, key: str) -> Any | None: ...
    def set(self, namespace: str, key: str, value: Any) -> None: ...
    def delete(self, namespace: str, key: str) -> None: ...
    def list(self, namespace: str) -> list[tuple[str, Any]]: ...
    def close(self) -> None: ...


@runtime_checkable
class Cache(Protocol):
    """Fast, volatile storage."""

    def get(self, key: str) -> Any | None: ...
    def set(self, key: str, value: Any) -> None: ...
    def delete(self, key: str) -> None: ...
    def clear(self) -> None: ...


class MemoryStore:
    """In-process `Store`, used for tests and ephemeral runs."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    def get(self, namespace: str, key: str) -> Any | None:
        return self._data.get(namespace, {}).get(key)

    def set(self, namespace: str, key: str, value: Any) -> None:
        self._data.setdefault(namespace, {})[key] = value

    def delete(self, namespace: str, key: str) -> None:
        self._data.get(namespace, {}).pop(key, None)

    def list(self, namespace: str) -> list[tuple[str, Any]]:
        return list(self._data.get(namespace, {}).items())

    def close(self) -> None:
        self._data.clear()


class SqliteStore:
    """SQLite backed `Store`. Values are JSON encoded."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = ":memory:" if path is None else str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS kv ("
            " ns TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,"
            " updated_at REAL NOT NULL, PRIMARY KEY (ns, key))"
        )
        self._db.commit()

    def get(self, namespace: str, key: str) -> Any | None:
        row = self._db.execute(
            "SELECT value FROM kv WHERE ns = ? AND key = ?", (namespace, key)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def set(self, namespace: str, key: str, value: Any) -> None:
        self._db.execute(
            "INSERT INTO kv (ns, key, value, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(ns, key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (namespace, key, json.dumps(value), time.time()),
        )
        self._db.commit()

    def delete(self, namespace: str, key: str) -> None:
        self._db.execute("DELETE FROM kv WHERE ns = ? AND key = ?", (namespace, key))
        self._db.commit()

    def list(self, namespace: str) -> list[tuple[str, Any]]:
        rows = self._db.execute(
            "SELECT key, value FROM kv WHERE ns = ? ORDER BY updated_at", (namespace,)
        ).fetchall()
        return [(key, json.loads(value)) for key, value in rows]

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self._db.close()


class LruCache:
    """In-memory LRU `Cache` backed by ``cachetools``."""

    def __init__(self, maxsize: int = 256, ttl: float | None = None) -> None:
        import cachetools

        self._cache: Any = (
            cachetools.TTLCache(maxsize=maxsize, ttl=ttl)
            if ttl
            else cachetools.LRUCache(maxsize=maxsize)
        )

    def get(self, key: str) -> Any | None:
        return self._cache.get(key)

    def set(self, key: str, value: Any) -> None:
        self._cache[key] = value

    def delete(self, key: str) -> None:
        self._cache.pop(key, None)

    def clear(self) -> None:
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)


# ---------------------------------------------------------------------------
# Sessions
#
# A session owns an isolated workspace directory. When it is closed or expires
# the directory is removed, leaving no trace on the host.
#
# The record of a live session is written to the `Store`, so a manager built
# over the same store rehydrates what was live when the process stopped,
# reaps the workspaces that no longer have an owner, and expires the rest on a
# timer rather than only when someone asks for them.
# ---------------------------------------------------------------------------


@dataclass
class Session:
    id: str
    workspace: Path
    created_at: float
    expires_at: float
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def touch(self, ttl: float) -> None:
        self.expires_at = time.time() + ttl

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace": str(self.workspace),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        """Rebuild a session from a stored record."""
        return cls(
            id=str(data["id"]),
            workspace=Path(str(data["workspace"])),
            created_at=float(data.get("created_at") or 0.0),
            expires_at=float(data.get("expires_at") or 0.0),
            meta=dict(data.get("meta") or {}),
        )


class SessionManager:
    """Creates, tracks and destroys isolated session workspaces."""

    NAMESPACE = "sessions"

    def __init__(
        self,
        *,
        root: Path | None = None,
        ttl: float = 3600,
        store: Store | None = None,
        cache: Cache | None = None,
        prefix: str = "agent",
        keep_workspace: bool = False,
        seed: Path | None = None,
        durable: bool = False,
        sweep_interval: float = 0,
        restore: bool = True,
    ) -> None:
        self.root = Path(root).expanduser() if root else None
        self.seed = Path(seed).expanduser() if seed else None
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self.store = store
        self.cache = cache
        self.prefix = prefix
        self.keep_workspace = keep_workspace
        self.durable = durable
        self.sweep_interval = sweep_interval
        self._sessions: dict[str, Session] = {}
        self._sweeper: "asyncio.Task[None] | None" = None
        if restore:
            self.restore()

    def create(self, **meta: Any) -> Session:
        session_id = uuid.uuid4().hex[:12]
        workspace = Path(
            tempfile.mkdtemp(prefix=f"{self.prefix}-{session_id}-", dir=self.root)
        ).resolve()
        if self.seed is not None and self.seed.is_dir():
            shutil.copytree(self.seed, workspace, dirs_exist_ok=True, symlinks=False)
        now = time.time()
        session = Session(
            id=session_id,
            workspace=workspace,
            created_at=now,
            expires_at=now + self.ttl,
            meta=meta,
        )
        self._sessions[session_id] = session
        self._persist(session)
        return session

    def get(self, session_id: str | None) -> Session | None:
        """Return a live session, renewing its lease, or ``None``.

        Access renews the lease so an active session is never expired under a
        client; an idle one is left to the sweeper or to the next access.
        """
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.expired:
            self.close(session_id)
            return None
        session.touch(self.ttl)
        self._persist(session)
        return session

    def ensure(self, session_id: str | None = None, **meta: Any) -> Session:
        """Return an existing live session or create a new one."""
        return self.get(session_id) or self.create(**meta)

    def update(self, session: Session) -> Session:
        """Persist changes made to the metadata of a live session."""
        self._persist(session)
        return session

    def list(self) -> list[Session]:
        return sorted(self._sessions.values(), key=lambda s: s.created_at)

    def close(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        self._destroy(session)
        return True

    def purge_expired(self) -> list[str]:
        expired = [s.id for s in list(self._sessions.values()) if s.expired]
        for session_id in expired:
            self.close(session_id)
        return expired

    def close_all(self, destroy: bool | None = None) -> None:
        """Release every live session.

        Durable managers only forget them: the workspaces and the store records
        are what `restore()` needs on the next start. Pass ``destroy`` to decide
        explicitly.
        """
        if destroy is None:
            destroy = not self.durable
        if not destroy:
            self._sessions.clear()
            return
        for session_id in list(self._sessions):
            self.close(session_id)

    # -- durability ---------------------------------------------------------

    def restore(self) -> list[Session]:
        """Rehydrate the sessions the store still holds a record of.

        A record whose workspace is gone, whose lease has run out or which
        cannot be read is reconciled away instead of being adopted, so a
        restart never hands out a session that no longer has a directory.
        """
        if self.store is None:
            return []
        restored: list[Session] = []
        for session_id, record in self.store.list(self.NAMESPACE):
            try:
                session = Session.from_dict(record)
            except (AttributeError, KeyError, TypeError, ValueError):
                self.store.delete(self.NAMESPACE, session_id)
                if self.cache is not None:
                    self.cache.delete(session_id)
                continue
            if session.expired or not session.workspace.is_dir():
                self._destroy(session)
                continue
            self._sessions[session.id] = session
            if self.cache is not None:
                self.cache.set(session.id, session.to_dict())
            restored.append(session)
        return restored

    def orphans(self) -> list[Path]:
        """Workspace directories under the root that no live session owns.

        Only a configured root is scanned, and only the directories this
        manager could have created: a shared temporary directory is never
        touched.
        """
        if self.root is None or not self.root.is_dir():
            return []
        owned = {str(session.workspace) for session in self._sessions.values()}
        found: list[Path] = []
        with contextlib.suppress(OSError):
            for path in sorted(self.root.iterdir()):
                if not path.is_dir() or not path.name.startswith(f"{self.prefix}-"):
                    continue
                if str(path) in owned or str(path.resolve()) in owned:
                    continue
                found.append(path)
        return found

    def reap_orphans(self) -> list[Path]:
        """Remove the orphaned workspaces, unless workspaces are kept."""
        if self.keep_workspace:
            return []
        reaped: list[Path] = []
        for path in self.orphans():
            shutil.rmtree(path, ignore_errors=True)
            reaped.append(path)
        return reaped

    def sweep(self) -> tuple[list[str], list[Path]]:
        """Expire what is due and reap what has no owner."""
        return self.purge_expired(), self.reap_orphans()

    def start_sweeper(self, interval: float | None = None) -> "asyncio.Task[None] | None":
        """Sweep on a timer for as long as the loop runs. Idempotent."""
        period = self.sweep_interval if interval is None else interval
        if period <= 0:
            return None
        if self._sweeper is not None and not self._sweeper.done():
            return self._sweeper
        self._sweeper = asyncio.create_task(self._sweep_forever(period))
        return self._sweeper

    async def stop_sweeper(self) -> None:
        task, self._sweeper = self._sweeper, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _sweep_forever(self, period: float) -> None:
        while True:
            await asyncio.sleep(period)
            try:
                await asyncio.to_thread(self.sweep)
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[sessions] sweep failed: {exc}")

    # -- overridable hooks --------------------------------------------------

    def _persist(self, session: Session) -> None:
        if self.store is not None:
            self.store.set(self.NAMESPACE, session.id, session.to_dict())
        if self.cache is not None:
            self.cache.set(session.id, session.to_dict())

    def _destroy(self, session: Session) -> None:
        if not self.keep_workspace:
            shutil.rmtree(session.workspace, ignore_errors=True)
        if self.store is not None:
            self.store.delete(self.NAMESPACE, session.id)
        if self.cache is not None:
            self.cache.delete(session.id)


# ---------------------------------------------------------------------------
# Memory
#
# A session remembers its conversation: every run appends the exchange to a
# transcript that is persisted through the `Store`, replayed to the model on the
# next run and replayed to a client that reconnects. Trimming keeps it inside a
# budget, optionally summarising what falls out instead of dropping it.
# ---------------------------------------------------------------------------

#: Roles a remembered turn may carry, mapped onto the block kind of the UI.
TURN_KINDS = {"user": "prompt", "assistant": "output", "system": "log"}


@dataclass
class Turn:
    """One remembered message of a conversation."""

    role: str
    text: str
    created_at: float = field(default_factory=time.time)

    @property
    def kind(self) -> str:
        return TURN_KINDS.get(self.role, "output")

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "text": self.text,
            "kind": self.kind,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Turn":
        return cls(
            role=str(data.get("role") or "user"),
            text=str(data.get("text") or ""),
            created_at=float(data.get("created_at") or time.time()),
        )

    def as_message(self) -> dict[str, str]:
        """The turn as a chat message the model understands."""
        return {"role": self.role, "content": self.text}


@dataclass
class Transcript:
    """The ordered turns of one session."""

    session_id: str
    turns: list[Turn] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.turns)

    @property
    def size(self) -> int:
        """Total number of characters remembered."""
        return sum(len(turn.text) for turn in self.turns)

    def append(self, *turns: Turn) -> "Transcript":
        self.turns.extend(t for t in turns if t.text.strip())
        return self

    def messages(self) -> list[dict[str, str]]:
        return [turn.as_message() for turn in self.turns]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session_id,
            "turns": [turn.to_dict() for turn in self.turns],
        }

    @classmethod
    def from_dict(cls, session_id: str, data: dict[str, Any] | None) -> "Transcript":
        turns = (data or {}).get("turns") or []
        return cls(
            session_id=session_id,
            turns=[Turn.from_dict(t) for t in turns if isinstance(t, dict)],
        )

    def overflow(self, max_turns: int, max_chars: int) -> int:
        """How many of the oldest turns must go to fit inside the budgets.

        The newest turn always survives: it is the exchange that just happened,
        and a budget smaller than it would otherwise erase the conversation.
        """
        count = 0
        turns, size = len(self.turns), self.size
        while count < len(self.turns) - 1 and (
            (max_turns > 0 and turns > max_turns) or (max_chars > 0 and size > max_chars)
        ):
            size -= len(self.turns[count].text)
            turns -= 1
            count += 1
        return count


class ConversationMemory:
    """Transcripts by session, persisted through a `Store`.

    The policy is deliberately small: the newest turns are kept, the oldest fall
    out of the window, and a `summarizer` (when one is given) folds what falls
    out into a single system turn so the gist survives the trim. A consumer that
    wants another policy subclasses this and overrides `trim`, or decides what is
    remembered at all by overriding `Agent.remember`.
    """

    NAMESPACE = "transcripts"

    def __init__(
        self,
        store: Store | None = None,
        *,
        enabled: bool = True,
        max_turns: int = 40,
        max_chars: int = 24000,
        summarizer: Callable[[list[Turn]], Awaitable[str]] | None = None,
    ) -> None:
        self.store = store
        self.enabled = enabled
        self.max_turns = max_turns
        self.max_chars = max_chars
        self.summarizer = summarizer
        self._transcripts: dict[str, Transcript] = {}

    def transcript(self, session_id: str) -> Transcript:
        """Return the transcript of a session, loading it from the store once."""
        transcript = self._transcripts.get(session_id)
        if transcript is None:
            record = self.store.get(self.NAMESPACE, session_id) if self.store else None
            transcript = Transcript.from_dict(session_id, record)
            self._transcripts[session_id] = transcript
        return transcript

    def history(self, session_id: str) -> list[dict[str, str]]:
        """The remembered turns as chat messages, oldest first."""
        if not self.enabled:
            return []
        return self.transcript(session_id).messages()

    def replay(self, session_id: str) -> list[dict[str, Any]]:
        """The remembered turns as JSON for a client that (re)connects."""
        if not self.enabled:
            return []
        return [turn.to_dict() for turn in self.transcript(session_id).turns]

    async def remember(self, session_id: str, turns: Iterable[Turn]) -> Transcript:
        """Append `turns`, trim to the budgets and persist the result."""
        transcript = self.transcript(session_id)
        if not self.enabled:
            return transcript
        transcript.append(*turns)
        await self.trim(transcript)
        self.persist(transcript)
        return transcript

    async def trim(self, transcript: Transcript) -> None:
        """Drop (or summarise) the oldest turns until the budgets are met."""
        count = transcript.overflow(self.max_turns, self.max_chars)
        if count <= 0:
            return
        dropped, transcript.turns = transcript.turns[:count], transcript.turns[count:]
        if self.summarizer is None:
            return
        with contextlib.suppress(Exception):
            summary = (await self.summarizer(dropped)).strip()
            if summary:
                transcript.turns.insert(0, Turn("system", summary))

    def persist(self, transcript: Transcript) -> None:
        if self.store is not None:
            self.store.set(self.NAMESPACE, transcript.session_id, transcript.to_dict())

    def forget(self, session_id: str) -> None:
        """Erase the transcript of a session, in memory and in the store."""
        self._transcripts.pop(session_id, None)
        if self.store is not None:
            self.store.delete(self.NAMESPACE, session_id)


# ---------------------------------------------------------------------------
# Repository
#
# A session may be backed by a git checkout of a default repository: it is
# cloned into the session workspace on a branch of its own, and the work done
# there is published by committing, pushing and opening a pull request.
# ---------------------------------------------------------------------------

#: ``git@host:owner/name(.git)`` and ``https://host/owner/name(.git)``.
SSH_URL_RE = re.compile(r"^(?:ssh://)?git@(?P<host>[^:/]+)[:/](?P<path>.+?)(?:\.git)?/?$")
HTTP_URL_RE = re.compile(r"^https?://(?:[^@/]+@)?(?P<host>[^/]+)/(?P<path>.+?)(?:\.git)?/?$")
SHORTHAND_RE = re.compile(r"^(?P<path>[\w.-]+/[\w.-]+?)(?:\.git)?$")

DEFAULT_GIT_HOST = "github.com"
GIT_TOKEN_USER = "x-access-token"


class RepoError(ValueError):
    """Raised when a git or forge operation fails."""


@dataclass(frozen=True)
class RepoSpec:
    """An addressable repository: where it lives and how to write to it."""

    url: str = ""
    branch: str = ""
    token: str = ""
    remote: str = "origin"
    branch_prefix: str = "agent"
    directory: str = ""
    author_name: str = "agent"
    author_email: str = f"agent@users.noreply.{DEFAULT_GIT_HOST}"
    depth: int = 1

    @classmethod
    def from_config(cls, config: "AgentConfig") -> "RepoSpec":
        return cls(
            url=(config.repo_url or "").strip(),
            branch=(config.repo_branch or "").strip(),
            token=config.repo_token or "",
            remote=config.repo_remote or "origin",
            branch_prefix=config.repo_branch_prefix or "agent",
            directory=(config.repo_dir or "").strip(),
            author_name=(config.repo_author_name or config.name or "agent").strip(),
            author_email=(
                config.repo_author_email
                or f"{safe_name(config.name or 'agent')}@users.noreply.{DEFAULT_GIT_HOST}"
            ).strip(),
            depth=max(0, int(config.repo_depth or 0)),
        )

    @property
    def configured(self) -> bool:
        return bool(self.url)

    @property
    def local(self) -> bool:
        """True for a filesystem repository (a path or a ``file://`` URL)."""
        raw = self.url.strip()
        return raw.startswith(("file://", "/", "./", "../", "~"))

    @property
    def parts(self) -> tuple[str, str]:
        """Return ``(host, owner/name)`` for the configured URL.

        A filesystem repository has no host and its slug is its directory name.
        """
        raw = self.url.strip()
        if not raw:
            raise RepoError("no repository is configured")
        if self.local:
            path = raw[len("file://"):] if raw.startswith("file://") else raw
            return ("", path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git"))
        for pattern, host in ((SSH_URL_RE, ""), (HTTP_URL_RE, ""), (SHORTHAND_RE, DEFAULT_GIT_HOST)):
            match = pattern.match(raw)
            if match:
                return (host or match.group("host"), match.group("path").strip("/"))
        raise RepoError(f"unrecognized repository URL: {raw}")

    @property
    def slug(self) -> str:
        """``owner/name``."""
        return self.parts[1]

    @property
    def name(self) -> str:
        return self.slug.rsplit("/", 1)[-1]

    @property
    def clone_url(self) -> str:
        """The URL git clones from, without any credential in it."""
        raw = self.url.strip()
        if self.local or raw.startswith(("http://", "https://", "git@", "ssh://")):
            return raw
        host, slug = self.parts
        return f"https://{host}/{slug}.git"

    @property
    def api_url(self) -> str:
        """Base URL of the GitHub REST API for the host of this repository."""
        host = self.parts[0]
        if not host:
            raise RepoError("a filesystem repository has no pull request API")
        if host in (DEFAULT_GIT_HOST, "www.github.com"):
            return "https://api.github.com"
        return f"https://{host}/api/v3"

    def authenticated_url(self) -> str:
        """The clone URL with the token embedded, for one-off git invocations.

        The token is never written to ``.git/config``: it is only ever passed on
        an argument list, and command output is masked before it is surfaced.
        """
        url = self.clone_url
        if not self.token or not url.startswith("https://"):
            return url
        return f"https://{GIT_TOKEN_USER}:{self.token}@{url[len('https://'):]}"

    #: Tokens shorter than this are not masked: a one or two character string
    #: would shred unrelated output without protecting anything real.
    MIN_MASKED = 8

    def mask(self, text: str) -> str:
        if not self.token or len(self.token) < self.MIN_MASKED:
            return text
        return text.replace(self.token, "***")

    def identity(self) -> list[str]:
        """``git -c`` arguments giving the commits an author of their own."""
        args = []
        if self.author_name:
            args += ["-c", f"user.name={self.author_name}"]
        if self.author_email:
            args += ["-c", f"user.email={self.author_email}"]
        return args


@dataclass
class Checkout:
    """A cloned repository inside a session workspace."""

    manager: "RepoManager"
    path: Path
    branch: str
    base: str
    url: str = ""

    @classmethod
    def from_meta(cls, manager: "RepoManager", meta: dict[str, Any]) -> "Checkout":
        return cls(
            manager=manager,
            path=Path(meta["path"]),
            branch=meta.get("branch", ""),
            base=meta.get("base", ""),
            url=meta.get("url", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "branch": self.branch,
            "base": self.base,
            "url": self.url,
        }

    async def status(self) -> str:
        return await self.manager.status(self.path)

    async def commit(self, message: str) -> str:
        return await self.manager.commit(self.path, message)

    async def push(self) -> str:
        return await self.manager.push(self.path, self.branch)

    async def pull_request(self, title: str, body: str = "") -> dict[str, Any]:
        return await self.manager.open_pull_request(
            title=title, body=body, head=self.branch, base=self.base
        )

    async def publish(self, message: str, title: str = "", body: str = "") -> dict[str, Any]:
        """Commit, push and open a pull request in one step."""
        commit = await self.commit(message)
        push = await self.push()
        pull = await self.pull_request(title or message, body)
        return {"commit": commit, "push": push, "pull_request": pull}


class RepoManager:
    """Clones the configured repository and publishes the work done in it.

    `spec` is replaceable at runtime (the ``/repo`` command does exactly that),
    so a manager is never bound to one repository for its lifetime.
    """

    def __init__(self, spec: RepoSpec | None = None, *, timeout: int = 300) -> None:
        self.spec = spec or RepoSpec()
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return self.spec.configured

    def branch_name(self, session_id: str) -> str:
        prefix = safe_name(self.spec.branch_prefix or "agent").strip("-") or "agent"
        return f"{prefix}/{safe_name(session_id)}"

    # -- git ----------------------------------------------------------------

    async def git(self, *args: str, cwd: Path | None = None) -> str:
        """Run a git command, returning its output with secrets masked."""
        env = os.environ.copy()
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_ASKPASS": "",
                "GCM_INTERACTIVE": "never",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=str(cwd) if cwd else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except OSError as exc:
            raise RepoError(f"git is not available: {exc}") from exc
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise RepoError(f"git timed out after {self.timeout}s") from None
        output = self.spec.mask(stdout.decode("utf-8", errors="replace")).strip()
        if process.returncode:
            raise RepoError(output or f"git failed with exit code {process.returncode}")
        return output

    async def clone(self, workspace: Path | str, *, session_id: str) -> Checkout:
        """Clone the repository into `workspace` and branch off for the session."""
        if not self.configured:
            raise RepoError("no repository is configured")
        root = Path(workspace).expanduser().resolve()
        target = root / (self.spec.directory or self.spec.name)
        if target.exists() and any(target.iterdir()):
            raise RepoError(f"clone target is not empty: {target.name}")
        args = ["clone"]
        if self.spec.depth:
            args += ["--depth", str(self.spec.depth)]
        if self.spec.branch:
            args += ["--branch", self.spec.branch]
        args += ["--origin", self.spec.remote, self.spec.authenticated_url(), str(target)]
        await self.git(*args, cwd=root)
        # Keep the credential out of the stored remote; it is supplied per push.
        await self.git(
            "remote", "set-url", self.spec.remote, self.spec.clone_url, cwd=target
        )
        base = self.spec.branch or await self.git(
            "rev-parse", "--abbrev-ref", "HEAD", cwd=target
        )
        branch = self.branch_name(session_id)
        await self.git("checkout", "-b", branch, cwd=target)
        return Checkout(
            manager=self, path=target, branch=branch, base=base, url=self.spec.clone_url
        )

    async def status(self, path: Path) -> str:
        return await self.git("status", "--short", "--branch", cwd=path) or "(clean)"

    async def commit(self, path: Path, message: str) -> str:
        message = (message or "").strip()
        if not message:
            raise RepoError("a commit message is required")
        await self.git("add", "--all", cwd=path)
        staged = await self.git("diff", "--cached", "--name-only", cwd=path)
        if not staged:
            raise RepoError("there is nothing to commit")
        await self.git(*self.spec.identity(), "commit", "--message", message, cwd=path)
        return await self.git("log", "--oneline", "--max-count", "1", cwd=path)

    async def push(self, path: Path, branch: str) -> str:
        """Push `branch`, passing the credential on the command line only."""
        return (
            await self.git(
                "push",
                "--set-upstream",
                self.spec.authenticated_url(),
                f"HEAD:refs/heads/{branch}",
                cwd=path,
            )
            or f"Pushed {branch}"
        )

    async def open_pull_request(
        self, *, title: str, body: str = "", head: str, base: str = ""
    ) -> dict[str, Any]:
        """Open a pull request for `head` against `base` on the forge."""
        title = (title or "").strip()
        if not title:
            raise RepoError("a pull request title is required")
        if not self.spec.token:
            raise RepoError("a repository token is required to open a pull request")
        base = (base or self.spec.branch or "").strip()
        if not base:
            raise RepoError("a base branch is required to open a pull request")
        payload = {"title": title, "body": body or "", "head": head, "base": base}
        url = f"{self.spec.api_url}/repos/{self.spec.slug}/pulls"
        data = await asyncio.to_thread(self._post, url, payload)
        return {
            "number": data.get("number"),
            "url": data.get("html_url", ""),
            "title": data.get("title", title),
            "state": data.get("state", ""),
        }

    def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST JSON to the forge API. Overridable; keeps IO in one place."""
        import urllib.error
        import urllib.request

        if not url.startswith("https://"):
            raise RepoError(f"refusing to call a non HTTPS API: {url}")
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + self.spec.token,
                "Content-Type": "application/json",
                "User-Agent": "agent-harness",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:  # forge said no: surface why
            detail = exc.read().decode("utf-8", errors="replace")
            raise RepoError(self.spec.mask(f"{exc.code} {exc.reason}: {detail}")) from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RepoError(self.spec.mask(str(exc))) from None


# ---------------------------------------------------------------------------
# Resilience
#
# Every call to a provider is bounded: one timeout per attempt, a few retries
# with exponential backoff and jitter on the failures another attempt can get
# past, and a taxonomy (`ErrorKind`) that tells a consumer which kind of failure
# it got instead of a string it has to parse. Token accounting rides the same
# path, because what a run spent is only knowable where its calls are made.
# ---------------------------------------------------------------------------


class ErrorKind:
    """Why a run failed, as reported on `RunResult.error_kind`."""

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    CONNECTION = "connection"
    SERVER = "server"
    AUTH = "auth"
    INVALID = "invalid_request"
    MAX_TURNS = "max_turns"
    CANCELLED = "cancelled"
    INTERNAL = "internal"

    #: The kinds an identical attempt may get past. Everything else is a
    #: decision of the provider (or a defect here) and is reported at once.
    TRANSIENT = (TIMEOUT, RATE_LIMIT, CONNECTION, SERVER)


#: Exception class names mapped onto the taxonomy. Matching by name keeps the
#: harness from importing the exception hierarchy of any provider SDK; a class
#: that is not listed still classifies by the HTTP status it carries.
ERROR_KINDS = {
    "APITimeoutError": ErrorKind.TIMEOUT,
    "TimeoutError": ErrorKind.TIMEOUT,
    "ReadTimeout": ErrorKind.TIMEOUT,
    "ConnectTimeout": ErrorKind.TIMEOUT,
    "APIConnectionError": ErrorKind.CONNECTION,
    "ConnectionError": ErrorKind.CONNECTION,
    "ConnectError": ErrorKind.CONNECTION,
    "RateLimitError": ErrorKind.RATE_LIMIT,
    "InternalServerError": ErrorKind.SERVER,
    "AuthenticationError": ErrorKind.AUTH,
    "PermissionDeniedError": ErrorKind.AUTH,
    "BadRequestError": ErrorKind.INVALID,
    "NotFoundError": ErrorKind.INVALID,
    "UnprocessableEntityError": ErrorKind.INVALID,
    "MaxTurnsExceeded": ErrorKind.MAX_TURNS,
}


class ModelError(RuntimeError):
    """A model call that failed, carrying its kind, role and attempt count."""

    def __init__(
        self,
        message: str,
        kind: str = ErrorKind.INTERNAL,
        *,
        role: str = LEADER_ROLE,
        attempts: int = 1,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.role = role
        self.attempts = attempts
        #: What the provider said, without the framing of the message above.
        self.detail = detail or message


def status_kind(status: int) -> str | None:
    """The taxonomy entry of an HTTP status, or ``None`` when it says nothing."""
    if status in (408, 504):
        return ErrorKind.TIMEOUT
    if status == 429:
        return ErrorKind.RATE_LIMIT
    if status in (401, 403):
        return ErrorKind.AUTH
    if 500 <= status < 600:
        return ErrorKind.SERVER
    if 400 <= status < 500:
        return ErrorKind.INVALID
    return None


def error_status(exc: BaseException) -> int | None:
    """The HTTP status of a provider exception, wherever its SDK keeps it."""
    for holder in (exc, getattr(exc, "response", None)):
        for name in ("status_code", "status", "http_status"):
            value = getattr(holder, name, None)
            if isinstance(value, int):
                return value
    return None


def classify_error(exc: BaseException) -> str:
    """Classify a failure as one of the `ErrorKind` values."""
    if isinstance(exc, ModelError):
        return exc.kind
    if isinstance(exc, asyncio.CancelledError):
        return ErrorKind.CANCELLED
    for cls in type(exc).__mro__:
        kind = ERROR_KINDS.get(cls.__name__)
        if kind is not None:
            return kind
    status = error_status(exc)
    if status is not None:
        kind = status_kind(status)
        if kind is not None:
            return kind
    if isinstance(exc, OSError):
        return ErrorKind.CONNECTION
    return ErrorKind.INTERNAL


def model_error(
    exc: BaseException, kind: str, *, role: str = LEADER_ROLE, attempts: int = 1
) -> ModelError:
    """Wrap a failed model call, keeping the message the provider gave."""
    detail = (
        exc.detail
        if isinstance(exc, ModelError)
        else (str(exc).strip() or type(exc).__name__)
    )
    tries = f" after {attempts} attempts" if attempts > 1 else ""
    return ModelError(
        f"{role} model {kind}{tries}: {detail}",
        kind,
        role=role,
        attempts=attempts,
        detail=detail,
    )


def usage_value(raw: Any, *names: str) -> int:
    """The first of `names` present on a usage payload, as attribute or key."""
    for name in names:
        value = raw.get(name) if isinstance(raw, dict) else getattr(raw, name, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return 0


@dataclass
class Usage:
    """What was spent: per run on `RunResult`, in total on a `ModelPool`."""

    requests: int = 0
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(
        self, *, requests: int = 1, input_tokens: int = 0, output_tokens: int = 0
    ) -> "Usage":
        self.requests += int(requests)
        self.input_tokens += int(input_tokens)
        self.output_tokens += int(output_tokens)
        return self

    def record(self, raw: Any) -> "Usage":
        """Accumulate one provider usage payload, under either naming."""
        if raw is None:
            return self
        return self.add(
            requests=usage_value(raw, "requests") or 1,
            input_tokens=usage_value(raw, "input_tokens", "prompt_tokens"),
            output_tokens=usage_value(raw, "output_tokens", "completion_tokens"),
        )

    def price(self, input_rate: float = 0.0, output_rate: float = 0.0) -> float:
        """Cost the tokens at rates quoted per million, and keep the result."""
        spent = self.input_tokens * input_rate + self.output_tokens * output_rate
        self.cost = spent / 1_000_000
        return self.cost

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost": round(self.cost, 6),
        }


#: The accounting of the run in progress on this task. A model call made
#: anywhere below `Engine.running` — a tool delegating to the vision model, the
#: summariser of the memory — finds its sink here instead of being handed one,
#: and two concurrent runs never share it because each run is its own task.
_USAGE: "contextvars.ContextVar[Usage | None]" = contextvars.ContextVar(
    "agent_usage", default=None
)


def current_usage() -> Usage | None:
    """The `Usage` of the run in progress on this task, when there is one."""
    return _USAGE.get()


def record_usage(raw: Any, *sinks: "Usage | None") -> None:
    """Accumulate one provider usage payload onto each distinct sink given."""
    accounted: list[Usage] = []
    for sink in (*sinks, current_usage()):
        if isinstance(sink, Usage) and not any(sink is seen for seen in accounted):
            accounted.append(sink)
            sink.record(raw)


def usage_of(streamed: Any) -> Any:
    """The usage of an SDK run, wherever that SDK happens to keep it.

    The running totals of the SDK are preferred; a build that does not keep them
    is summed from the raw responses instead, and one that keeps neither simply
    reports nothing rather than failing the run it was accounting.
    """
    running = getattr(getattr(streamed, "context_wrapper", None), "usage", None)
    if running is not None:
        return running
    totals = {"requests": 0, "input_tokens": 0, "output_tokens": 0}
    for response in getattr(streamed, "raw_responses", None) or []:
        raw = getattr(response, "usage", None)
        if raw is None:
            continue
        totals["requests"] += usage_value(raw, "requests") or 1
        totals["input_tokens"] += usage_value(raw, "input_tokens", "prompt_tokens")
        totals["output_tokens"] += usage_value(raw, "output_tokens", "completion_tokens")
    return totals if totals["requests"] else None


@dataclass(frozen=True)
class RetryPolicy:
    """How a model call is bounded in time and how often it is tried again.

    `attempts` counts attempts in total (``1`` never retries) and `timeout`
    bounds a single attempt (``0`` leaves it unbounded). The wait after failure
    *n* is ``backoff * 2 ** (n - 1)`` seconds, capped at `max_backoff` and
    multiplied by a random factor in ``[1 - jitter, 1]``, so that the clients a
    provider failed together do not all come back at the same moment.
    """

    attempts: int = 3
    timeout: float = 120.0
    backoff: float = 0.5
    max_backoff: float = 30.0
    jitter: float = 0.5
    transient: tuple[str, ...] = ErrorKind.TRANSIENT

    @classmethod
    def from_config(cls, config: "AgentConfig") -> "RetryPolicy":
        """The policy a configuration describes (`request_timeout`, `retry_*`)."""
        return cls(
            attempts=max(1, int(config.retry_attempts)),
            timeout=max(0.0, float(config.request_timeout)),
            backoff=max(0.0, float(config.retry_backoff)),
            max_backoff=max(0.0, float(config.retry_max_backoff)),
            jitter=min(max(float(config.retry_jitter), 0.0), 1.0),
        )

    def retryable(self, kind: str, attempt: int) -> bool:
        """True when `kind` is worth another attempt after attempt `attempt`."""
        return attempt < self.attempts and kind in self.transient

    def delay(self, attempt: int) -> float:
        """Seconds to wait after failure `attempt` before trying again."""
        capped = min(self.backoff * 2 ** max(attempt - 1, 0), self.max_backoff)
        return capped * (1.0 - self.jitter * random.random())

    async def call(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        role: str = LEADER_ROLE,
        bounded: bool = True,
        resumable: Callable[[], bool] | None = None,
        on_retry: Callable[[dict[str, Any]], Any] | None = None,
    ) -> Any:
        """Await ``operation()`` under this policy and return what it returns.

        A failure is classified, tried again while it is transient and the
        caller still considers the call `resumable`, and otherwise raised: as a
        `ModelError` carrying the kind, or unchanged when nothing is known about
        it, so a defect in the harness is never disguised as a provider fault.
        Cancellation is never retried. Set `bounded` to ``False`` for a call
        that enforces its own timeout, such as a stream read event by event.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                if bounded and self.timeout:
                    async with asyncio.timeout(self.timeout):
                        return await operation()
                return await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                kind = classify_error(exc)
                again = self.retryable(kind, attempt) and (
                    resumable is None or resumable()
                )
                if not again:
                    if kind == ErrorKind.INTERNAL:
                        raise
                    raise model_error(exc, kind, role=role, attempts=attempt) from exc
                pause = self.delay(attempt)
                usage = current_usage()
                if usage is not None:
                    usage.retries += 1
                if on_retry is not None:
                    outcome = on_retry(
                        {
                            "role": role,
                            "error_kind": kind,
                            "attempt": attempt,
                            "attempts": self.attempts,
                            "delay": pause,
                            "error": str(exc),
                        }
                    )
                    if inspect.isawaitable(outcome):
                        await outcome
                await asyncio.sleep(pause)


# ---------------------------------------------------------------------------
# Models
#
# A run may need more than one model: a leader that drives the agentic loop and
# specialists (vision, ...) that tools delegate to. `ModelPool` keeps one client
# per endpoint and one SDK model per model name so extra roles are cheap, and
# every request it makes itself goes through a `RetryPolicy` and is accounted.
# ---------------------------------------------------------------------------


class ModelPool:
    """Lazily builds and caches API clients and SDK models by `ModelSpec`."""

    def __init__(self, policy: RetryPolicy | None = None) -> None:
        self._clients: dict[tuple[str, str], Any] = {}
        self._models: dict[tuple[str, str, str], Any] = {}
        #: Applied to the calls the pool makes when one is not passed in.
        self.policy = policy or RetryPolicy()
        #: Everything this pool has spent, across runs and roles.
        self.usage = Usage()

    def client(self, spec: ModelSpec) -> Any:
        """Return the shared async client for the endpoint of `spec`.

        The client is told not to retry: a retry inside the SDK is invisible to
        the event bus, unjittered, uncounted and nested inside whatever the
        harness is already doing, so retrying belongs to the `RetryPolicy` and
        to it alone. The transport timeout of the pool's policy is kept, since
        it is what stops a socket that has gone quiet mid-response.
        """
        client = self._clients.get(spec.endpoint)
        if client is None:
            from openai import AsyncOpenAI

            options: dict[str, Any] = {"max_retries": 0}
            if self.policy.timeout:
                options["timeout"] = self.policy.timeout
            # An empty key means "no key here": pass nothing so the SDK falls
            # back to its own environment lookups (OPENAI_API_KEY, ...). When
            # that lookup would find nothing either, hand the SDK a placeholder
            # instead — it refuses to construct a client without some key, and
            # endpoints that need no auth (a local Ollama, a proxy) would fail
            # to start over a credential they never ask for.
            if spec.api_key:
                options["api_key"] = spec.api_key
            elif not os.environ.get("OPENAI_API_KEY"):
                options["api_key"] = "no-key"
            client = AsyncOpenAI(base_url=spec.api_url, **options)
            self._clients[spec.endpoint] = client
        return client

    def model(self, spec: ModelSpec) -> Any:
        """Return the shared SDK model for `spec`."""
        key = (spec.name, spec.api_url, spec.api_key)
        model = self._models.get(key)
        if model is None:
            from agents import OpenAIChatCompletionsModel

            model = OpenAIChatCompletionsModel(
                model=spec.name, openai_client=self.client(spec)
            )
            self._models[key] = model
        return model

    async def complete(
        self,
        spec: ModelSpec,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        policy: RetryPolicy | None = None,
        usage: "Usage | None" = None,
        on_retry: Callable[[dict[str, Any]], Any] | None = None,
    ) -> str:
        """Single chat completion against `spec`; no tools, no agentic loop.

        The request is bounded and retried by `policy` (the pool's by default)
        and what it spends is recorded on `usage`, on the run in progress and on
        the totals of the pool. A failure that survives the policy is raised as
        a `ModelError`, so the caller sees why it failed and not only that it
        did.
        """
        resolved = policy or self.policy

        async def request() -> Any:
            return await self.client(spec).chat.completions.create(
                model=spec.name, messages=messages, max_tokens=max_tokens
            )

        response = await resolved.call(request, role=spec.role, on_retry=on_retry)
        self.record(getattr(response, "usage", None), usage)
        choices = getattr(response, "choices", None) or []
        if not choices:
            return ""
        return (choices[0].message.content or "").strip()

    def record(self, raw: Any, usage: "Usage | None" = None) -> None:
        """Account one provider response on the pool, the run and `usage`."""
        record_usage(raw, self.usage, usage)

    async def describe_image(
        self,
        spec: ModelSpec,
        data_url: str,
        question: str = "",
        *,
        instructions: str = DEFAULT_VISION_INSTRUCTIONS,
        max_tokens: int | None = None,
        policy: RetryPolicy | None = None,
        usage: "Usage | None" = None,
        on_retry: Callable[[dict[str, Any]], Any] | None = None,
    ) -> str:
        """Ask a vision model about one image and return its answer as text."""
        messages = [
            {"role": "system", "content": instructions},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question.strip() or "Describe this image."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        return await self.complete(
            spec,
            messages,
            max_tokens=max_tokens,
            policy=policy,
            usage=usage,
            on_retry=on_retry,
        )

    def clear(self) -> None:
        """Drop every cached client and model. The totals are kept."""
        self._clients.clear()
        self._models.clear()


# ---------------------------------------------------------------------------
# Tools
#
# `Workspace` holds the sandboxed filesystem/shell primitives (unit testable on
# their own) and `ToolRegistry` exposes them to the model as function tools.
# ---------------------------------------------------------------------------

IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

MAX_READ_BYTES = 2 * 1024 * 1024


def guess_mime(path: Path | str) -> str:
    """Guess a MIME type from a file extension."""
    suffix = Path(path).suffix.lower()
    if suffix in IMAGE_MIME:
        return IMAGE_MIME[suffix]
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


class WorkspaceError(ValueError):
    """Raised when an operation would escape the workspace root."""


class Workspace:
    """Filesystem and shell primitives scoped to a single directory."""

    def __init__(self, root: Path | str, *, shell_timeout: int = 120) -> None:
        self.root = Path(root).expanduser().resolve()
        self.shell_timeout = shell_timeout

    def resolve(self, raw: str) -> Path:
        """Resolve a model supplied path inside the workspace root."""
        cleaned = (raw or "").strip().strip("\"'")
        if not cleaned:
            raise WorkspaceError("path must not be empty")
        path = Path(cleaned).expanduser()
        if not path.is_absolute():
            path = self.root / path
        try:
            # resolve() also follows symlinks, so links cannot escape the root
            resolved = path.resolve()
        except OSError as exc:  # pragma: no cover - platform specific
            raise WorkspaceError(str(exc)) from exc
        if resolved != self.root and not resolved.is_relative_to(self.root):
            raise WorkspaceError(f"path escapes the workspace root: {raw}")
        return resolved

    def relative(self, path: Path) -> str:
        with contextlib.suppress(ValueError):
            return str(path.relative_to(self.root))
        return str(path)

    # -- sync primitives ----------------------------------------------------

    def read_text(self, raw: str) -> str:
        path = self.resolve(raw)
        if not path.is_file():
            raise WorkspaceError(f"file not found: {self.relative(path)}")
        if path.stat().st_size > MAX_READ_BYTES:
            raise WorkspaceError(f"file too large: {self.relative(path)}")
        return path.read_text(encoding="utf-8", errors="replace")

    def write_text(self, raw: str, content: str) -> str:
        path = self.resolve(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return self.relative(path)

    def list_dir(self, raw: str = ".") -> list[str]:
        path = self.resolve(raw)
        if not path.is_dir():
            raise WorkspaceError(f"directory not found: {self.relative(path)}")
        return sorted(e.name + ("/" if e.is_dir() else "") for e in path.iterdir())

    def read_data_url(self, raw: str) -> str:
        path = self.resolve(raw)
        if not path.is_file():
            raise WorkspaceError(f"file not found: {self.relative(path)}")
        data = path.read_bytes()
        if not data:
            raise WorkspaceError(f"file is empty: {self.relative(path)}")
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{guess_mime(path)};base64,{encoded}"

    def apply_patch(self, raw: str, diff: str, operation: str = "update_file") -> str:
        """Apply a unified diff to a workspace file."""
        from agents.apply_diff import apply_diff

        path = self.resolve(raw)
        if operation == "delete_file":
            path.unlink(missing_ok=True)
            return f"Deleted {self.relative(path)}"
        if operation == "create_file":
            text = apply_diff("", diff, mode="create")
        elif operation == "update_file":
            if not path.is_file():
                raise WorkspaceError(f"file not found: {self.relative(path)}")
            text = apply_diff(path.read_text(encoding="utf-8"), diff, mode="default")
        else:
            raise WorkspaceError(f"unknown operation: {operation}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return f"Wrote {self.relative(path)}"

    # -- async primitives ---------------------------------------------------

    async def run_shell(self, command: str) -> str:
        """Run a shell command in the workspace without blocking the loop."""
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.root,
                env=env,
            )
        except OSError as exc:
            return f"(command failed: {exc})"
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.shell_timeout
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            return f"(command timed out after {self.shell_timeout}s)"
        output = stdout.decode("utf-8", errors="replace")
        error = stderr.decode("utf-8", errors="replace")
        if error:
            output += ("\n" if output else "") + error
        if process.returncode:
            output += f"\n[exit code {process.returncode}]"
        return output.strip() or "(no output)"


class ToolRegistry:
    """Builds the model facing tool list for a workspace.

    Consumers add or replace tools with `register`; each factory receives the
    `Workspace` and returns an ``openai-agents`` tool. Tools may delegate to a
    specialist model (see the `vision` argument of `build`).
    """

    def __init__(self, *, defaults: bool = True, shell: bool = True) -> None:
        self._factories: dict[str, Callable[[Workspace], Any]] = {}
        self._defaults = defaults
        self._shell = shell

    def register(self, name: str, factory: Callable[[Workspace], Any]) -> None:
        self._factories[name] = factory

    def unregister(self, name: str) -> None:
        self._factories.pop(name, None)

    def build(
        self,
        workspace: Workspace,
        *,
        vision: Callable[[str, str], Awaitable[str]] | None = None,
        checkout: "Checkout | None" = None,
    ) -> list[Any]:
        """Build the tools for `workspace`.

        `vision` is an optional ``(data_url, question) -> text`` delegate. When
        it is given, images are inspected by the vision model and only its text
        answer reaches the leader, so the leader needs no vision of its own.
        `checkout` is the session's git checkout; when it is given the leader
        also gets the tools that publish its work.
        """
        tools = list(self._build_defaults(workspace, vision)) if self._defaults else []
        if checkout is not None:
            tools.extend(self._build_repo_tools(checkout))
        tools.extend(factory(workspace) for factory in self._factories.values())
        return tools

    @staticmethod
    def guard(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        """Turn workspace and repository errors into model readable text."""

        @functools.wraps(fn)  # keeps the signature the tool schema needs
        async def wrapper(*args: Any, **kwargs: Any) -> str:
            try:
                return await fn(*args, **kwargs)
            except (WorkspaceError, RepoError, OSError, ValueError) as exc:
                return f"Error: {exc}"

        return wrapper

    def _build_repo_tools(self, checkout: "Checkout") -> Iterable[Any]:
        """Tools that let the leader publish the work in its git checkout."""
        from agents import function_tool

        guard = self.guard

        @function_tool(
            name_override="git_status",
            description_override=(
                "Show the state of the git checkout of this session: its branch "
                "and the files that changed."
            ),
        )
        @guard
        async def git_status() -> str:
            return (
                f"Repository {checkout.url}\nBranch {checkout.branch} "
                f"(base {checkout.base})\n{await checkout.status()}"
            )

        @function_tool(
            name_override="git_commit",
            description_override=(
                "Stage every change in the git checkout and commit it with the "
                "given message."
            ),
        )
        @guard
        async def git_commit(message: str) -> str:
            return await checkout.commit(message)

        @function_tool(
            name_override="git_push",
            description_override=(
                "Push the session branch of the git checkout to the remote."
            ),
        )
        @guard
        async def git_push() -> str:
            return await checkout.push()

        @function_tool(
            name_override="open_pull_request",
            description_override=(
                "Open a pull request from the session branch onto the base "
                "branch and return its URL. Push the branch first."
            ),
        )
        @guard
        async def open_pull_request(title: str, body: str = "") -> str:
            pull = await checkout.pull_request(title, body)
            return f"Opened pull request #{pull['number']}: {pull['url']}"

        @function_tool(
            name_override="publish_work",
            description_override=(
                "Finish the task: commit every change, push the session branch "
                "and open a pull request. Returns the pull request URL."
            ),
        )
        @guard
        async def publish_work(message: str, title: str = "", body: str = "") -> str:
            result = await checkout.publish(message, title, body)
            pull = result["pull_request"]
            return f"Published {result['commit']} as pull request #{pull['number']}: {pull['url']}"

        return [git_status, git_commit, git_push, open_pull_request, publish_work]

    # NOTE: the hosted tools of openai-agents (ApplyPatchTool, ShellTool, ...)
    # require the OpenAI Responses API. Chat Completions backends such as
    # OpenRouter only support plain function tools, so we implement our own.
    def _build_defaults(
        self,
        ws: Workspace,
        vision: Callable[[str, str], Awaitable[str]] | None = None,
    ) -> Iterable[Any]:
        from agents import function_tool

        guard = self.guard

        @function_tool(
            name_override="list_directory",
            description_override=(
                "List files and subdirectories of a workspace directory. Use "
                "relative paths such as '.' or 'notes'. One entry per line."
            ),
        )
        @guard
        async def list_directory(path: str = ".") -> str:
            entries = await asyncio.to_thread(ws.list_dir, path)
            return "\n".join(entries) if entries else "(empty)"

        @function_tool(
            name_override="read_text_file",
            description_override=(
                "Read a UTF-8 text file from the workspace and return its contents."
            ),
        )
        @guard
        async def read_text_file(path: str) -> str:
            return await asyncio.to_thread(ws.read_text, path)

        @function_tool(
            name_override="write_text_file",
            description_override=(
                "Write text to a workspace file, creating parent directories as "
                "needed. Overwrites the file when it already exists."
            ),
        )
        @guard
        async def write_text_file(path: str, content: str) -> str:
            written = await asyncio.to_thread(ws.write_text, path, content)
            return f"Wrote {len(content)} characters to {written}"

        @function_tool(
            name_override="view_image",
            description_override=(
                "Read an image from the workspace and return it as a base64 data "
                "URL so it can be inspected."
            ),
        )
        @guard
        async def view_image(path: str) -> str:
            return await asyncio.to_thread(ws.read_data_url, path)

        @function_tool(
            name_override="describe_image",
            description_override=(
                "Look at a workspace image with a vision model and return what it "
                "sees as text. Pass 'question' to ask about something specific, "
                "for example 'what text appears in this screenshot?'."
            ),
        )
        @guard
        async def describe_image(path: str, question: str = "") -> str:
            assert vision is not None
            data_url = await asyncio.to_thread(ws.read_data_url, path)
            try:
                answer = await vision(data_url, question)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the specialist must never break the leader
                return f"Error: the vision model failed: {exc}"
            return answer or "(the vision model returned nothing)"

        image_tool = describe_image if vision is not None else view_image

        @function_tool(
            name_override="apply_patch",
            description_override=(
                "Apply a unified diff to a workspace file. operation is one of "
                "'create_file', 'update_file' or 'delete_file'."
            ),
        )
        @guard
        async def apply_patch(
            path: str, diff: str, operation: str = "update_file"
        ) -> str:
            return await asyncio.to_thread(ws.apply_patch, path, diff, operation)

        tools = [list_directory, read_text_file, write_text_file, image_tool, apply_patch]

        if self._shell:

            @function_tool(
                name_override="run_shell_command",
                description_override=(
                    "Run a shell command with the workspace as the working "
                    "directory and return stdout, stderr and the exit code."
                ),
            )
            @guard
            async def run_shell_command(command: str) -> str:
                return await ws.run_shell(command)

            tools.append(run_shell_command)

        return tools


# ---------------------------------------------------------------------------
# Console
#
# The default renderer is only an event subscriber: consumers can subclass it,
# swap it out, or drop it entirely and subscribe their own.
# ---------------------------------------------------------------------------


class ConsoleRenderer:
    """Streams agent block events to a terminal."""

    STYLES = {
        "reasoning": "\x1b[2;3m",
        "tool": "\x1b[2m",
        "error": "\x1b[31m",
        "title": "\x1b[1;36m",
    }
    RESET = "\x1b[0m"

    def __init__(
        self,
        *,
        color: bool | None = None,
        stream: Any = None,
        banners: bool = True,
    ) -> None:
        self.stream = stream or sys.stdout
        if color is None:
            color = bool(getattr(self.stream, "isatty", lambda: False)()) and (
                os.environ.get("NO_COLOR") is None
            )
        self.color = color
        #: Print the config banner when a run starts. A repl prints its own once
        #: and turns this off, so a banner is not repeated on every prompt.
        self.banners = banners
        self._prev_kind: str | None = None
        self._trailing_newlines = 0

    # -- overridable output -------------------------------------------------

    def write(self, text: str, end: str = "") -> None:
        print(text, end=end, file=self.stream, flush=True)

    def style(self, kind: str, text: str) -> str:
        code = self.STYLES.get(kind, "") if self.color else ""
        return f"{code}{text}{self.RESET}" if code else text

    # -- subscription -------------------------------------------------------

    def attach(self, bus: EventBus) -> Callable[[], None]:
        """Subscribe to a bus; returns an unsubscriber."""
        return bus.on(EventType.ALL, self.handle)

    def handle(self, event: Event) -> None:
        kind = event.data.get("kind", "output")
        if event.type == EventType.AGENT_START:
            config = event.data.get("config")
            if self.banners and isinstance(config, AgentConfig):
                self.banner(config.name, config.banner_items())
        elif event.type == EventType.BLOCK_DELTA:
            self.emit(kind, event.data.get("text", ""))
        elif event.type == EventType.TOOL_START:
            self.emit("tool", f"-> {event.data.get('text', 'tool')}\n")
        elif event.type == EventType.TOOL_END:
            self.emit("tool", self.tool_outcome(event.data))
        elif event.type == EventType.MODEL_RETRY:
            self.emit("error", self.retry_notice(event.data))
        elif event.type == EventType.AGENT_ERROR:
            self.emit("error", str(event.data.get("error", "")))
        elif event.type == EventType.AGENT_END:
            if self._trailing_newlines == 0:
                self.write("\n")
            self._prev_kind = None
            self._trailing_newlines = 0

    @staticmethod
    def retry_notice(data: dict[str, Any]) -> str:
        """The line printed when a model call is about to be tried again."""
        role = data.get("role", LEADER_ROLE)
        kind = data.get("error_kind", "error")
        attempt = int(data.get("attempt", 1)) + 1
        attempts = int(data.get("attempts", attempt))
        delay = float(data.get("delay") or 0.0)
        return f"!! {role} model {kind}: retry {attempt}/{attempts} in {delay:.1f}s\n"

    @staticmethod
    def tool_outcome(data: dict[str, Any]) -> str:
        """The closing line of a tool call: its status, duration and result."""
        status = "ok" if data.get("ok", True) else "failed"
        duration = float(data.get("duration") or 0.0) * 1000
        line = f"<- {data.get('name', 'tool')}: {status} in {duration:.0f} ms"
        body = str(data.get("result") or "")
        return f"{line}\n{body}\n" if body else f"{line}\n"

    def emit(self, kind: str, text: str) -> None:
        """Print streamed text, keeping one blank line between blocks."""
        if not text:
            return
        if self._prev_kind is not None and kind != self._prev_kind:
            needed = 2 - min(self._trailing_newlines, 2)
            if needed > 0:
                self.write("\n" * needed)
            text = text.lstrip("\n")
            self._trailing_newlines = 0
            if not text:
                self._prev_kind = kind
                return
        self.write(self.style(kind, text))
        self._trailing_newlines = min(len(text) - len(text.rstrip("\n")), 2)
        self._prev_kind = kind

    # -- banner -------------------------------------------------------------

    def banner(self, title: str, items: list[tuple[str, str]], width: int = 60) -> None:
        """Print a title and key/value rows inside an ASCII box."""
        items = items or [("", "")]
        label_w = max(len(label) for label, _ in items)
        value_w = max(1, width - label_w - 4)
        rows = [
            f"{label:<{label_w}} -> {truncate(value, value_w)}" for label, value in items
        ]
        title = truncate(title, width)
        inner = max([len(title), *(len(row) for row in rows)])
        border = "-" * (inner + 2)
        self.write(f"+{border}+\n")
        self.write(f"  {self.style('title', f'{title:<{inner}}')}\n")
        self.write(f"+{border}+\n")
        for row in rows:
            self.write(f"  {row}\n")
        self.write(f"+{border}+\n")


def truncate(text: str, max_len: int) -> str:
    """Truncate text; paths keep their tail, everything else keeps its head."""
    if max_len <= 3:
        return text[:max_len]
    if len(text) <= max_len:
        return text
    keep = max_len - 3
    if "/" in text or "\\" in text:
        return "..." + text[-keep:]
    return text[:keep] + "..."


# ---------------------------------------------------------------------------
# Commands
#
# Slash commands power everything the discussion container does not, on both
# the CLI and the web client.
# ---------------------------------------------------------------------------

COMMAND_RE = re.compile(r"^/(?P<name>[a-zA-Z][\w-]*)\s*(?P<args>.*)$", re.DOTALL)


@dataclass
class Command:
    name: str
    description: str
    handler: Callable[..., Any]


class CommandRegistry:
    """Registry of slash commands, keyed by name."""

    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(self, name: str, description: str, handler: Callable[..., Any]) -> None:
        self._commands[name] = Command(name, description, handler)

    def unregister(self, name: str) -> None:
        self._commands.pop(name, None)

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def describe(self) -> list[dict[str, str]]:
        return [
            {"name": c.name, "description": c.description}
            for c in sorted(self._commands.values(), key=lambda c: c.name)
        ]

    @staticmethod
    def parse(text: str) -> tuple[str, str] | None:
        """Split ``/name args`` into ``(name, args)``; ``None`` when not a command."""
        match = COMMAND_RE.match((text or "").strip())
        if not match:
            return None
        return match.group("name").lower(), match.group("args").strip()

    async def invoke(self, text: str, **context: Any) -> Any:
        parsed = self.parse(text)
        if parsed is None:
            return None
        name, args = parsed
        command = self.get(name)
        if command is None:
            return {"ok": False, "message": f"Unknown command: /{name}"}
        result = command.handler(args, **context)
        if inspect.isawaitable(result):
            result = await result
        return result


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


@dataclass
class Block:
    """A chronological unit of request or response content."""

    id: str
    kind: str
    role: str = "assistant"
    text: str = ""
    session_id: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "role": self.role,
            "text": self.text,
            "session": self.session_id,
            "created_at": self.created_at,
        }


#: Argument and result names whose value is never published.
SECRET_KEYS = (
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "passphrase",
    "password",
    "secret",
    "token",
)

#: What a redacted value is replaced with.
REDACTED = "[redacted]"


def secret_key(name: str) -> bool:
    """Whether an argument name looks like it carries a credential."""
    lowered = str(name).lower()
    return any(needle in lowered for needle in SECRET_KEYS)


def redact(value: Any, limit: int = 512) -> Any:
    """A JSON safe copy of a value with secrets removed and long text cut."""
    if isinstance(value, dict):
        return {
            str(k): REDACTED if secret_key(k) else redact(v, limit)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, limit) for v in value]
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value), limit)


@dataclass
class ToolCall:
    """One tool invocation of a run: what was called, with what, and how it went.

    The record is what makes a run auditable: it is published as `tool.start`
    when the model asks for the call and as `tool.end` when the result comes
    back, and it is kept on the `RunResult` so a caller can inspect the whole
    sequence afterwards.
    """

    id: str
    name: str
    call_id: str | None = None
    arguments: Any = field(default_factory=dict)
    result: str = ""
    ok: bool = True
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None

    @property
    def done(self) -> bool:
        return self.ended_at is not None

    @property
    def duration(self) -> float:
        """Seconds the call took, so far or in total."""
        return (self.ended_at or time.time()) - self.started_at

    def finish(self, result: str, ok: bool = True) -> "ToolCall":
        self.result = result
        self.ok = ok
        self.ended_at = time.time()
        return self

    def signature(self) -> str:
        """The call as a single readable line: `name(key=value, …)`."""
        args = self.arguments
        if isinstance(args, dict):
            inner = ", ".join(f"{k}={json_dump(v)}" for k, v in args.items())
        elif args in ("", None):
            inner = ""
        else:
            inner = json_dump(args)
        return f"{self.name}({inner})"

    def report(self) -> str:
        """The call and, once it is done, its outcome, duration and result."""
        line = self.signature()
        if not self.done:
            return line
        status = "ok" if self.ok else "failed"
        line = f"{line} -> {status} in {self.duration * 1000:.0f} ms"
        return f"{line}\n{self.result}" if self.result else line

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "call_id": self.call_id,
            "arguments": self.arguments,
            "result": self.result,
            "ok": self.ok,
            "done": self.done,
            "duration": self.duration,
            "started_at": self.started_at,
        }


def json_dump(value: Any) -> str:
    """A value as compact JSON, falling back to its string form."""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


@dataclass
class RunResult:
    output: str = ""
    blocks: list[Block] = field(default_factory=list)
    tools: list[ToolCall] = field(default_factory=list)
    session_id: str | None = None
    prompt: str = ""
    error: str | None = None
    #: One of `ErrorKind` when the run failed, so a caller can tell a rate limit
    #: from a bad credential without reading `error`.
    error_kind: str | None = None
    #: Tokens and cost of every model call the run made, specialists included.
    usage: Usage = field(default_factory=Usage)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def transient(self) -> bool:
        """True when the run failed for a reason another run might get past."""
        return self.error_kind in ErrorKind.TRANSIENT

    def fail(self, error: str, kind: str = ErrorKind.INTERNAL) -> "RunResult":
        """Record a failure and its kind on the result."""
        self.error = error
        self.error_kind = kind
        return self

    def text_of(self, kind: str) -> str:
        return "".join(b.text for b in self.blocks if b.kind == kind)


class Engine:
    """The agentic loop on its own: a model, its tools and a block stream.

    `Engine` is the embeddable half of the harness. It owns nothing but a
    configuration, an event bus, a prompt renderer, a tool registry and a model
    pool — no storage, no sessions, no conversation memory, no git checkouts, no
    console and no web layer — so an application that already has that
    infrastructure can drive inference and tool calls without inheriting any of
    it::

        engine = Engine(api_key=..., model=..., env=False)
        result = await engine.run(my_messages, tools=my_tools)

    `run` takes the model input verbatim (a prompt string or chat messages the
    caller assembled from its own history) and returns a `RunResult`. `Agent`
    subclasses this and adds the batteries.
    """

    #: Delta stream events of the SDK mapped onto block kinds.
    DELTA_KINDS = {
        "response.reasoning_summary_text.delta": "reasoning",
        "response.reasoning_text.delta": "reasoning",
        "response.output_text.delta": "output",
    }

    def __init__(
        self,
        config: AgentConfig | None = None,
        *,
        events: EventBus | None = None,
        renderer: ConsoleRenderer | None = None,
        prompts: PromptRenderer | None = None,
        tools: ToolRegistry | None = None,
        models: ModelPool | None = None,
        workspace: Path | str | None = None,
        console: bool = False,
        env: bool = True,
        config_file: bool | str | Path | None = True,
        **overrides: Any,
    ) -> None:
        base = config or AgentConfig()
        #: The config file this instance was resolved from, when there was one.
        self.config_file = self.locate_config(config_file)
        if self.config_file is not None:
            base = base.with_file(self.config_file)
        self.config = (base.with_env() if env else base).merge(**overrides)
        self.events = events or EventBus()
        self.prompts = prompts or PromptRenderer()
        self.tools = tools or ToolRegistry(shell=self.config.shell_enabled)
        self.models = models or ModelPool(RetryPolicy.from_config(self.config))
        #: Default directory the built-in file and shell tools are sandboxed to.
        #: Without one an embedded engine runs with no built-in tools at all.
        self.workspace = Path(workspace).expanduser().resolve() if workspace else None
        self.renderer = renderer
        if console and renderer is None and not self.config.quiet:
            self.renderer = ConsoleRenderer(color=self.config.color)
        if self.renderer is not None:
            self.renderer.attach(self.events)

    # -- configuration ------------------------------------------------------

    @staticmethod
    def locate_config(config_file: bool | str | Path | None = True) -> Path | None:
        """Resolve the ``config_file`` argument to a path, or to nothing.

        ``True`` searches (`find_config_file`), ``False`` or ``None`` skips the
        file layer entirely, and a path is taken as given — a path that does not
        exist is an error, because the caller asked for that file by name.
        """
        if config_file is None or config_file is False:
            return None
        if config_file is True:
            return find_config_file()
        file = Path(config_file).expanduser()
        if not file.is_file():
            raise FileNotFoundError(f"no such config file: {file}")
        return file

    def resolve_config(self, config: AgentConfig | None = None, **overrides: Any) -> AgentConfig:
        """Resolve the config used for a run.

        The ``input`` override is a file path or raw text; when it is a path the
        file is read and the text is assigned to ``config.input`` so templates
        can inject it.
        """
        base = (config or self.config).merge(**overrides)
        source = overrides.get("input") or base.input_source or base.input
        text = resolve_input(source) if source else ""
        return base.merge(input_source=str(source or ""), input=text)

    # -- model / sdk --------------------------------------------------------

    def build_model(self, config: AgentConfig, role: str = LEADER_ROLE) -> Any:
        """Return the SDK model for `role`, or ``None`` when it is unconfigured.

        Models come from the pool, so a role that shares an endpoint or a model
        name with another role reuses the same client and wrapper.
        """
        from agents import set_default_openai_client, set_tracing_disabled

        spec = config.model_spec(role)
        if spec is None:
            return None
        set_tracing_disabled(True)
        model = self.models.model(spec)
        if role == LEADER_ROLE:
            set_default_openai_client(self.models.client(spec))
        return model

    def build_policy(self, config: AgentConfig) -> RetryPolicy:
        """The policy every model call of a run is made under.

        Override to bound a role differently, to widen the transient set or to
        take the numbers from somewhere other than the configuration.
        """
        return RetryPolicy.from_config(config)

    def retry_reporter(
        self, session_id: str | None = None
    ) -> Callable[[dict[str, Any]], Awaitable[None]]:
        """A `RetryPolicy` callback that publishes `model.retry` on the bus."""

        async def report(info: dict[str, Any]) -> None:
            await self.events.publish(
                EventType.MODEL_RETRY, session_id=session_id, **info
            )

        return report

    def build_vision(
        self,
        config: AgentConfig,
        *,
        usage: "Usage | None" = None,
        session_id: str | None = None,
    ) -> Callable[[str, str], Awaitable[str]] | None:
        """Return an image describing delegate, or ``None`` when the leader sees.

        Without a separate vision model the leader is assumed to be multimodal
        and receives image data directly; with one, every image is routed to the
        specialist in a single completion and only its text answer comes back.
        """
        spec = config.model_spec(VISION_ROLE)
        if spec is None:
            return None
        policy = self.build_policy(config)
        on_retry = self.retry_reporter(session_id)

        async def describe(data_url: str, question: str = "") -> str:
            return await self.models.describe_image(
                spec,
                data_url,
                question,
                instructions=config.vision_instructions,
                max_tokens=config.vision_max_tokens or None,
                policy=policy,
                usage=usage,
                on_retry=on_retry,
            )

        return describe

    def build_tools(
        self,
        workspace: Workspace,
        config: AgentConfig | None = None,
        checkout: "Checkout | None" = None,
        *,
        usage: "Usage | None" = None,
        session_id: str | None = None,
    ) -> list[Any]:
        resolved = config or self.config
        vision = self.build_vision(resolved, usage=usage, session_id=session_id)
        return self.tools.build(workspace, vision=vision, checkout=checkout)

    def build_sdk_agent(self, config: AgentConfig, tools: list[Any]) -> Any:
        from agents import Agent as SdkAgent

        return SdkAgent(
            name=config.name,
            instructions=config.instructions,
            model=self.build_model(config),
            tools=tools,
        )

    def build_workspace(
        self, workspace: "Workspace | Path | str | None", config: AgentConfig
    ) -> Workspace | None:
        """Coerce whatever a caller passed as a workspace into a `Workspace`."""
        target = workspace if workspace is not None else self.workspace
        if target is None or isinstance(target, Workspace):
            return target
        return Workspace(target, shell_timeout=config.shell_timeout)

    # -- run ----------------------------------------------------------------

    async def run(
        self,
        model_input: str | list[dict[str, Any]],
        *,
        config: AgentConfig | None = None,
        tools: list[Any] | None = None,
        workspace: "Workspace | Path | str | None" = None,
        session_id: str | None = None,
        checkout: "Checkout | None" = None,
        **overrides: Any,
    ) -> RunResult:
        """Run the agentic loop over `model_input` and return a `RunResult`.

        `model_input` is used verbatim: a prompt string, or the chat messages
        the caller assembled from whatever history it keeps. `tools` replaces
        the built-in tool list; without it, tools are built for `workspace` (or
        the engine's default one) and there are none when neither is set.
        Failures are reported on the result rather than raised.
        """
        resolved = self.resolve_config(config, **overrides)
        result = RunResult(session_id=session_id or uuid.uuid4().hex[:12])
        async with self.running(resolved, result):
            result.prompt = prompt_of(model_input)
            await self.turn(
                model_input,
                resolved,
                result,
                tools=tools,
                workspace=workspace,
                checkout=checkout,
            )
        return result

    @contextlib.asynccontextmanager
    async def running(
        self, config: AgentConfig, result: RunResult
    ) -> AsyncIterator[RunResult]:
        """Publish the lifecycle events of a run, trap and classify its failures.

        The accounting of the result is published as well: while the body runs
        it is the sink every model call underneath finds, and `agent.end` always
        carries the tokens it collected, priced when the configuration says what
        a token costs.
        """
        token = _USAGE.set(result.usage)
        await self.events.publish(
            EventType.AGENT_START, session_id=result.session_id, config=config
        )
        try:
            yield result
        except asyncio.CancelledError:
            result.fail("cancelled", ErrorKind.CANCELLED)
            await self.events.publish(
                EventType.AGENT_ERROR,
                session_id=result.session_id,
                error=result.error,
                error_kind=result.error_kind,
            )
            raise
        except Exception as exc:
            result.fail(str(exc) or type(exc).__name__, classify_error(exc))
            await self.events.publish(
                EventType.AGENT_ERROR,
                session_id=result.session_id,
                error=result.error,
                error_kind=result.error_kind,
            )
        finally:
            result.usage.price(config.cost_input, config.cost_output)
            _USAGE.reset(token)
            await self.events.publish(
                EventType.AGENT_END,
                session_id=result.session_id,
                output=result.output,
                error=result.error,
                error_kind=result.error_kind,
                usage=result.usage.to_dict(),
                config=config,
            )

    async def turn(
        self,
        model_input: str | list[dict[str, Any]],
        config: AgentConfig,
        result: RunResult,
        *,
        tools: list[Any] | None = None,
        workspace: "Workspace | Path | str | None" = None,
        checkout: "Checkout | None" = None,
    ) -> RunResult:
        """One pass of the loop: build the tools, the model and stream it.

        The stream runs under the retry policy of the run, but only while it has
        produced nothing: once a block or a tool call exists, a second attempt
        would replay work the consumer has already seen, so a failure after that
        point is reported instead.
        """
        sandbox = self.build_workspace(workspace, config)
        if tools is None:
            tools = (
                self.build_tools(
                    sandbox,
                    config,
                    checkout,
                    usage=result.usage,
                    session_id=result.session_id,
                )
                if sandbox
                else []
            )
        sdk_agent = self.build_sdk_agent(config, tools)
        return await self.build_policy(config).call(
            lambda: self.stream(sdk_agent, model_input, config, result),
            role=LEADER_ROLE,
            bounded=False,
            resumable=lambda: not (result.blocks or result.tools),
            on_retry=self.retry_reporter(result.session_id),
        )

    async def stream(
        self,
        sdk_agent: Any,
        model_input: str | list[dict[str, str]],
        config: AgentConfig,
        result: RunResult,
    ) -> RunResult:
        """Consume the SDK event stream, publishing block events as it goes.

        `model_input` is the prompt on its own, or a replayed transcript with
        the prompt as its newest message. The read is bounded by a stall guard:
        a provider that stops sending for longer than the budget of the run ends
        the attempt with `ErrorKind.TIMEOUT` instead of hanging the caller, and
        whatever the attempt spent is accounted even when it failed.
        """
        from agents import RawResponsesStreamEvent, RunItemStreamEvent, Runner

        session_id = result.session_id
        policy = self.build_policy(config)
        streamed = Runner.run_streamed(
            starting_agent=sdk_agent, input=model_input, max_turns=config.max_turns
        )
        blocks: dict[str, Block] = {}
        current: Block | None = None

        def new_block(kind: str) -> Block:
            block = Block(
                id=f"{session_id}-{len(result.blocks)}", kind=kind, session_id=session_id
            )
            blocks[block.id] = block
            result.blocks.append(block)
            return block

        async def open_block(kind: str) -> Block:
            block = new_block(kind)
            await self.events.publish(
                EventType.BLOCK_START,
                session_id=session_id,
                id=block.id,
                kind=kind,
                role=block.role,
            )
            return block

        async def close_block(block: Block | None) -> None:
            if block is None:
                return
            await self.events.publish(
                EventType.BLOCK_END,
                session_id=session_id,
                id=block.id,
                kind=block.kind,
                text=block.text,
            )

        async def begin_tool(item: Any) -> None:
            nonlocal current
            await close_block(current)
            current = None
            block = new_block("tool")
            call = self.tool_call_of(item, block.id)
            result.tools.append(call)
            block.text = call.report()
            await self.events.publish(
                EventType.TOOL_START,
                session_id=session_id,
                id=block.id,
                kind="tool",
                name=call.name,
                call_id=call.call_id,
                arguments=call.arguments,
                text=block.text,
            )

        async def finish_tool(item: Any) -> None:
            output, ok = self.tool_result_of(item)
            call = self.pending_tool(result, getattr(item, "call_id", None))
            if call is None:
                return
            call.finish(output, ok)
            block = blocks.get(call.id)
            if block is not None:
                block.text = call.report()
            await self.events.publish(
                EventType.TOOL_END,
                session_id=session_id,
                id=call.id,
                kind="tool",
                name=call.name,
                call_id=call.call_id,
                arguments=call.arguments,
                ok=call.ok,
                result=call.result,
                duration=call.duration,
                text=call.report(),
            )

        events = streamed.stream_events().__aiter__()
        try:
            while True:
                waiting = any(not call.done for call in result.tools)
                try:
                    event = await self.next_event(
                        events, self.stall_timeout(policy, config, waiting)
                    )
                except StopAsyncIteration:
                    break
                if isinstance(event, RunItemStreamEvent):
                    if event.name == "tool_called":
                        await begin_tool(event.item)
                    elif event.name == "tool_output":
                        await finish_tool(event.item)
                    continue
                if not isinstance(event, RawResponsesStreamEvent):
                    continue
                data = event.data
                kind = self.DELTA_KINDS.get(getattr(data, "type", ""))
                if kind is not None:
                    delta = getattr(data, "delta", "") or ""
                    if not delta:
                        continue
                    if current is None or current.kind != kind:
                        await close_block(current)
                        current = await open_block(kind)
                    current.text += delta
                    await self.events.publish(
                        EventType.BLOCK_DELTA,
                        session_id=session_id,
                        id=current.id,
                        kind=kind,
                        text=delta,
                    )
                elif getattr(data, "type", "") == "response.function_call_arguments.delta":
                    # The call itself is published from the run item event; here
                    # the only job is to end the block the model was writing.
                    await close_block(current)
                    current = None
        except BaseException:
            self.cancel_stream(streamed)
            raise
        finally:
            self.models.record(usage_of(streamed), result.usage)
        await close_block(current)
        result.output = streamed.final_output or result.text_of("output")
        return result

    def stall_timeout(
        self, policy: RetryPolicy, config: AgentConfig, waiting: bool = False
    ) -> float:
        """How long the leader may say nothing before the attempt is failed.

        The SDK runs tool calls inside the stream, so while one is still
        outstanding the budget is extended by the time a tool is allowed to
        take; otherwise a slow shell command would be indistinguishable from a
        stalled provider.
        """
        if not policy.timeout:
            return 0.0
        return policy.timeout + config.shell_timeout if waiting else policy.timeout

    async def next_event(self, events: Any, timeout: float = 0.0) -> Any:
        """The next event of an SDK stream, bounded by a stall `timeout`."""
        if not timeout:
            return await events.__anext__()
        try:
            async with asyncio.timeout(timeout):
                return await events.__anext__()
        except TimeoutError as exc:
            raise ModelError(
                f"the model sent nothing for {timeout:g}s",
                ErrorKind.TIMEOUT,
                role=LEADER_ROLE,
            ) from exc

    @staticmethod
    def cancel_stream(streamed: Any) -> None:
        """Stop an SDK stream that will not be read any further."""
        cancel = getattr(streamed, "cancel", None)
        if callable(cancel):
            with contextlib.suppress(Exception):
                cancel()

    # -- tool call transparency ---------------------------------------------

    #: Longest argument or result value published before it is cut.
    TOOL_VALUE_LIMIT = 512

    def tool_call_of(self, item: Any, block_id: str) -> ToolCall:
        """The `ToolCall` of an SDK tool call item, with arguments redacted."""
        raw = getattr(item, "raw_item", None)
        name = (
            getattr(item, "tool_name", None)
            or getattr(raw, "name", None)
            or getattr(raw, "type", None)
            or "tool"
        )
        call_id = getattr(raw, "call_id", None) or getattr(raw, "id", None)
        return ToolCall(
            id=block_id,
            name=str(name),
            call_id=str(call_id) if call_id else None,
            arguments=self.redact_arguments(getattr(raw, "arguments", None)),
        )

    def tool_result_of(self, item: Any) -> tuple[str, bool]:
        """The redacted result of an SDK tool output item and whether it is ok."""
        output = getattr(item, "output", None)
        if output is None:
            raw = getattr(item, "raw_item", None)
            output = raw.get("output") if isinstance(raw, dict) else raw
        text = output if isinstance(output, str) else json_dump(redact(output))
        ok = not text.lstrip().lower().startswith("error")
        return str(redact(text, self.TOOL_VALUE_LIMIT)), ok

    def redact_arguments(self, arguments: Any) -> Any:
        """Tool arguments as JSON safe data with credentials removed."""
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except (TypeError, ValueError):
                pass
        return redact(arguments if arguments is not None else {}, self.TOOL_VALUE_LIMIT)

    @staticmethod
    def pending_tool(result: RunResult, call_id: Any) -> ToolCall | None:
        """The call an output belongs to: matched by id, else the oldest open one."""
        if call_id is not None:
            for call in result.tools:
                if call.call_id == str(call_id):
                    return call
        return next((call for call in result.tools if not call.done), None)

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Release the resources of the engine: its clients and models."""
        self.models.clear()

    async def aclose(self) -> None:
        await asyncio.to_thread(self.close)

    async def __aenter__(self) -> "Engine":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()


def prompt_of(model_input: str | list[dict[str, Any]]) -> str:
    """The prompt of a model input: itself, or the newest message in a list."""
    if isinstance(model_input, str):
        return model_input
    return str(model_input[-1].get("content", "")) if model_input else ""


class Agent(Engine):
    """The one API: a feature rich agent harness.

    ``await Agent().run(template)`` resolves the configuration, renders the
    template against it and runs the agentic loop, streaming block events onto
    the bus. Every step is an overridable method or an injectable collaborator.

    Everything below the loop itself — sessions, workspaces, storage,
    conversation memory, git checkouts, slash commands, console and web — lives
    here; consumers that only want inference and tool calls use `Engine`.
    """

    #: Template used when the harness is driven by the web layer.
    template: str | None = None

    def __init__(
        self,
        config: AgentConfig | None = None,
        *,
        events: EventBus | None = None,
        renderer: ConsoleRenderer | None = None,
        prompts: PromptRenderer | None = None,
        tools: ToolRegistry | None = None,
        models: ModelPool | None = None,
        store: Store | None = None,
        cache: Cache | None = None,
        sessions: SessionManager | None = None,
        memory: "ConversationMemory | None" = None,
        repos: "RepoManager | None" = None,
        commands: CommandRegistry | None = None,
        console: bool = True,
        config_file: bool | str | Path | None = True,
        **overrides: Any,
    ) -> None:
        super().__init__(
            config,
            events=events,
            renderer=renderer,
            prompts=prompts,
            tools=tools,
            models=models,
            console=console,
            config_file=config_file,
            **overrides,
        )
        self.store = store or SqliteStore(self.config.db_path)
        self.cache = cache or LruCache(self.config.cache_size)
        self.sessions = sessions or SessionManager(
            root=self.config.workspace_root,
            ttl=self.config.session_ttl,
            store=self.store,
            cache=self.cache,
            prefix=self.config.name,
            keep_workspace=self.config.keep_workspace,
            seed=self.config.workspace_seed,
            durable=self.config.session_durable,
            sweep_interval=self.config.session_sweep_interval,
        )
        self.repos = repos or RepoManager(
            RepoSpec.from_config(self.config), timeout=self.config.repo_timeout
        )
        self.memory = memory or ConversationMemory(
            self.store,
            enabled=self.config.memory_enabled,
            max_turns=self.config.memory_max_turns,
            max_chars=self.config.memory_max_chars,
        )
        if self.memory.summarizer is None:
            self.memory.summarizer = self.build_summarizer(self.config)
        self.commands = commands or CommandRegistry()
        self.register_default_commands()

    # -- prompt -------------------------------------------------------------

    def build_prompt(self, template: str, config: AgentConfig, **context: Any) -> str:
        """Render the consumer's template against the resolved config."""
        return self.decorate_prompt(
            self.prompts.render(template, config, **context), config
        )

    def decorate_prompt(self, prompt: str, config: AgentConfig) -> str:
        """Append harness supplied context (attachments) to a rendered prompt."""
        files = config.extras.get("attachments") or []
        if not files or "## Attached files" in prompt:
            return prompt
        listing = "\n".join(f"- {name}" for name in files)
        return f"{prompt}\n\n## Attached files\n\n{listing}"

    # -- model / sdk --------------------------------------------------------

    def build_summarizer(
        self, config: AgentConfig
    ) -> Callable[[list[Turn]], Awaitable[str]] | None:
        """Return a delegate that compresses forgotten turns, or ``None``.

        Summarising is opt-in (`memory_summary`); the `summary` role falls back
        to the leader so a second model is optional.
        """
        if not (config.memory_enabled and config.memory_summary):
            return None
        spec = config.model_spec(SUMMARY_ROLE) or config.model_spec(LEADER_ROLE)
        if spec is None:
            return None

        policy = self.build_policy(config)

        async def summarize(turns: list[Turn]) -> str:
            transcript = "\n\n".join(f"{t.role}: {t.text}" for t in turns)
            return await self.models.complete(
                spec,
                [
                    {"role": "system", "content": config.summary_instructions},
                    {"role": "user", "content": transcript},
                ],
                max_tokens=config.summary_max_tokens or None,
                policy=policy,
                on_retry=self.retry_reporter(),
            )

        return summarize

    # -- run ----------------------------------------------------------------

    async def run(
        self,
        template: str,
        *,
        config: AgentConfig | None = None,
        session: Session | str | None = None,
        context: dict[str, Any] | None = None,
        **overrides: Any,
    ) -> RunResult:
        """Render `template` and run the agentic loop, returning a `RunResult`.

        Unlike `Engine.run`, which takes the model input verbatim, this renders
        the consumer's template inside a session workspace and replays what that
        session remembers.
        """
        resolved = self.resolve_config(config, **overrides)
        session_obj = self.acquire_session(session)
        await self.prepare_session(session_obj)
        result = RunResult(session_id=session_obj.id)
        async with self.running(resolved, result):
            result.prompt = self.build_prompt(template, resolved, **(context or {}))
            await self.turn(
                self.build_input(result.prompt, session_obj, resolved),
                resolved,
                result,
                workspace=session_obj.workspace,
                checkout=self.checkout(session_obj),
            )
            await self.remember(session_obj, resolved, result)
        return result

    def acquire_session(self, session: Session | str | None) -> Session:
        if isinstance(session, Session):
            return session
        self.sessions.purge_expired()
        return self.sessions.ensure(session)

    # -- memory -------------------------------------------------------------

    def build_input(
        self, prompt: str, session: Session, config: AgentConfig
    ) -> str | list[dict[str, str]]:
        """The model input for a run: the prompt, preceded by what is remembered.

        A session with nothing remembered is a plain string, which is what the
        SDK expects for a one shot run; otherwise the transcript is replayed as
        chat messages and the prompt becomes the newest user message.
        """
        history = self.memory.history(session.id) if config.memory_enabled else []
        if not history:
            return prompt
        return [*history, {"role": "user", "content": prompt}]

    async def remember(
        self, session: Session, config: AgentConfig, result: RunResult
    ) -> None:
        """Append the exchange of a run to the transcript of its session.

        What is remembered is the input the user actually wrote (falling back to
        the rendered prompt) and the output of the model — not the rendered
        template, which would be replayed verbatim on every turn. Override to
        remember more, less, or something else entirely.
        """
        if not config.memory_enabled or not result.ok or not result.output.strip():
            return
        asked = (config.input or "").strip() or result.prompt
        await self.memory.remember(
            session.id,
            [Turn("user", asked), Turn("assistant", result.output.strip())],
        )

    # -- repository ---------------------------------------------------------

    def checkout(self, session: Session | None) -> Checkout | None:
        """Return the git checkout of `session`, when it has one."""
        meta = (session.meta.get("repo") if session else None) or {}
        return Checkout.from_meta(self.repos, meta) if meta.get("path") else None

    async def prepare_session(self, session: Session) -> Checkout | None:
        """Clone the default repository into a fresh session workspace.

        Idempotent: a session that already owns a checkout keeps it, and a run
        is never blocked by a clone failure (it is reported and the session
        simply has no checkout).
        """
        existing = self.checkout(session)
        if existing is not None or not self.repos.configured:
            return existing
        if not self.config.repo_clone:
            return None
        try:
            checkout = await self.repos.clone(session.workspace, session_id=session.id)
        except RepoError as exc:
            await self.events.publish(
                EventType.LOG,
                session_id=session.id,
                kind="error",
                message=f"clone failed: {exc}",
            )
            return None
        session.meta["repo"] = checkout.to_dict()
        self.sessions.update(session)
        await self.events.publish(
            EventType.SESSION_OPEN,
            session_id=session.id,
            kind="log",
            repo=checkout.to_dict(),
        )
        return checkout

    # -- commands -----------------------------------------------------------

    def register_default_commands(self) -> None:
        """Register the built-in slash commands. Override to add your own."""

        def help_command(_args: str, **_: Any) -> dict[str, Any]:
            return {"ok": True, "commands": self.commands.describe()}

        async def new_session(_args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            if session_id:
                self.sessions.close(session_id)
                self.memory.forget(session_id)
            session = self.sessions.create()
            checkout = await self.prepare_session(session)
            message = "New session"
            if checkout is not None:
                message += f" on branch {checkout.branch} of {self.repos.spec.slug}"
            return {"ok": True, "session": session.to_dict(), "message": message}

        def end_session(args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            target = args or session_id or ""
            closed = self.sessions.close(target)
            if closed:
                self.memory.forget(target)
            return {
                "ok": closed,
                "session": None,
                "message": "Session closed" if closed else "No such session",
            }

        def forget_command(_args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            if not session_id:
                return {"ok": False, "message": "No session"}
            remembered = len(self.memory.transcript(session_id))
            self.memory.forget(session_id)
            return {"ok": True, "message": f"Forgot {remembered} remembered turns"}

        def list_sessions(_args: str, **_: Any) -> dict[str, Any]:
            return {"ok": True, "sessions": [s.to_dict() for s in self.sessions.list()]}

        def use_session(args: str, **_: Any) -> dict[str, Any]:
            session = self.sessions.get(args.strip())
            if session is None:
                return {"ok": False, "message": f"No such session: {args.strip()}"}
            return {"ok": True, "session": session.to_dict(), "message": "Session switched"}

        def model_command(args: str, **_: Any) -> dict[str, Any]:
            if args:
                self.config = self.config.merge(model=args.strip())
            return {"ok": True, "message": f"Model: {self.config.model}"}

        def vision_command(args: str, **_: Any) -> dict[str, Any]:
            if args:
                value = args.strip()
                self.config = self.config.merge(
                    vision_model="" if value in ("none", "off") else value
                )
            spec = self.config.model_spec(VISION_ROLE)
            name = spec.name if spec is not None else "(leader)"
            return {"ok": True, "message": f"Vision model: {name}"}

        def current_session(session_id: str | None) -> Session | None:
            return self.sessions.get(session_id)

        def repo_command(args: str, **_: Any) -> dict[str, Any]:
            if args:
                value = args.strip()
                self.config = self.config.merge(
                    repo_url="" if value in ("none", "off") else value
                )
                self.repos.spec = RepoSpec.from_config(self.config)
            url = self.repos.spec.url or "(unset)"
            return {"ok": True, "message": f"Repository: {url}"}

        async def clone_command(_args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            session = current_session(session_id)
            if session is None:
                return {"ok": False, "message": "No live session"}
            if not self.repos.configured:
                return {"ok": False, "message": "No repository is configured"}
            try:
                checkout = await self.prepare_session(session)
            except RepoError as exc:
                return {"ok": False, "message": str(exc)}
            if checkout is None:
                return {"ok": False, "message": "The repository could not be cloned"}
            return {
                "ok": True,
                "session": session.to_dict(),
                "message": (
                    f"Cloned {self.repos.spec.slug} into {checkout.path.name} "
                    f"on branch {checkout.branch}"
                ),
            }

        async def repo_action(
            session_id: str | None, action: Callable[[Checkout], Awaitable[str]]
        ) -> dict[str, Any]:
            checkout = self.checkout(current_session(session_id))
            if checkout is None:
                return {"ok": False, "message": "This session has no git checkout"}
            try:
                return {"ok": True, "message": await action(checkout)}
            except RepoError as exc:
                return {"ok": False, "message": str(exc)}

        async def status_command(_args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            return await repo_action(session_id, lambda c: c.status())

        async def commit_command(args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            return await repo_action(session_id, lambda c: c.commit(args))

        async def push_command(_args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            return await repo_action(session_id, lambda c: c.push())

        async def pull_request_command(args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            title, _, body = args.partition("\n")

            async def open_pull(checkout: Checkout) -> str:
                pull = await checkout.pull_request(title.strip(), body.strip())
                return f"Pull request #{pull['number']}: {pull['url']}"

            return await repo_action(session_id, open_pull)

        async def publish_command(args: str, session_id: str | None = None, **_: Any) -> dict[str, Any]:
            title, _, body = args.partition("\n")

            async def publish(checkout: Checkout) -> str:
                result = await checkout.publish(title.strip(), body=body.strip())
                pull = result["pull_request"]
                return f"{result['commit']} -> pull request #{pull['number']}: {pull['url']}"

            return await repo_action(session_id, publish)

        self.commands.register("help", "List the available commands", help_command)
        self.commands.register("new", "Start a new isolated session", new_session)
        self.commands.register("end", "End the current session and wipe it", end_session)
        self.commands.register("sessions", "List live sessions", list_sessions)
        self.commands.register("use", "Switch to an existing session", use_session)
        self.commands.register(
            "forget", "Forget the conversation of this session", forget_command
        )
        self.commands.register("model", "Show or set the model", model_command)
        self.commands.register(
            "vision", "Show or set the vision model ('none' to unset)", vision_command
        )
        self.commands.register(
            "repo", "Show or set the default repository ('none' to unset)", repo_command
        )
        self.commands.register(
            "clone", "Clone the default repository into this session", clone_command
        )
        self.commands.register("status", "Show the git status of this session", status_command)
        self.commands.register("commit", "Commit every change with a message", commit_command)
        self.commands.register("push", "Push the session branch to the remote", push_command)
        self.commands.register(
            "pr", "Open a pull request: '/pr <title>' then an optional body", pull_request_command
        )
        self.commands.register(
            "publish", "Commit, push and open a pull request in one step", publish_command
        )

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Release every resource: workspaces are wiped, storage is closed.

        A durable manager keeps its workspaces and their records instead, so the
        next start rehydrates the sessions that were still live.
        """
        self.sessions.close_all()
        self.cache.clear()
        self.store.close()
        super().close()

    async def aclose(self) -> None:
        await self.sessions.stop_sweeper()
        await asyncio.to_thread(self.close)

    # -- application entry points -------------------------------------------

    async def serve(self, **overrides: Any) -> None:
        """Run the REST + websocket server backed by this agent."""
        await WebServer(self, **overrides).serve()

    def repl(self, **overrides: Any) -> "Repl":
        """The interactive terminal for this agent. Override to swap the class."""
        return Repl(self, **overrides)

    @classmethod
    def cli(cls, template: str | None = None, argv: list[str] | None = None, **kwargs: Any) -> int:
        """Run this agent as a CLI application. Returns a process exit code.

        Without a template the one `load_template` resolves is used, which the
        config file, ``AGENT_TEMPLATE`` or an ``agent_prompt.md`` beside the
        module can all supply.
        """
        args = parse_args(argv)
        kwargs.setdefault("config_file", args.config or True)
        agent = cls(**kwargs)
        return asyncio.run(agent.execute(template or load_template(agent.config), args))

    async def execute(self, template: str, args: argparse.Namespace) -> int:
        """Dispatch parsed CLI arguments to the repl, the run or the serve path.

        ``--serve`` wins, then an explicit ``--repl``, then ``--input``; with no
        arguments at all the terminal opens, because a harness with nothing to
        do is a harness waiting to be talked to.
        """
        self.template = template
        try:
            if args.serve:
                await self.serve()
                return 0
            if args.repl or not args.input:
                return await self.repl().start(opening=args.input)
            result = await self.run(template, input=args.input)
            return 0 if result.ok else 1
        finally:
            await self.aclose()


# ---------------------------------------------------------------------------
# Repl
#
# The terminal client: the same loop the web layer drives, reading lines from a
# terminal instead of a websocket. It wraps an agent harness instance and owns
# nothing of its own but the session it is pointed at.
# ---------------------------------------------------------------------------


class Repl:
    """An interactive terminal for an agent harness.

    Wrap any `Agent` (or a subclass) and call `start`::

        agent = Agent()
        await Repl(agent).start()

    A line that begins with ``/`` is dispatched to the agent's command registry,
    exactly as the web client dispatches it, and anything else is a prompt: it
    runs the agent's template in the current session and streams the answer
    through the agent's console renderer. The conversation is remembered, so the
    session carries from one prompt to the next until ``/new`` or ``/end``.

    Lines are read on a dedicated thread, so the event loop keeps running while
    the terminal waits: timers fire, the session sweeper sweeps and a run that is
    already streaming is never blocked by the prompt. Ctrl-C cancels the active
    run and leaves the repl open; Ctrl-D, ``/exit`` or ``/quit`` leaves it.
    """

    #: Commands the repl answers itself instead of passing to the agent.
    EXITS = ("exit", "quit")
    PROMPT = "> "

    def __init__(
        self,
        agent: "Agent",
        *,
        template: str | None = None,
        prompt: str | None = None,
        stream: Any = None,
        reader: Callable[[str], str] | None = None,
    ) -> None:
        self.agent = agent
        self.template = (
            template or getattr(agent, "template", None) or PASSTHROUGH_TEMPLATE
        )
        self.prompt = self.PROMPT if prompt is None else prompt
        self.stream = stream or sys.stdout
        #: Reads one line, given the prompt to show. Injected by the tests.
        self.reader = reader or self.read_line
        self.session: Session | None = None
        self.task: "asyncio.Task[Any] | None" = None
        self.running = False
        self._asks: "queue.Queue[str | None]" = queue.Queue()
        self._lines: "asyncio.Queue[str | None] | None" = None
        self._thread: threading.Thread | None = None

    # -- loop ---------------------------------------------------------------

    async def start(self, opening: str = "") -> int:
        """Read and answer until the user leaves. Returns a process exit code.

        `opening` is answered first, which is how ``--repl --input ...`` hands
        the command line prompt to an interactive session.
        """
        self.session = self.agent.sessions.ensure(None)
        await self.agent.prepare_session(self.session)
        self.agent.sessions.start_sweeper()
        self.greet()
        disarm = self.arm_interrupt()
        self.running = True
        try:
            if opening.strip():
                await self.dispatch(opening.strip())
            while self.running:
                line = await self.read()
                if line is None:  # Ctrl-D
                    self.emit("output", "\n")
                    break
                if line.strip():
                    await self.dispatch(line.strip())
        finally:
            self.running = False
            disarm()
            self.stop_reader()
        return 0

    async def dispatch(self, text: str) -> None:
        """Answer one line: a slash command, or a prompt for the agent."""
        parsed = CommandRegistry.parse(text)
        if parsed is None:
            await self.ask(text)
            return
        await self.command(text, parsed[0])

    async def ask(self, text: str) -> None:
        """Run one prompt in the current session, streaming the answer."""
        session = self.current()
        self.task = asyncio.create_task(
            self.agent.run(self.template, session=session, input=text)
        )
        try:
            await self.task
        except asyncio.CancelledError:
            self.emit("error", "\n(cancelled)\n")
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise  # the repl itself is going away, not just this run
        except Exception as exc:  # the run reported it; the repl stays open
            self.emit("error", f"{exc}\n")
        finally:
            self.task = None

    async def command(self, text: str, name: str = "") -> None:
        """Invoke a slash command and report what it returned."""
        name = name or (CommandRegistry.parse(text) or ("", ""))[0]
        if name in self.EXITS:
            self.running = False
            return
        result = await self.agent.commands.invoke(
            text, session_id=self.session.id if self.session else None, repl=self
        )
        self.adopt(result)
        self.report(result)

    # -- session ------------------------------------------------------------

    def current(self) -> Session:
        """The session prompts run in, replacing one that has gone away."""
        live = self.agent.sessions.get(self.session.id) if self.session else None
        if live is None:
            live = self.agent.sessions.ensure(None)
        self.session = live
        return live

    def adopt(self, result: Any) -> None:
        """Follow a command that started, switched or ended a session."""
        if not isinstance(result, dict) or "session" not in result:
            return
        session = result.get("session")
        if isinstance(session, dict):
            self.session = self.agent.sessions.get(str(session.get("id"))) or self.session
        elif session is None and result.get("ok"):
            self.session = None  # ended; the next prompt opens a fresh one

    # -- input --------------------------------------------------------------

    def read_line(self, prompt: str) -> str | None:
        """Read one line from the terminal. Returns ``None`` at end of input."""
        try:
            return input(prompt)
        except EOFError:
            return None

    async def read(self) -> str | None:
        """Ask the reader thread for the next line, without blocking the loop."""
        if self._lines is None:
            self._lines = asyncio.Queue()
            self.start_reader(asyncio.get_running_loop())
        self._asks.put(self.prompt)
        return await self._lines.get()

    def start_reader(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start the one thread that reads the terminal for this repl."""
        if self._thread is not None:
            return
        with contextlib.suppress(ImportError):
            import readline  # noqa: F401 - line editing and history for input()

        self._thread = threading.Thread(
            target=self.pump, args=(loop,), name="repl-reader", daemon=True
        )
        self._thread.start()

    def pump(self, loop: asyncio.AbstractEventLoop) -> None:
        """Reader thread: one line per request, delivered back to the loop."""
        while True:
            prompt = self._asks.get()
            if prompt is None:
                return
            try:
                line = self.reader(prompt)
            except (EOFError, KeyboardInterrupt):
                line = None
            except Exception:  # pragma: no cover - a broken terminal
                line = None
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self.deliver, line)

    def deliver(self, line: str | None) -> None:
        """Hand a line the reader produced to whoever is waiting for it."""
        if self._lines is not None:
            self._lines.put_nowait(line)

    def stop_reader(self) -> None:
        """Release the reader thread. A blocked ``input()`` ends with stdin."""
        self._asks.put(None)

    # -- interrupts ---------------------------------------------------------

    def arm_interrupt(self) -> Callable[[], None]:
        """Make Ctrl-C cancel the active run instead of killing the process.

        Returns the undo. The loop handler is used where there is one (POSIX)
        and a plain signal handler otherwise, and a platform that allows neither
        simply keeps its default behaviour.
        """
        loop = asyncio.get_running_loop()
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(signal.SIGINT, self.interrupt)
            return lambda: _quietly(loop.remove_signal_handler, signal.SIGINT)
        previous = signal.getsignal(signal.SIGINT)

        def handler(_signum: int, _frame: Any) -> None:
            loop.call_soon_threadsafe(self.interrupt)

        with contextlib.suppress(ValueError, OSError, AttributeError):
            signal.signal(signal.SIGINT, handler)
            return lambda: _quietly(signal.signal, signal.SIGINT, previous)
        return lambda: None

    def interrupt(self) -> None:
        """Ctrl-C: cancel the run in flight, or remind an idle prompt."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
            return
        self.emit("error", f"\n(/exit or Ctrl-D to leave)\n{self.prompt}")

    # -- output -------------------------------------------------------------

    def emit(self, kind: str, text: str) -> None:
        """Write through the agent's renderer so spacing stays consistent."""
        if not text:
            return
        renderer = getattr(self.agent, "renderer", None)
        if renderer is not None:
            renderer.emit(kind, text)
            return
        print(text, end="", file=self.stream, flush=True)

    def greet(self) -> None:
        """Print the banner the repl opens with."""
        renderer = getattr(self.agent, "renderer", None)
        if renderer is None:
            return
        renderer.banner(f"{self.agent.config.name} (repl)", self.banner_items())
        renderer.banners = False  # one banner per repl, not one per run

    def banner_items(self) -> list[tuple[str, str]]:
        """Key/value rows for the opening banner."""
        items = [
            item for item in self.agent.config.banner_items() if item[0] != "Input"
        ]
        file = getattr(self.agent, "config_file", None)
        if file is not None:
            items.append(("Config", str(file)))
        if self.session is not None:
            items.append(("Session", self.session.id))
            checkout = self.agent.checkout(self.session)
            if checkout is not None:
                items.append(("Branch", checkout.branch))
        items.append(("Commands", "/help, /exit"))
        return items

    def describe(self) -> list[dict[str, str]]:
        """The agent's commands plus the ones the repl answers itself."""
        own = [{"name": "exit", "description": "Leave the repl (/quit, Ctrl-D)"}]
        return sorted(
            [*self.agent.commands.describe(), *own], key=lambda c: c["name"]
        )

    def report(self, result: Any) -> None:
        """Print what a command returned: a listing, a message, or both."""
        if result is None:
            self.emit("error", "Not a command\n")
            return
        if not isinstance(result, dict):
            self.emit("output", f"{result}\n")
            return
        lines: list[str] = []
        if result.get("commands") is not None:
            width = max((len(c["name"]) for c in self.describe()), default=0)
            lines += [
                f"/{c['name']:<{width}}  {c['description']}" for c in self.describe()
            ]
        for session in result.get("sessions") or []:
            marker = "*" if self.session and session["id"] == self.session.id else " "
            lines.append(f"{marker} {session['id']}  {session['workspace']}")
        message = str(result.get("message") or "")
        if message:
            lines.append(message)
        if not lines:
            lines.append("ok" if result.get("ok", True) else "failed")
        self.emit("output" if result.get("ok", True) else "error", "\n".join(lines) + "\n")


def _quietly(action: Callable[..., Any], *args: Any) -> None:
    """Undo a handler installation without caring that it is already gone."""
    with contextlib.suppress(ValueError, RuntimeError, OSError, NotImplementedError):
        action(*args)


# ---------------------------------------------------------------------------
# Web
#
# A tiny REST surface for read only state plus a websocket hub (channels,
# subscriptions and named message handlers, in the spirit of SignalR and
# socket.io) that streams block events to connected clients.
# ---------------------------------------------------------------------------

#: Subset of the VS Code theme spec understood by the client.
THEME_KEYS = {
    "editor.background": "--bg",
    "editor.foreground": "--fg",
    "sideBar.background": "--bg-soft",
    "editorWidget.background": "--bg-raised",
    "input.background": "--input-bg",
    "input.foreground": "--input-fg",
    "input.border": "--input-border",
    "button.background": "--accent",
    "button.foreground": "--accent-fg",
    "focusBorder": "--focus",
    "panel.border": "--border",
    "descriptionForeground": "--muted",
    "errorForeground": "--error",
    "textLink.foreground": "--link",
    "textCodeBlock.background": "--code-bg",
    "badge.background": "--badge-bg",
    "badge.foreground": "--badge-fg",
    "scrollbarSlider.background": "--scroll",
}

MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_BYTES = 16 * 1024 * 1024


def load_vscode_theme(path: Path | str | None) -> dict[str, str]:
    """Map a VS Code theme file onto the CSS variables the client understands."""
    if not path:
        return {}
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    colors = data.get("colors") or {}
    theme = {
        css: str(colors[key]) for key, css in THEME_KEYS.items() if colors.get(key)
    }
    if data.get("type"):
        theme["--theme-type"] = str(data["type"])
    return theme


def safe_name(name: str) -> str:
    """Reduce an arbitrary client supplied file name to a safe leaf name."""
    leaf = Path(str(name or "file")).name
    leaf = re.sub(r"[^A-Za-z0-9._-]+", "_", leaf).strip("._") or "file"
    return leaf[:128]


class Connection:
    """One websocket client."""

    def __init__(self, hub: "Hub", websocket: Any) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.hub = hub
        self.ws = websocket
        self.channels: set[str] = set()
        self.session_id: str | None = None
        self.task: asyncio.Task[Any] | None = None

    async def send(self, message_type: str, **data: Any) -> None:
        payload = json.dumps({"type": message_type, **data})
        with contextlib.suppress(Exception):
            await self.ws.send(payload)

    async def fail(self, message: str) -> None:
        await self.send("error", message=message)

    async def cancel_task(self) -> bool:
        """Cancel the in-flight agent run, if any."""
        task, self.task = self.task, None
        if task is None or task.done():
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return True


class Hub:
    """Channel based websocket hub with named message handlers."""

    def __init__(self) -> None:
        self.connections: dict[str, Connection] = {}
        self.channels: dict[str, set[str]] = {}
        self._handlers: dict[str, Callable[[Connection, dict[str, Any]], Any]] = {}

    def on(self, message_type: str, handler: Callable[[Connection, dict[str, Any]], Any]) -> None:
        self._handlers[message_type] = handler

    def join(self, connection: Connection, channel: str) -> None:
        self.channels.setdefault(channel, set()).add(connection.id)
        connection.channels.add(channel)

    def leave(self, connection: Connection, channel: str) -> None:
        self.channels.get(channel, set()).discard(connection.id)
        connection.channels.discard(channel)

    async def broadcast(self, channel: str, message_type: str, **data: Any) -> None:
        for connection_id in list(self.channels.get(channel, ())):
            connection = self.connections.get(connection_id)
            if connection is not None:
                await connection.send(message_type, **data)

    async def send_all(self, message_type: str, **data: Any) -> None:
        for connection in list(self.connections.values()):
            await connection.send(message_type, **data)

    async def dispatch(self, connection: Connection, message: dict[str, Any]) -> None:
        handler = self._handlers.get(str(message.get("type", "")))
        if handler is None:
            await connection.fail(f"Unknown message type: {message.get('type')}")
            return
        result = handler(connection, message)
        if inspect.isawaitable(result):
            await result

    async def serve_connection(self, websocket: Any) -> None:
        """Read/dispatch loop for a single client."""
        from websockets.exceptions import ConnectionClosed

        connection = Connection(self, websocket)
        self.connections[connection.id] = connection
        await self.connected(connection)
        try:
            async for raw in websocket:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                if len(raw) > MAX_MESSAGE_BYTES:
                    await connection.fail("Message too large")
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await connection.fail("Invalid JSON")
                    continue
                if not isinstance(message, dict):
                    await connection.fail("Invalid message")
                    continue
                await self.dispatch(connection, message)
        except ConnectionClosed:
            pass
        finally:
            await connection.cancel_task()
            for channel in list(connection.channels):
                self.leave(connection, channel)
            self.connections.pop(connection.id, None)
            await self.disconnected(connection)

    # -- overridable lifecycle hooks ---------------------------------------

    async def connected(self, connection: Connection) -> None:
        await connection.send("ready", connection=connection.id)

    async def disconnected(self, connection: Connection) -> None:
        return None


class WebServer:
    """Serves the chat page, a read only REST API and the websocket hub.

    State changing operations (prompts, session management, cancellation) all
    travel over the hub so the client keeps a single ordered channel.
    """

    ASSETS = {
        "/": ("agent_ui.html", "text/html; charset=utf-8"),
        "/index.html": ("agent_ui.html", "text/html; charset=utf-8"),
        "/agent_ui.css": ("agent_ui.css", "text/css; charset=utf-8"),
        "/agent_ui.js": ("agent_ui.js", "text/javascript; charset=utf-8"),
    }

    def __init__(
        self,
        agent: "Agent",
        *,
        host: str | None = None,
        port: int | None = None,
        template: str | None = None,
        assets_root: Path | None = None,
        hub: Hub | None = None,
    ) -> None:
        self.agent = agent
        self.host = host or agent.config.host
        self.port = port or agent.config.port
        self.template = template or getattr(agent, "template", None) or PASSTHROUGH_TEMPLATE
        self.assets_root = assets_root or ROOT
        self.hub = hub or Hub()
        self.theme = load_vscode_theme(agent.config.theme)
        self.register_handlers()
        self.agent.events.on(EventType.ALL, self._on_agent_event)

    # -- agent bridge -------------------------------------------------------

    async def _on_agent_event(self, event: Event) -> None:
        if event.type in (EventType.AGENT_START,):
            return  # carries the config object, which is not serializable
        if event.session_id is None:
            return
        await self.hub.broadcast(
            self.channel(event.session_id), event.type, **self.encode(event)
        )

    @staticmethod
    def channel(session_id: str) -> str:
        return f"session:{session_id}"

    def encode(self, event: Event) -> dict[str, Any]:
        """Project an event onto a JSON safe payload. Override to add fields."""
        return {
            k: v
            for k, v in event.data.items()
            if isinstance(v, (str, int, float, bool, list, dict, type(None)))
        }

    # -- handlers -----------------------------------------------------------

    def register_handlers(self) -> None:
        self.hub.on("hello", self.on_hello)
        self.hub.on("prompt", self.on_prompt)
        self.hub.on("command", self.on_command)
        self.hub.on("cancel", self.on_cancel)
        self.hub.on("ping", lambda conn, _msg: conn.send("pong"))

    async def on_hello(self, connection: Connection, message: dict[str, Any]) -> None:
        session = self.agent.sessions.ensure(message.get("session"))
        await self.agent.prepare_session(session)
        self.bind(connection, session.id)
        await connection.send(
            "hello",
            session=session.to_dict(),
            history=self.agent.memory.replay(session.id),
            config=self.agent.config.to_dict(),
            commands=self.agent.commands.describe(),
            theme=self.theme,
        )

    async def on_prompt(self, connection: Connection, message: dict[str, Any]) -> None:
        text = str(message.get("text", "")).strip()
        if not text:
            await connection.fail("Prompt cannot be empty")
            return
        if connection.task is not None and not connection.task.done():
            await connection.fail("A run is already active")
            return
        session = self.agent.sessions.ensure(connection.session_id)
        self.bind(connection, session.id)
        try:
            attachments = self.store_attachments(session, message.get("attachments") or [])
        except (ValueError, OSError) as exc:
            await connection.fail(f"Attachment rejected: {exc}")
            return
        await self.hub.broadcast(
            self.channel(session.id),
            EventType.BLOCK_START,
            id=f"prompt-{time.time_ns()}",
            kind="prompt",
            role="user",
            text=text,
            attachments=attachments,
            complete=True,
        )
        connection.task = asyncio.create_task(
            self.run(connection, session, text, attachments)
        )

    async def on_command(self, connection: Connection, message: dict[str, Any]) -> None:
        text = str(message.get("text", ""))
        result = await self.agent.commands.invoke(
            text, session_id=connection.session_id, connection=connection
        )
        if result is None:
            await connection.fail("Not a command")
            return
        if isinstance(result, dict) and isinstance(result.get("session"), dict):
            self.bind(connection, str(result["session"]["id"]))
        await connection.send("command", command=text, result=result)

    async def on_cancel(self, connection: Connection, _message: dict[str, Any]) -> None:
        cancelled = await connection.cancel_task()
        await connection.send("cancelled", ok=cancelled)

    # -- helpers ------------------------------------------------------------

    def bind(self, connection: Connection, session_id: str) -> None:
        """Point a connection at a session channel, leaving the previous one."""
        if connection.session_id == session_id:
            self.hub.join(connection, self.channel(session_id))
            return
        if connection.session_id:
            self.hub.leave(connection, self.channel(connection.session_id))
        connection.session_id = session_id
        self.hub.join(connection, self.channel(session_id))

    def store_attachments(
        self, session: Session, attachments: list[dict[str, Any]]
    ) -> list[str]:
        """Persist client attachments inside the session workspace."""
        workspace = Workspace(session.workspace)
        saved: list[str] = []
        total = 0
        for attachment in attachments:
            raw = str(attachment.get("data", ""))
            payload = raw.split(",", 1)[1] if raw.startswith("data:") else raw
            try:
                blob = base64.b64decode(payload, validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"invalid encoding for {attachment.get('name')}") from exc
            total += len(blob)
            if total > MAX_ATTACHMENT_BYTES:
                raise ValueError("attachments exceed the size limit")
            path = workspace.resolve(f"attachments/{safe_name(attachment.get('name'))}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
            saved.append(workspace.relative(path))
        return saved

    async def run(
        self,
        connection: Connection,
        session: Session,
        text: str,
        attachments: list[str],
    ) -> None:
        try:
            await self.agent.run(
                self.template,
                session=session,
                input=text,
                attachments=attachments,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            await connection.fail(str(exc))
        finally:
            connection.task = None

    # -- http ---------------------------------------------------------------

    def rest(self, path: str) -> tuple[int, str] | None:
        """Read only REST endpoints. Returns ``(status, json)`` or ``None``."""
        if path == "/api/health":
            return 200, json.dumps({"ok": True, "name": self.agent.config.name})
        if path == "/api/config":
            return 200, json.dumps(self.agent.config.to_dict())
        if path == "/api/commands":
            return 200, json.dumps(self.agent.commands.describe())
        if path == "/api/theme":
            return 200, json.dumps(self.theme)
        if path == "/api/sessions":
            return 200, json.dumps([s.to_dict() for s in self.agent.sessions.list()])
        return None

    async def http(self, connection: Any, request: Any) -> Any:
        path = request.path.split("?", 1)[0]
        if path == "/ws":
            return None  # hand over to the websocket handler
        asset = self.ASSETS.get(path)
        if asset is not None:
            name, content_type = asset
            file = self.assets_root / name
            if not file.is_file():
                return connection.respond(404, "Not Found")
            response = connection.respond(200, file.read_text(encoding="utf-8"))
            response.headers["Content-Type"] = content_type
            response.headers["Cache-Control"] = "no-cache"
            return response
        rest = self.rest(path)
        if rest is not None:
            status, body = rest
            response = connection.respond(status, body)
            response.headers["Content-Type"] = "application/json"
            return response
        return connection.respond(404, "Not Found")

    async def serve(self) -> None:
        """Serve until cancelled."""
        from websockets.asyncio.server import serve as ws_serve

        if self.agent.renderer is not None:
            self.agent.renderer.banner(
                f"{self.agent.config.name} (web)",
                [
                    ("URL", f"http://{self.host}:{self.port}"),
                    ("Model", self.agent.config.model),
                    ("Vision Model", self.agent.config.vision_model or "(leader)"),
                    ("Theme", str(self.agent.config.theme or "(default)")),
                ],
            )
        async with ws_serve(
            self.hub.serve_connection,
            self.host,
            self.port,
            process_request=self.http,
            compression=None,
            max_size=MAX_MESSAGE_BYTES,
        ):
            self.agent.sessions.start_sweeper()
            try:
                await asyncio.Future()
            finally:
                await self.agent.sessions.stop_sweeper()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

PASSTHROUGH_TEMPLATE = "{{ config.input }}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the supported arguments: ``--input``, ``--repl``, ``--serve``, ``--config``."""
    parser = argparse.ArgumentParser(prog="agent", description=__doc__.splitlines()[0])
    parser.add_argument("--input", default="", help="a prompt file path or raw text")
    parser.add_argument(
        "--repl",
        action="store_true",
        help="chat in the terminal (the default with no arguments)",
    )
    parser.add_argument(
        "--serve", action="store_true", help="serve the web UI and websocket hub"
    )
    parser.add_argument(
        "--config",
        default="",
        help=f"a JSON or YAML config file (default: {STEM}.json|.yaml here or beside {STEM}.py)",
    )
    return parser.parse_args(argv)


def template_candidates(directories: Iterable[Path | str] | None = None) -> list[Path]:
    """Every template file that is looked at, in order of precedence."""
    return [
        directory / f"{STEM}_prompt.md" for directory in config_directories(directories)
    ]


def load_template(config: AgentConfig | None = None) -> str:
    """Resolve the template for the data driven application.

    Looks at `config.template` — which a config file or ``AGENT_TEMPLATE`` sets,
    as a path or as the template itself — then ``agent_prompt.md`` in the current
    directory and next to ``agent.py``, and finally falls back to passing the
    input through.
    """
    raw = (getattr(config, "template", "") or "").strip()
    raw = raw or os.environ.get(f"{ENV_PREFIX}TEMPLATE", "").strip()
    if raw:
        path = Path(raw).expanduser()
        with contextlib.suppress(OSError, ValueError):
            if path.is_file():
                return path.read_text(encoding="utf-8")
        return raw
    for default in template_candidates():
        if default.is_file():
            return default.read_text(encoding="utf-8")
    return PASSTHROUGH_TEMPLATE


def main(argv: list[str] | None = None) -> int:
    """Entry point of the data driven application."""
    return Agent.cli(None, argv)


if __name__ == "__main__":
    raise SystemExit(main())
