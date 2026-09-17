#!/usr/bin/env python3
"""End to end capture of the web UI.

Starts the server with a scripted agent behind it, drives the chat page with
Playwright and writes the screenshots [`GUIDE.md`](GUIDE.md) shows into
`assets/`. Three frames are taken per device — the page after launch, the page
during a request and the page after the response — on a desktop display and on
a phone in portrait and in landscape.

    python agent_e2e.py                       # every device, into assets/
    python agent_e2e.py --device phone-portrait --out /tmp/shots

Nothing here reaches a provider: `ScriptedAgent` replaces the model stream with
a fixed answer that pauses halfway through, so every capture is offline, free
and the same picture every time.

Requires `playwright` and a Chromium build (`pip install -e .[e2e]` then
`playwright install chromium`).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

import agent as A

ROOT = Path(__file__).resolve().parent

#: Where the images GUIDE.md links to are written.
ASSETS = ROOT / "assets"

#: Chromium builds that are looked at when Playwright cannot find its own.
BROWSER_CANDIDATES = (
    "/opt/pw-browsers/chromium",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
)


# ---------------------------------------------------------------------------
# Script
#
# What the captured conversation says. The answer is split in two so the run
# can stop between the halves and be photographed mid stream.
# ---------------------------------------------------------------------------

PROMPT = "Read agent.py and summarise what the harness does."

TOOL_NAME = "read_file"
TOOL_ARGUMENTS = {"path": "agent.py"}
TOOL_RESULT = "agent.py — 4656 lines, 16 sections"

ANSWER_HEAD = """\
`agent.py` is one flat module: the agentic loop, the batteries around it and the application shell, in that order.

- **Engine** — config, events, prompt, tools and models. `Engine.run()` streams the model and publishes the agent, block and tool events this page listens to.
"""

ANSWER_TAIL = """\
- **Agent** — the engine plus storage, sessions, memory, git checkouts, slash commands, the console renderer, the repl and the web layer.
- **Web** — a read only REST surface and a websocket hub that pushes every block to this page while it is being written.

Run `agent --serve` for this UI and `agent --repl` for the terminal.
"""


def chunks(text: str, words: int = 3) -> Iterator[str]:
    """Cut text into the small pieces a provider streams it in."""
    parts = re.findall(r"\S+\s*", text)
    for start in range(0, len(parts), words):
        yield "".join(parts[start : start + words])


# ---------------------------------------------------------------------------
# Scripted agent
# ---------------------------------------------------------------------------


class Gate:
    """A rendezvous between the scripted run and the browser driving it.

    The run stops in the middle of its answer and stays there until the capture
    releases it, so the "during request" frame is a decision rather than a race
    with the stream.
    """

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.resumed = threading.Event()

    def arm(self) -> None:
        """Ready the gate for the next run."""
        self.reached.clear()
        self.resumed.clear()

    async def hold(self) -> None:
        """Called by the run: announce the pause and wait to be let go."""
        self.reached.set()
        while not self.resumed.is_set():
            await asyncio.sleep(0.02)

    def wait(self, timeout: float = 30.0) -> bool:
        """Called by the capture: block until the run is paused."""
        return self.reached.wait(timeout)

    def release(self) -> None:
        """Called by the capture: let the rest of the answer through."""
        self.resumed.set()


class ScriptedAgent(A.Agent):
    """An agent whose model is a script: no provider, no key, no variance.

    Only the three seams that reach outward are replaced — the tool list, the
    SDK agent and the stream — so everything the capture photographs (sessions,
    memory, the event bridge, the hub, the page) is the real thing.
    """

    def __init__(
        self,
        *args: Any,
        gate: Gate | None = None,
        pace: float = 0.03,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.gate = gate or Gate()
        self.pace = pace

    def build_tools(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    def build_sdk_agent(self, config: A.AgentConfig, tools: list[Any]) -> Any:
        return {"config": config, "tools": tools}

    async def stream(
        self,
        sdk_agent: Any,
        model_input: str | list[dict[str, Any]],
        config: A.AgentConfig,
        result: A.RunResult,
    ) -> A.RunResult:
        """Publish the scripted tool call and answer as the real stream would."""
        await self.call_tool(result)
        block = A.Block(
            id=f"{result.session_id}-answer", kind="output", session_id=result.session_id
        )
        result.blocks.append(block)
        await self.events.publish(
            A.EventType.BLOCK_START,
            session_id=result.session_id,
            id=block.id,
            kind=block.kind,
            role=block.role,
        )
        await self.write(block, ANSWER_HEAD)
        await self.gate.hold()
        await self.write(block, ANSWER_TAIL)
        await self.events.publish(
            A.EventType.BLOCK_END,
            session_id=result.session_id,
            id=block.id,
            kind=block.kind,
            text=block.text,
        )
        result.output = block.text
        return result

    async def call_tool(self, result: A.RunResult) -> None:
        """One tool call, started and finished, so the page shows a tool block."""
        call = A.ToolCall(
            id=f"{result.session_id}-tool",
            name=TOOL_NAME,
            call_id="call-1",
            arguments=TOOL_ARGUMENTS,
        )
        result.tools.append(call)
        await self.events.publish(
            A.EventType.TOOL_START,
            session_id=result.session_id,
            id=call.id,
            kind="tool",
            name=call.name,
            call_id=call.call_id,
            arguments=call.arguments,
            text=call.report(),
        )
        await asyncio.sleep(self.pace * 4)
        call.finish(TOOL_RESULT)
        await self.events.publish(
            A.EventType.TOOL_END,
            session_id=result.session_id,
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

    async def write(self, block: A.Block, text: str) -> None:
        """Send text the way a provider does: a few words at a time."""
        for chunk in chunks(text):
            block.text += chunk
            await self.events.publish(
                A.EventType.BLOCK_DELTA,
                session_id=block.session_id,
                id=block.id,
                kind=block.kind,
                text=chunk,
            )
            await asyncio.sleep(self.pace)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


def free_port(host: str = "127.0.0.1") -> int:
    """A port the server can have."""
    with socket.socket() as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


class Server:
    """The real web server on a background thread, with a scripted agent behind it."""

    def __init__(
        self,
        gate: Gate,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        pace: float = 0.03,
    ) -> None:
        self.gate = gate
        self.host = host
        self.port = port or free_port(host)
        self.pace = pace
        self.workspaces = Path(tempfile.mkdtemp(prefix="agent-e2e-"))
        self.thread = threading.Thread(target=self._main, name="agent-e2e", daemon=True)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task[Any] | None = None
        self.error: BaseException | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 30.0) -> "Server":
        """Start the thread and wait until the page is being served."""
        self.thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.error is not None:
                raise RuntimeError(f"the server failed to start: {self.error}")
            with contextlib.suppress(OSError, urllib.error.URLError):
                with urllib.request.urlopen(f"{self.url}/api/health", timeout=1) as body:
                    if json.load(body).get("ok"):
                        return self
            time.sleep(0.1)
        raise TimeoutError(f"the server did not answer on {self.url}")

    def stop(self, timeout: float = 10.0) -> None:
        """Cancel the server and wait for the thread to unwind."""
        if self.loop is not None and self.task is not None:
            self.loop.call_soon_threadsafe(self.task.cancel)
        self.thread.join(timeout)

    def _main(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:  # pragma: no cover - reported to the caller
            self.error = exc

    async def _serve(self) -> None:
        agent = ScriptedAgent(
            gate=self.gate,
            pace=self.pace,
            console=False,
            env=False,
            config_file=False,
            host=self.host,
            port=self.port,
            workspace_root=self.workspaces,
            session_sweep_interval=0,
        )
        server = A.WebServer(agent, host=self.host, port=self.port)
        self.loop = asyncio.get_running_loop()
        self.task = asyncio.ensure_future(server.serve())
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        finally:
            await agent.aclose()


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

PHONE_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


@dataclass(frozen=True)
class Device:
    """A viewport to photograph the page in."""

    name: str
    width: int
    height: int
    scale: float = 1.0
    mobile: bool = False
    user_agent: str = ""

    def context(self) -> dict[str, Any]:
        """The Playwright context options for this device."""
        options: dict[str, Any] = {
            "viewport": {"width": self.width, "height": self.height},
            "device_scale_factor": self.scale,
            "is_mobile": self.mobile,
            "has_touch": self.mobile,
            "color_scheme": "dark",
        }
        if self.user_agent:
            options["user_agent"] = self.user_agent
        return options


#: An average desktop window and an average phone, held both ways.
DEVICES = (
    Device("desktop", 1440, 900),
    Device("phone-portrait", 390, 844, scale=2.0, mobile=True, user_agent=PHONE_AGENT),
    Device("phone-landscape", 844, 390, scale=2.0, mobile=True, user_agent=PHONE_AGENT),
)

#: The three moments of a conversation, in the order they are captured.
MOMENTS = ("launch", "request", "response")


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def find_browser(explicit: str = "") -> str:
    """The Chromium executable to drive, or an empty string for Playwright's own."""
    for candidate in (explicit, os.environ.get("AGENT_E2E_BROWSER", "")):
        if candidate:
            if not Path(candidate).exists():
                raise FileNotFoundError(f"no such browser: {candidate}")
            return candidate
    return ""


def fallback_browser() -> str:
    """A Chromium on this machine, when Playwright has not installed one."""
    for candidate in BROWSER_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return ""


def launch(playwright: Any, browser_path: str = "") -> Any:
    """Launch Chromium, falling back to a build already on the machine."""
    arguments = ["--no-sandbox", "--hide-scrollbars", "--force-color-profile=srgb"]
    if browser_path:
        return playwright.chromium.launch(executable_path=browser_path, args=arguments)
    try:
        return playwright.chromium.launch(args=arguments)
    except Exception:
        spare = fallback_browser()
        if not spare:
            raise
        return playwright.chromium.launch(executable_path=spare, args=arguments)


def capture(browser: Any, device: Device, url: str, gate: Gate, out: Path) -> list[Path]:
    """Drive one conversation on one device, photographing the three moments."""
    written: list[Path] = []
    context = browser.new_context(**device.context())
    page = context.new_page()
    try:
        page.goto(url, wait_until="load")
        page.wait_for_selector('#app[data-state="online"]')
        page.wait_for_function("() => !document.getElementById('input').disabled")
        written.append(shoot(page, out, device, "launch"))

        gate.arm()
        page.fill("#input", PROMPT)
        page.click("#send")
        if not gate.wait():
            raise TimeoutError("the run never reached the hold point")
        page.wait_for_function("() => !document.getElementById('stop').hidden")
        page.wait_for_function(
            "() => document.querySelector('.block[data-kind=\"output\"]"
            "[data-streaming=\"true\"]')"
        )
        settle(page)
        written.append(shoot(page, out, device, "request"))

        gate.release()
        page.wait_for_function("() => document.getElementById('stop').hidden")
        page.wait_for_function(
            "() => !document.querySelector('.block[data-streaming=\"true\"]')"
        )
        settle(page)
        written.append(shoot(page, out, device, "response"))
    finally:
        context.close()
    return written


def settle(page: Any, milliseconds: int = 250) -> None:
    """Let the last paint land before the shutter."""
    page.wait_for_timeout(milliseconds)


def shoot(page: Any, out: Path, device: Device, moment: str) -> Path:
    """Write one viewport screenshot and return where it went."""
    path = out / f"{device.name}-{moment}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path))
    return path


def run(devices: Iterable[Device], out: Path, args: argparse.Namespace) -> list[Path]:
    """Serve the page, capture every device, and return the images written."""
    from playwright.sync_api import sync_playwright

    gate = Gate()
    server = Server(gate, host=args.host, port=args.port, pace=args.pace).start()
    written: list[Path] = []
    try:
        with sync_playwright() as playwright:
            browser = launch(playwright, find_browser(args.browser))
            try:
                for device in devices:
                    print(f"[e2e] {device.name} {device.width}x{device.height}")
                    written.extend(capture(browser, device, server.url, gate, out))
            finally:
                browser.close()
    finally:
        server.stop()
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the supported arguments."""
    parser = argparse.ArgumentParser(
        prog="agent_e2e", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--out", default=str(ASSETS), help=f"where the images go (default: {ASSETS})"
    )
    parser.add_argument(
        "--device",
        action="append",
        choices=[device.name for device in DEVICES],
        help="capture only this device (repeatable, default: all of them)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="the address to serve on")
    parser.add_argument(
        "--port", type=int, default=0, help="the port to serve on (0 picks a free one)"
    )
    parser.add_argument(
        "--pace", type=float, default=0.03, help="seconds between streamed chunks"
    )
    parser.add_argument(
        "--browser",
        default="",
        help="a Chromium executable (default: the one Playwright installed)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point: capture every device and report what was written."""
    args = parse_args(argv)
    wanted = set(args.device or [device.name for device in DEVICES])
    devices = [device for device in DEVICES if device.name in wanted]
    out = Path(args.out).expanduser().resolve()
    try:
        written = run(devices, out, args)
    except Exception as exc:
        print(f"[e2e] failed: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(f"[e2e] wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
