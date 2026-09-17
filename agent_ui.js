/* Agent chat client.
 *
 * Subsystems: Markdown (safe rendering), Media (inline preview), Blocks
 * (content subscribers, updated concurrently and out of order), Theme (VS Code
 * theme subset), Commands (slash commands) and Socket (hub client).
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const el = {
    app: $("app"),
    brand: $("brand"),
    status: $("status-text"),
    session: $("session"),
    discussion: $("discussion"),
    empty: $("empty"),
    form: $("form"),
    input: $("input"),
    send: $("send"),
    stop: $("stop"),
    file: $("file"),
    attachments: $("attachments"),
    hints: $("hints"),
  };

  // ---------------------------------------------------------------- markdown

  const Markdown = (() => {
    const escape = (text) =>
      text.replace(
        /[&<>"']/g,
        (c) =>
          ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
      );

    const safeUrl = (raw) => {
      const url = (raw || "").trim();
      if (/^(https?:|mailto:|\/|\.{0,2}\/)/i.test(url)) return url;
      if (/^data:(image|video|audio)\//i.test(url)) return url;
      return "";
    };

    const inline = (text) =>
      escape(text)
        .replace(/`([^`]+)`/g, (_m, code) => `<code>${code}</code>`)
        .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, url) => Media.tag(url, alt) || m)
        .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, url) => {
          const href = safeUrl(url);
          return href
            ? `<a href="${escape(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`
            : m;
        })
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|\W)\*([^*]+)\*/g, "$1<em>$2</em>")
        .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, (m, pre, url) => {
          const media = Media.tag(url, "");
          return media
            ? pre + media
            : `${pre}<a href="${escape(url)}" target="_blank" rel="noopener noreferrer">${escape(url)}</a>`;
        });

    /** Render a markdown subset to HTML. Input is escaped before anything else. */
    const render = (source) => {
      const lines = String(source || "").split("\n");
      const out = [];
      let list = null;
      let fence = null;
      let paragraph = [];

      const flushParagraph = () => {
        if (paragraph.length) {
          out.push(`<p>${inline(paragraph.join("\n"))}</p>`);
          paragraph = [];
        }
      };
      const flushList = () => {
        if (list) {
          out.push(`</${list}>`);
          list = null;
        }
      };

      for (const line of lines) {
        const fenceMatch = /^\s*```(.*)$/.exec(line);
        if (fenceMatch) {
          if (fence === null) {
            flushParagraph();
            flushList();
            fence = [];
          } else {
            out.push(`<pre><code>${escape(fence.join("\n"))}</code></pre>`);
            fence = null;
          }
          continue;
        }
        if (fence !== null) {
          fence.push(line);
          continue;
        }
        if (!line.trim()) {
          flushParagraph();
          flushList();
          continue;
        }
        const heading = /^(#{1,6})\s+(.*)$/.exec(line);
        if (heading) {
          flushParagraph();
          flushList();
          const level = heading[1].length;
          out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
          continue;
        }
        if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
          flushParagraph();
          flushList();
          out.push("<hr />");
          continue;
        }
        const quote = /^\s*>\s?(.*)$/.exec(line);
        if (quote) {
          flushParagraph();
          flushList();
          out.push(`<blockquote>${inline(quote[1])}</blockquote>`);
          continue;
        }
        const item = /^\s*(?:([-*+])|(\d+)[.)])\s+(.*)$/.exec(line);
        if (item) {
          flushParagraph();
          const kind = item[1] ? "ul" : "ol";
          if (list !== kind) {
            flushList();
            out.push(`<${kind}>`);
            list = kind;
          }
          out.push(`<li>${inline(item[3])}</li>`);
          continue;
        }
        paragraph.push(line);
      }
      if (fence !== null) out.push(`<pre><code>${escape(fence.join("\n"))}</code></pre>`);
      flushParagraph();
      flushList();
      return out.join("\n");
    };

    return { render, escape, safeUrl };
  })();

  // ------------------------------------------------------------------ media

  const Media = (() => {
    const KINDS = {
      image: /\.(png|jpe?g|gif|webp|bmp|svg)(\?|#|$)|^data:image\//i,
      video: /\.(mp4|webm|ogv|mov)(\?|#|$)|^data:video\//i,
      audio: /\.(mp3|wav|ogg|m4a|flac)(\?|#|$)|^data:audio\//i,
    };

    const kindOf = (url) =>
      Object.keys(KINDS).find((kind) => KINDS[kind].test(url)) || "";

    /** Return an inline media tag for a URL, or "" when it is not media. */
    const tag = (rawUrl, alt) => {
      const url = Markdown.safeUrl(rawUrl);
      if (!url) return "";
      const src = Markdown.escape(url);
      switch (kindOf(url)) {
        case "image":
          return `<img src="${src}" alt="${Markdown.escape(alt || "")}" loading="lazy" />`;
        case "video":
          return `<video src="${src}" controls playsinline preload="metadata"></video>`;
        case "audio":
          return `<audio src="${src}" controls preload="metadata"></audio>`;
        default:
          return "";
      }
    };

    return { tag, kindOf };
  })();

  // ------------------------------------------------------------------ theme

  const Theme = {
    /** Apply a CSS variable map (already mapped from a VS Code theme). */
    apply(vars) {
      if (!vars) return;
      Object.entries(vars).forEach(([name, value]) => {
        if (name === "--theme-type") {
          document.documentElement.dataset.themeType = value;
          return;
        }
        if (/^--[\w-]+$/.test(name) && typeof value === "string") {
          document.documentElement.style.setProperty(name, value);
        }
      });
    },
    /** Map a raw VS Code theme file onto CSS variables (client side loading). */
    fromVsCode(theme) {
      const keys = {
        "editor.background": "--bg",
        "editor.foreground": "--fg",
        "sideBar.background": "--bg-soft",
        "editorWidget.background": "--bg-raised",
        "input.background": "--input-bg",
        "input.foreground": "--input-fg",
        "input.border": "--input-border",
        "button.background": "--accent",
        "button.foreground": "--accent-fg",
        focusBorder: "--focus",
        "panel.border": "--border",
        descriptionForeground: "--muted",
        errorForeground: "--error",
        "textLink.foreground": "--link",
        "textCodeBlock.background": "--code-bg",
        "badge.background": "--badge-bg",
        "badge.foreground": "--badge-fg",
        "scrollbarSlider.background": "--scroll",
      };
      const colors = (theme && theme.colors) || {};
      const vars = {};
      Object.entries(keys).forEach(([key, name]) => {
        if (colors[key]) vars[name] = colors[key];
      });
      if (theme && theme.type) vars["--theme-type"] = theme.type;
      return vars;
    },
  };

  // ----------------------------------------------------------------- blocks

  /** A block subscribes to content and re-renders itself when it changes. */
  class Block {
    constructor(id, kind, role) {
      this.id = id;
      this.kind = kind || "output";
      this.text = "";
      this.streaming = true;
      this.node = document.createElement("article");
      this.node.className = "block";
      this.node.dataset.kind = this.kind;
      this.node.dataset.streaming = "true";
      this.node.innerHTML =
        `<header class="block-head"><span class="label"></span>` +
        `<span class="time"></span></header><div class="block-body"></div>`;
      this.label(role === "user" ? "you" : this.kind);
      this.node.querySelector(".time").textContent = new Date().toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
      });
      this.body = this.node.querySelector(".block-body");
    }

    label(text) {
      this.node.querySelector(".label").textContent = text;
    }

    append(text) {
      if (!text) return;
      this.text += text;
      this.render();
    }

    set(text) {
      this.text = text || "";
      this.render();
    }

    render() {
      // A tool call is a transcript of what ran, never markdown to interpret.
      if (this.kind === "tool") this.body.textContent = this.text;
      else this.body.innerHTML = Markdown.render(this.text);
    }

    end() {
      this.streaming = false;
      this.node.dataset.streaming = "false";
      this.render();
    }
  }

  const Blocks = {
    map: new Map(),
    /** Get or create a block: updates may arrive concurrently, out of order. */
    ensure(id, kind, role) {
      let block = this.map.get(id);
      if (!block) {
        block = new Block(id, kind, role);
        this.map.set(id, block);
        el.empty.hidden = true;
        el.discussion.appendChild(block.node);
        this.scroll();
      }
      return block;
    },
    scroll() {
      const near =
        el.discussion.scrollHeight - el.discussion.scrollTop - el.discussion.clientHeight;
      if (near < 160) el.discussion.scrollTop = el.discussion.scrollHeight;
    },
    clear() {
      this.map.clear();
      [...el.discussion.querySelectorAll(".block")].forEach((n) => n.remove());
      el.empty.hidden = false;
    },
    note(kind, text) {
      const block = this.ensure(`note-${Date.now()}-${Math.random()}`, kind, "system");
      block.set(text);
      block.end();
      this.scroll();
    },
  };

  // --------------------------------------------------------------- commands

  const Commands = {
    server: [],
    local: {
      clear: {
        description: "Clear the discussion",
        run: () => Blocks.clear(),
      },
      theme: {
        description: "Load a VS Code theme file",
        run: () => Commands.pickTheme(),
      },
    },
    all() {
      return [
        ...this.server,
        ...Object.entries(this.local).map(([name, c]) => ({
          name,
          description: c.description,
        })),
      ].sort((a, b) => a.name.localeCompare(b.name));
    },
    parse(text) {
      const match = /^\/([a-zA-Z][\w-]*)\s*(.*)$/s.exec((text || "").trim());
      return match ? { name: match[1].toLowerCase(), args: match[2].trim() } : null;
    },
    pickTheme() {
      const picker = document.createElement("input");
      picker.type = "file";
      picker.accept = ".json";
      picker.addEventListener("change", async () => {
        const file = picker.files && picker.files[0];
        if (!file) return;
        try {
          Theme.apply(Theme.fromVsCode(JSON.parse(await file.text())));
          Blocks.note("log", `Theme applied: ${file.name}`);
        } catch (err) {
          Blocks.note("error", `Invalid theme file: ${err.message}`);
        }
      });
      picker.click();
    },
    /** Returns true when handled locally, otherwise the server handles it. */
    run(text) {
      const parsed = this.parse(text);
      if (!parsed) return false;
      const local = this.local[parsed.name];
      if (!local) return false;
      local.run(parsed.args);
      return true;
    },
  };

  // ----------------------------------------------------------- attachments

  const Attachments = {
    items: [],
    async add(files) {
      for (const file of files) {
        const data = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onerror = () => reject(reader.error);
          reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
          reader.readAsDataURL(file);
        });
        this.items.push({ name: file.name, type: file.type, data });
      }
      this.render();
    },
    remove(name) {
      this.items = this.items.filter((item) => item.name !== name);
      this.render();
    },
    take() {
      const items = this.items;
      this.items = [];
      this.render();
      return items;
    },
    render() {
      el.attachments.hidden = this.items.length === 0;
      el.attachments.replaceChildren(
        ...this.items.map((item) => {
          const chip = document.createElement("span");
          chip.className = "chip";
          const label = document.createElement("span");
          label.textContent = item.name;
          const close = document.createElement("button");
          close.type = "button";
          close.textContent = "×";
          close.addEventListener("click", () => this.remove(item.name));
          chip.append(label, close);
          return chip;
        }),
      );
    },
  };

  // ----------------------------------------------------------------- socket

  const Socket = {
    ws: null,
    session: null,
    retry: 0,
    connect() {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      this.ws = new WebSocket(`${scheme}://${location.host}/ws`);
      this.ws.addEventListener("open", () => {
        this.retry = 0;
        this.setState("online");
        this.send({ type: "hello", session: this.session });
      });
      this.ws.addEventListener("close", () => {
        this.setState("offline");
        this.retry = Math.min(this.retry + 1, 6);
        setTimeout(() => this.connect(), 250 * 2 ** this.retry);
      });
      this.ws.addEventListener("message", (event) => {
        let message;
        try {
          message = JSON.parse(event.data);
        } catch {
          return;
        }
        this.receive(message);
      });
    },
    setState(state) {
      el.app.dataset.state = state;
      el.status.textContent = state === "online" ? "connected" : "disconnected";
      const offline = state !== "online";
      el.input.disabled = offline;
      el.send.disabled = offline;
      if (!offline) el.input.focus({ preventScroll: true });
    },
    send(message) {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify(message));
        return true;
      }
      Blocks.note("error", "Not connected.");
      return false;
    },
    busy(active) {
      el.stop.hidden = !active;
      el.send.hidden = active;
    },
    receive(message) {
      switch (message.type) {
        case "hello":
          this.session = message.session && message.session.id;
          el.session.textContent = this.session ? `#${this.session}` : "";
          el.brand.textContent = (message.config && message.config.name) || "agent";
          Commands.server = message.commands || [];
          Theme.apply(message.theme);
          this.replay(message.history);
          break;
        case "block.start":
          Blocks.ensure(message.id, message.kind, message.role).set(message.text || "");
          if (message.attachments && message.attachments.length) {
            Blocks.map
              .get(message.id)
              .append(
                `\n\n${message.attachments.map((name) => `- \`${name}\``).join("\n")}`,
              );
          }
          if (message.complete) Blocks.map.get(message.id).end();
          if (message.kind === "prompt") this.busy(true);
          break;
        case "block.delta":
          Blocks.ensure(message.id, message.kind).append(message.text || "");
          Blocks.scroll();
          break;
        case "tool.start":
        case "tool.end": {
          const tool = Blocks.ensure(message.id, "tool");
          tool.label(message.name || "tool");
          tool.set(message.text || "");
          if (message.type === "tool.end") {
            tool.node.dataset.status = message.ok === false ? "failed" : "ok";
            tool.end();
          }
          Blocks.scroll();
          break;
        }
        case "block.end":
          Blocks.ensure(message.id, message.kind).end();
          break;
        case "model.retry":
          Blocks.note(
            "error",
            `${message.role || "leader"} model ${message.error_kind || "error"}: ` +
              `retry ${(Number(message.attempt) || 1) + 1}/${Number(message.attempts) || 1} ` +
              `in ${(Number(message.delay) || 0).toFixed(1)}s`,
          );
          break;
        case "agent.end":
          this.busy(false);
          if (message.error) Blocks.note("error", message.error);
          break;
        case "agent.error":
          Blocks.note("error", String(message.error || "unknown error"));
          this.busy(false);
          break;
        case "command":
          this.applyCommand(message.result);
          break;
        case "cancelled":
          this.busy(false);
          break;
        case "error":
          Blocks.note("error", String(message.message || "error"));
          this.busy(false);
          break;
        default:
          break;
      }
    },
    /** Render the transcript the server remembers, replacing what is shown. */
    replay(history) {
      if (!Array.isArray(history) || !history.length) return;
      Blocks.clear();
      history.forEach((turn, index) => {
        const block = Blocks.ensure(
          `memory-${index}`,
          turn.kind || "output",
          turn.role || "assistant",
        );
        block.set(turn.text || "");
        block.end();
      });
      Blocks.scroll();
    },
    applyCommand(result) {
      if (!result) return;
      if (result.session && result.session.id) {
        this.session = result.session.id;
        el.session.textContent = `#${this.session}`;
      }
      if (result.commands) {
        Blocks.note(
          "log",
          Commands.all()
            .map((c) => `- \`/${c.name}\` — ${c.description}`)
            .join("\n"),
        );
        return;
      }
      if (result.sessions) {
        const list = result.sessions.length
          ? result.sessions.map((s) => `- \`${s.id}\``).join("\n")
          : "_no live sessions_";
        Blocks.note("log", list);
        return;
      }
      Blocks.note(result.ok ? "log" : "error", result.message || "done");
    },
  };

  // -------------------------------------------------------------- composer

  const resize = () => {
    el.input.style.height = "auto";
    el.input.style.height = `${Math.min(el.input.scrollHeight, 144)}px`;
  };

  const hints = {
    index: 0,
    items: [],
    show(prefix) {
      this.items = Commands.all().filter((c) => c.name.startsWith(prefix));
      this.index = 0;
      el.hints.hidden = this.items.length === 0;
      el.hints.replaceChildren(
        ...this.items.map((command, i) => {
          const row = document.createElement("div");
          row.className = "hint";
          row.setAttribute("aria-selected", String(i === this.index));
          const name = document.createElement("b");
          name.textContent = `/${command.name}`;
          const description = document.createElement("em");
          description.textContent = command.description;
          row.append(name, description);
          row.addEventListener("mousedown", (event) => {
            event.preventDefault();
            this.pick(i);
          });
          el.hints.appendChild(row);
          return row;
        }),
      );
    },
    move(delta) {
      if (!this.items.length) return;
      this.index = (this.index + delta + this.items.length) % this.items.length;
      [...el.hints.children].forEach((row, i) =>
        row.setAttribute("aria-selected", String(i === this.index)),
      );
    },
    pick(index) {
      const command = this.items[index === undefined ? this.index : index];
      if (!command) return;
      el.input.value = `/${command.name} `;
      this.hide();
      el.input.focus();
    },
    hide() {
      this.items = [];
      el.hints.hidden = true;
      el.hints.replaceChildren();
    },
  };

  const submit = () => {
    const text = el.input.value.trim();
    if (!text) return;
    if (Commands.run(text)) {
      el.input.value = "";
      resize();
      hints.hide();
      return;
    }
    const message = Commands.parse(text)
      ? { type: "command", text }
      : { type: "prompt", text, attachments: Attachments.take() };
    if (Socket.send(message)) {
      el.input.value = "";
      resize();
      hints.hide();
    }
  };

  el.form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit();
  });

  el.input.addEventListener("input", () => {
    resize();
    const match = /^\/([a-zA-Z-]*)$/.exec(el.input.value);
    if (match) hints.show(match[1].toLowerCase());
    else hints.hide();
  });

  el.input.addEventListener("keydown", (event) => {
    if (!el.hints.hidden) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        hints.move(event.key === "ArrowDown" ? 1 : -1);
        return;
      }
      if (event.key === "Tab" || (event.key === "Enter" && hints.items.length)) {
        event.preventDefault();
        hints.pick();
        return;
      }
      if (event.key === "Escape") hints.hide();
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  });

  el.file.addEventListener("change", async () => {
    await Attachments.add([...el.file.files]);
    el.file.value = "";
  });

  el.stop.addEventListener("click", () => Socket.send({ type: "cancel" }));

  Socket.connect();
})();
