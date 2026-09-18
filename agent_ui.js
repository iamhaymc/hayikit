/* Agent chat client.
 *
 * Subsystems: Icons (inline SVG set), Markdown (safe rendering), Media (inline
 * preview), Clipboard (copy with an http fallback), Blocks (content
 * subscribers, updated concurrently and out of order), Theme (VS Code theme
 * subset), Commands (slash commands) and Socket (hub client).
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

  // ------------------------------------------------------------------ icons

  /** A small stroke icon set, inline with the code: no font, no network. */
  const Icons = {
    paths: {
      "chevron-down": '<path d="m6 9 6 6 6-6"/>',
      copy:
        '<rect x="9" y="9" width="13" height="13" rx="2"/>' +
        '<path d="M5 15a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2"/>',
      check: '<path d="M20 6 9 17l-5-5"/>',
      close: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
      user:
        '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/>' +
        '<circle cx="12" cy="7" r="4"/>',
      bot:
        '<path d="M12 8V4H8"/><rect x="4" y="8" width="16" height="12" rx="2"/>' +
        '<path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/>',
      sparkles:
        '<path d="m12 3 1.9 4.6 4.6 1.9-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9Z"/>' +
        '<path d="m18 15.4.8 1.8 1.8.8-1.8.8-.8 1.8-.8-1.8-1.8-.8 1.8-.8Z"/>',
      wrench:
        '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.8-3.8a6 6 0 0 1-8 7.9l-6.9 6.9a2.1 2.1 0 0 1-3-3l6.9-6.9a6 6 0 0 1 7.9-8l-3.7 3.9z"/>',
      info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
      alert:
        '<path d="m21.7 18-8-14a2 2 0 0 0-3.4 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3Z"/>' +
        '<path d="M12 9v4"/><path d="M12 17h.01"/>',
    },
    /** Icon markup for a static template. The set is ours, so it is safe HTML. */
    markup(name, className) {
      const body = this.paths[name] || this.paths.info;
      return (
        `<svg class="${className || "icon"}" viewBox="0 0 24 24" ` +
        `aria-hidden="true" focusable="false">${body}</svg>`
      );
    },
  };

  /** The icon that stands for each kind of block. */
  const KIND_ICONS = {
    prompt: "user",
    output: "bot",
    reasoning: "sparkles",
    tool: "wrench",
    log: "info",
    error: "alert",
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

    //: Stands in for a code span while the rest of the inline markup is applied.
    const SPAN = "\u0000";

    /** Inline markup. Code spans are lifted out first so nothing rewrites them. */
    const inline = (text) => {
      const spans = [];
      const lifted = String(text).replace(/(`+)([\s\S]*?)\1/g, (_m, _t, code) => {
        spans.push(code.replace(/^ | $/g, ""));
        return `${SPAN}${spans.length - 1}${SPAN}`;
      });
      const html = escape(lifted)
        .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, url) => Media.tag(url, alt) || m)
        .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, url) => {
          const href = safeUrl(url);
          return href
            ? `<a href="${escape(href)}" target="_blank" rel="noopener noreferrer">${label}</a>`
            : m;
        })
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/__([^_]+)__/g, "<strong>$1</strong>")
        .replace(/~~([^~]+)~~/g, "<del>$1</del>")
        .replace(/(^|\W)\*([^*]+)\*/g, "$1<em>$2</em>")
        .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, (m, pre, url) => {
          const media = Media.tag(url, "");
          return media
            ? pre + media
            : `${pre}<a href="${escape(url)}" target="_blank" rel="noopener noreferrer">${escape(url)}</a>`;
        });
      return html.replace(
        /\u0000(\d+)\u0000/g,
        (_m, index) => `<code>${escape(spans[Number(index)])}</code>`,
      );
    };

    const ALIGNMENTS = { ":-": "left", "-:": "right", "::": "center" };

    /** The alignment row of a pipe table, or null when the line is not one. */
    const alignments = (line) => {
      if (!/\|/.test(line) || !/^[\s|:-]+$/.test(line)) return null;
      const cells = split(line);
      if (!cells.length || !cells.every((cell) => /^:?-{1,}:?$/.test(cell.trim())))
        return null;
      return cells.map((cell) => {
        const text = cell.trim();
        const edges = `${text.startsWith(":") ? ":" : "-"}${text.endsWith(":") ? ":" : "-"}`;
        return ALIGNMENTS[edges] || "";
      });
    };

    /** The cells of one table row, without the outer pipes. */
    const split = (line) =>
      line
        .trim()
        .replace(/^\|/, "")
        .replace(/\|$/, "")
        .split(/(?<!\\)\|/)
        .map((cell) => cell.replace(/\\\|/g, "|"));

    const cell = (tag, text, align) =>
      `<${tag}${align ? ` style="text-align:${align}"` : ""}>${inline(text.trim())}</${tag}>`;

    /** Render a markdown subset to HTML. Input is escaped before anything else. */
    const render = (source) => {
      const lines = String(source || "").split("\n");
      const out = [];
      const lists = [];
      let paragraph = [];

      const flushParagraph = () => {
        if (paragraph.length) {
          out.push(`<p>${inline(paragraph.join("\n"))}</p>`);
          paragraph = [];
        }
      };
      const closeLists = (indent) => {
        while (lists.length && (indent === undefined || indent < lists[lists.length - 1].indent)) {
          const list = lists.pop();
          if (list.item) out.push("</li>");
          out.push(`</${list.tag}>`);
        }
      };
      const flush = () => {
        flushParagraph();
        closeLists();
      };
      const openItem = (indent, tag, text) => {
        flushParagraph();
        closeLists(indent);
        const top = lists[lists.length - 1];
        if (!top || indent > top.indent) {
          out.push(`<${tag}>`);
          lists.push({ tag, indent, item: false });
        } else if (top.tag !== tag) {
          if (top.item) out.push("</li>");
          out.push(`</${top.tag}>`);
          lists.pop();
          out.push(`<${tag}>`);
          lists.push({ tag, indent, item: false });
        }
        const list = lists[lists.length - 1];
        if (list.item) out.push("</li>");
        const task = /^\[([ xX])\]\s+(.*)$/.exec(text);
        if (task) {
          const checked = task[1].toLowerCase() === "x" ? " checked" : "";
          out.push(
            `<li class="task"><input type="checkbox" disabled${checked} /> ${inline(task[2])}`,
          );
        } else {
          out.push(`<li>${inline(text)}`);
        }
        list.item = true;
      };

      for (let i = 0; i < lines.length; i += 1) {
        const line = lines[i].replace(/\t/g, "    ");

        const fence = /^\s*(`{3,}|~{3,})\s*([\w+-]*)\s*$/.exec(line);
        if (fence) {
          flush();
          const body = [];
          const marker = fence[1][0];
          i += 1;
          for (; i < lines.length; i += 1) {
            if (new RegExp(`^\\s*${marker}{3,}\\s*$`).test(lines[i])) break;
            body.push(lines[i]);
          }
          const language = fence[2]
            ? ` class="language-${escape(fence[2].toLowerCase())}"`
            : "";
          out.push(`<pre><code${language}>${escape(body.join("\n"))}</code></pre>`);
          continue;
        }

        if (!line.trim()) {
          flush();
          continue;
        }

        const heading = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
        if (heading) {
          flush();
          const level = heading[1].length;
          out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
          continue;
        }

        if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
          flush();
          out.push("<hr />");
          continue;
        }

        const quote = /^\s*>\s?(.*)$/.exec(line);
        if (quote) {
          flush();
          const body = [quote[1]];
          while (i + 1 < lines.length && /^\s*>\s?/.test(lines[i + 1])) {
            body.push(lines[(i += 1)].replace(/^\s*>\s?/, ""));
          }
          out.push(`<blockquote>${inline(body.join("\n"))}</blockquote>`);
          continue;
        }

        // A pipe table: a header row, an alignment row, then the body.
        const columns = i + 1 < lines.length ? alignments(lines[i + 1]) : null;
        if (columns && /\|/.test(line)) {
          flush();
          const head = split(line);
          const rows = [];
          i += 1;
          while (i + 1 < lines.length && /\|/.test(lines[i + 1]) && lines[i + 1].trim()) {
            rows.push(split(lines[(i += 1)]));
          }
          const header = head
            .map((text, index) => cell("th", text, columns[index]))
            .join("");
          const body = rows
            .map(
              (row) =>
                `<tr>${head
                  .map((_c, index) => cell("td", row[index] || "", columns[index]))
                  .join("")}</tr>`,
            )
            .join("");
          out.push(
            `<div class="table-wrap"><table><thead><tr>${header}</tr></thead>` +
              `<tbody>${body}</tbody></table></div>`,
          );
          continue;
        }

        const item = /^(\s*)(?:([-*+])|(\d+)[.)])\s+(.*)$/.exec(line);
        if (item) {
          openItem(item[1].length, item[2] ? "ul" : "ol", item[4]);
          continue;
        }

        // An indented line under an open item continues that item.
        const list = lists[lists.length - 1];
        if (list && list.item && /^\s{2,}/.test(line)) {
          out.push(` ${inline(line.trim())}`);
          continue;
        }

        closeLists();
        paragraph.push(line);
      }
      flush();
      return out.join("\n");
    };

    return { render, escape, safeUrl, inline };
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

  // -------------------------------------------------------------- clipboard

  const Clipboard = {
    /** Copy text, falling back to a selection when the async API is barred. */
    async write(text) {
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
          return true;
        }
      } catch {
        // the page is not allowed to use the async API; fall through
      }
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.cssText = "position:fixed;top:-1000px;opacity:0";
      document.body.appendChild(area);
      area.select();
      let copied = false;
      try {
        copied = document.execCommand("copy");
      } catch {
        copied = false;
      }
      area.remove();
      return copied;
    },
  };

  // ------------------------------------------------------------------ theme

  /** CSS variable to the VS Code colors it is taken from, best first.
   *  Mirrors THEME_KEYS in agent.py, which does the same for a server theme. */
  const THEME_KEYS = {
    "--bg": ["editor.background"],
    "--fg": ["editor.foreground", "foreground"],
    "--bg-soft": [
      "sideBar.background",
      "editorGroupHeader.tabsBackground",
      "activityBar.background",
    ],
    "--bg-raised": [
      "editorWidget.background",
      "dropdown.background",
      "menu.background",
      "input.background",
    ],
    "--input-bg": ["input.background", "editorWidget.background"],
    "--input-fg": ["input.foreground", "editor.foreground", "foreground"],
    "--input-border": [
      "input.border",
      "editorWidget.border",
      "panel.border",
      "contrastBorder",
    ],
    "--border": [
      "panel.border",
      "editorGroup.border",
      "editorWidget.border",
      "contrastBorder",
      "input.border",
    ],
    "--accent": ["button.background", "focusBorder"],
    "--accent-fg": ["button.foreground"],
    "--focus": ["focusBorder", "button.background"],
    "--muted": [
      "descriptionForeground",
      "editorLineNumber.foreground",
      "disabledForeground",
    ],
    "--error": ["errorForeground", "editorError.foreground", "inputValidation.errorBorder"],
    "--link": ["textLink.foreground", "textLink.activeForeground"],
    "--code-bg": ["textCodeBlock.background", "editorWidget.background"],
    "--badge-bg": ["badge.background"],
    "--badge-fg": ["badge.foreground"],
    "--scroll": ["scrollbarSlider.background"],
  };

  const Theme = {
    /** Normalise the theme kinds VS Code writes onto the ones the CSS knows. */
    type(raw) {
      const value = String(raw || "").toLowerCase().replace(/[\s_]+/g, "-");
      if (value === "hc" || value === "hcdark" || value === "hc-dark") return "hc-dark";
      if (value === "hclight" || value === "hc-light") return "hc-light";
      return value === "light" ? "light" : "dark";
    },
    //: What the last theme set, so a new one can undo it. A theme that names
    //: fewer colors than the one before it must not inherit the difference.
    applied: [],
    /** Apply a CSS variable map (already mapped from a VS Code theme). */
    apply(vars) {
      if (!vars) return;
      const root = document.documentElement;
      this.applied.forEach((name) => root.style.removeProperty(name));
      this.applied = [];
      Object.entries(vars).forEach(([name, value]) => {
        if (name === "--theme-type") {
          root.dataset.themeType = this.type(value);
          return;
        }
        if (/^--[\w-]+$/.test(name) && typeof value === "string") {
          root.style.setProperty(name, value);
          this.applied.push(name);
        }
      });
    },
    /** Map a raw VS Code theme file onto CSS variables (client side loading). */
    fromVsCode(theme) {
      const colors = (theme && theme.colors) || {};
      const vars = {};
      Object.entries(THEME_KEYS).forEach(([name, keys]) => {
        const key = keys.find((candidate) => colors[candidate]);
        if (key) vars[name] = String(colors[key]);
      });
      vars["--theme-type"] = this.type(theme && theme.type);
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
      this.frame = 0;
      this.node = document.createElement("article");
      this.node.className = "block";
      this.node.dataset.kind = this.kind;
      this.node.dataset.streaming = "true";
      this.node.dataset.collapsed = "false";
      this.node.innerHTML =
        `<header class="block-head">` +
        `<button class="block-toggle" type="button" aria-expanded="true">` +
        Icons.markup("chevron-down", "icon chevron") +
        Icons.markup(KIND_ICONS[this.kind] || "bot", "icon kind") +
        `<span class="label"></span></button>` +
        `<span class="time"></span>` +
        `<span class="block-actions">` +
        `<button class="button tiny copy" type="button" title="Copy" ` +
        `aria-label="Copy to clipboard">${Icons.markup("copy")}</button>` +
        `</span></header><div class="block-body"></div>`;
      this.label(role === "user" ? "you" : this.kind);
      this.node.querySelector(".time").textContent = new Date().toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
      });
      this.body = this.node.querySelector(".block-body");
      this.toggle = this.node.querySelector(".block-toggle");
      this.copyButton = this.node.querySelector(".copy");
      this.toggle.addEventListener("click", () => this.collapse());
      this.copyButton.addEventListener("click", () => this.copy());
    }

    label(text) {
      this.node.querySelector(".label").textContent = text;
    }

    /** Fold the body away, leaving the head as the handle that brings it back. */
    collapse(force) {
      const collapsed = force === undefined ? this.node.dataset.collapsed !== "true" : force;
      this.node.dataset.collapsed = String(collapsed);
      this.toggle.setAttribute("aria-expanded", String(!collapsed));
    }

    async copy() {
      const copied = await Clipboard.write(this.text);
      this.copyButton.innerHTML = Icons.markup(copied ? "check" : "close");
      this.copyButton.title = copied ? "Copied" : "Copy failed";
      clearTimeout(this.copied);
      this.copied = setTimeout(() => {
        this.copyButton.innerHTML = Icons.markup("copy");
        this.copyButton.title = "Copy";
      }, 1400);
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

    /** Coalesce renders onto a frame: a token arrives far faster than a paint. */
    render() {
      if (this.frame) return;
      this.frame = requestAnimationFrame(() => {
        this.frame = 0;
        this.paint();
      });
    }

    paint() {
      // A tool call is a transcript of what ran, never markdown to interpret.
      if (this.kind === "tool") this.body.textContent = this.text;
      else this.body.innerHTML = Markdown.render(this.text);
    }

    end() {
      this.streaming = false;
      this.node.dataset.streaming = "false";
      if (this.frame) cancelAnimationFrame(this.frame);
      this.frame = 0;
      this.paint();
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
          close.className = "button tiny";
          close.title = `Remove ${item.name}`;
          close.setAttribute("aria-label", `Remove ${item.name}`);
          close.innerHTML = Icons.markup("close");
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
    name: "agent",
    connect() {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      this.setState("connecting");
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
    /** The mark carries the state; the text is kept for screen readers only. */
    setState(state) {
      el.app.dataset.state = state;
      const words = {
        online: "connected",
        offline: "disconnected",
        connecting: "connecting",
      };
      el.status.textContent = words[state] || state;
      this.describe();
      const offline = state !== "online";
      el.input.disabled = offline;
      el.send.disabled = offline;
      if (!offline) el.input.focus({ preventScroll: true });
    },
    describe() {
      const state = el.app.dataset.state;
      const working = el.app.dataset.busy === "true" ? ", working" : "";
      const words = {
        online: "connected",
        offline: "disconnected",
        connecting: "connecting",
      };
      const label = `${this.name}, ${words[state] || state}${working}`;
      el.brand.setAttribute("aria-label", label);
      el.brand.title = label.replace(", ", " — ");
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
      el.app.dataset.busy = String(Boolean(active));
      el.stop.hidden = !active;
      el.send.hidden = active;
      this.describe();
    },
    receive(message) {
      switch (message.type) {
        case "hello":
          this.session = message.session && message.session.id;
          el.session.textContent = this.session ? `#${this.session}` : "";
          this.name = (message.config && message.config.name) || "agent";
          this.describe();
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

  /** Grow the box with the text it holds, up to the cap the stylesheet sets. */
  const resize = () => {
    const limit = parseFloat(getComputedStyle(el.input).maxHeight);
    const max = Number.isFinite(limit) ? limit : 176;
    el.input.style.height = "auto";
    const wanted = el.input.scrollHeight;
    el.input.style.height = `${Math.min(wanted, max)}px`;
    el.input.style.overflowY = wanted > max ? "auto" : "hidden";
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
      resize();
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

  resize();
  Socket.connect();

  // The subsystems, for a test harness or a console: everything the page does
  // is reachable without a socket.
  window.agentUI = { Icons, Markdown, Media, Clipboard, Theme, Blocks, Commands, Socket };
})();
