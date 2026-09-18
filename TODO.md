# agent — TODO

Everything still open, in one flat list, most important first. Nothing delivered
is recorded here: the reasoning behind what exists is in
[CHANGES.md](CHANGES.md), and the implementation is documented in
[GUIDE.md](GUIDE.md).

---

- [ ] **Authentication and authorization on the web layer.** The server binds
      without any identity: whoever reaches the port gets a session with shell,
      file and git tools and the configured credentials. It needs a token or
      session cookie on both the HTTP and websocket handshake, ownership of
      sessions per principal, an origin check on the socket, and rate limiting
      before it is exposed anywhere but localhost.

- [ ] **Concurrency limits and fairness.** Nothing bounds how many sessions,
      workspaces, subprocesses or runs exist at once; a handful of clients can
      exhaust the disk or the CPU of the host. Cap concurrent runs and live
      sessions, bound total workspace size, and apply backpressure on the hub
      instead of accepting work that cannot be served.

- [ ] **Stronger tool sandboxing.** The workspace confines paths, but
      `run_shell_command` inherits the environment, the network and the whole
      filesystem of the host, and `AGENT_SHELL_ENABLED` is the only control.
      Offer an allow/deny list of commands, a scrubbed environment, optional
      network isolation and a container or namespace backend for the cases where
      a real boundary is required.

- [ ] **Observability.** Diagnostics are `print` calls and a swallowed
      subscriber exception; SDK tracing is disabled outright. Add structured
      logging over the event bus with correlation by session and run, counters
      and timings for runs, tools and model calls, and an opt-in exporter so a
      deployment can see what the harness is doing.

- [ ] **Tests for the web layer and the client.** The unit tests stop at the
      protocol codec and `agent_e2e.py` only photographs a happy path: the hub
      handlers, attachment limits, cancellation, reconnection and most of
      `agent_ui.js` are still unasserted, which is most of the surface a user
      actually touches. Add server tests over a real websocket, and assertions
      (plus cancellation, errors and reconnection) to the browser pass.

- [ ] **Repl ergonomics.** The terminal reads plain lines: there is no history
      across restarts, no editing beyond what the platform's `input()` gives,
      no command completion, no multi-line composition and no way to attach a
      file the way the web client can. Add a history file, tab completion over
      the command registry and the workspace, an explicit multi-line mode and an
      `/attach` command.

- [ ] **Forge portability and a richer git workflow.** Pull requests are GitHub
      REST only, clones are shallow by default and the checkout can only commit,
      push and open a pull request. Add GitLab and Gitea (merge requests), let
      the model fetch, diff, rebase and update an existing pull request, handle
      conflicts and protected branches explicitly, and report what was rejected
      instead of failing the run.
