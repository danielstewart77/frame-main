/* frame-main web console.
 *
 * Ported from spark_to_bloom's terminal.html — same weight, no build step. The
 * differences that matter: layout lives on the server (frame_state on the
 * session row, sidebar_collapsed on the surface binding) instead of
 * localStorage, turns ride a WebSocket instead of SSE, and spawn exists.
 */
(function () {
  "use strict";

  const app = document.getElementById("app");
  const listEl = document.getElementById("list");
  const listEmpty = document.getElementById("list-empty");
  const gridEl = document.getElementById("grid");
  const dockEl = document.getElementById("dock");
  const tabActive = document.getElementById("tab-active");
  const tabArchived = document.getElementById("tab-archived");
  const railCollapse = document.getElementById("rail-collapse");
  const railExpand = document.getElementById("rail-expand");
  const spawnBtn = document.getElementById("spawn");
  const spawnHarness = document.getElementById("spawn-harness");
  const spawnModel = document.getElementById("spawn-model");

  const ACCENTS = ["#4ee8fc", "#4ee88a", "#c9a84c", "#e0724e", "#b06ee0", "#e05c8a", "#7e93a8"];
  const TTS_IDLE_MS = 1200;
  const isWide = () => window.matchMedia("(min-width: 860px)").matches;

  const frames = new Map();   // session id -> frame
  let boot = null;
  let filter = "active";
  let listed = [];
  let maximized = null;

  // --- api ----------------------------------------------------------------

  async function api(method, path, body) {
    const opts = { method: method, credentials: "same-origin" };
    if (body !== undefined) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(path, opts);
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    return resp.status === 204 ? null : resp.json();
  }

  function socketUrl(path) {
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    return scheme + "//" + location.host + path;
  }

  const patchSession = (id, fields) => api("PATCH", "/sessions/" + id, fields);

  // --- shared mic (one recognizer, aimed at whichever frame started it) ----

  const SpeechCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
  let recognition = null;
  let micTarget = null;
  let micBase = "";

  function stopMic() {
    if (!recognition) return;
    const target = micTarget;
    micTarget = null;
    try { recognition.stop(); } catch (e) {}
    if (target) target.setMic("off");
  }

  function startMic(frame) {
    if (!SpeechCtor) return;
    if (micTarget && micTarget !== frame) stopMic();
    if (!recognition) {
      recognition = new SpeechCtor();
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.lang = navigator.language || "en-US";
      recognition.onresult = function (event) {
        if (!micTarget) return;
        let final = "";
        let interim = "";
        for (let i = 0; i < event.results.length; i++) {
          const res = event.results[i];
          const text = res[0] ? res[0].transcript : "";
          if (res.isFinal) final += (final ? " " : "") + text.trim();
          else interim += text;
        }
        const kept = micBase ? micBase + " " : "";
        micTarget.setInput((kept + final + " " + interim).replace(/\s+/g, " ").trim());
      };
      recognition.onerror = function () {
        if (micTarget) micTarget.setMic("error");
        micTarget = null;
      };
      recognition.onend = function () {
        if (!micTarget) return;
        micBase = micTarget.getInput().trim();
        try { recognition.start(); } catch (e) { stopMic(); }
      };
    }
    micTarget = frame;
    micBase = frame.getInput().trim();
    try {
      recognition.start();
      frame.setMic("on");
    } catch (e) {
      frame.setMic("error");
    }
  }

  // --- one session, rendered as a frame ------------------------------------

  function createFrame(session) {
    const id = session.id;
    let state = session.frame_state === "minimized" ? "minimized" : "docked";
    let accent = session.color || "";
    let title = session.title || "";

    const el = document.createElement("section");
    el.className = "frame";
    el.dataset.sessionId = id;
    el.innerHTML =
      '<header class="frame-head">' +
        '<button class="action-btn f-back" type="button" title="back to sessions" aria-label="back">&lsaquo;</button>' +
        '<span class="frame-title"></span>' +
        '<button class="action-btn f-speaker" type="button" aria-pressed="false" title="toggle spoken replies">speaker off</button>' +
        '<button class="action-btn f-menu" type="button" title="frame menu" aria-label="frame menu">&#9776;</button>' +
        '<button class="action-btn f-min" type="button" title="minimize" aria-label="minimize">&minus;</button>' +
        '<button class="action-btn f-max" type="button" title="maximize" aria-label="maximize">&#9633;</button>' +
        '<button class="action-btn f-close" type="button" title="close frame" aria-label="close">&times;</button>' +
      '</header>' +
      '<div class="frame-body">' +
        '<div class="feed" aria-live="polite"></div>' +
        '<div class="sidecar">' +
          '<div class="sidecar-head">' +
            '<span class="sidecar-name"></span>' +
            '<span style="flex:1"></span>' +
            '<button class="action-btn sc-close" type="button" aria-label="close pane">&times;</button>' +
          '</div>' +
          '<div class="sidecar-body"></div>' +
        '</div>' +
      '</div>' +
      '<form class="bar" autocomplete="off">' +
        '<textarea class="f-input" rows="1" placeholder="message…" aria-label="message"></textarea>' +
        '<button class="action-btn f-mic" type="button" hidden title="dictate">mic</button>' +
        '<button class="action-btn" type="submit">send</button>' +
      '</form>';

    const feed = el.querySelector(".feed");
    const form = el.querySelector(".bar");
    const input = el.querySelector(".f-input");
    const titleEl = el.querySelector(".frame-title");
    const speakerBtn = el.querySelector(".f-speaker");
    const menuBtn = el.querySelector(".f-menu");
    const micBtn = el.querySelector(".f-mic");
    const sidecar = el.querySelector(".sidecar");
    const sidecarName = el.querySelector(".sidecar-name");
    const sidecarBody = el.querySelector(".sidecar-body");

    let socket = null;
    let assistantEl = null;
    let inFlight = false;
    let destroyed = false;
    const queue = [];           // {text, el, pill, sent}

    // per-frame TTS
    const audio = new Audio();
    let speakerOn = !!session.speaker;
    let ttsBuffer = "";
    let ttsTimer = null;
    let ttsPlaying = false;
    const ttsQueue = [];

    // --- chrome -----------------------------------------------------------

    function applyLabel() {
      titleEl.textContent = (title || id.slice(0, 8)) + " · " + session.harness;
      titleEl.title = id;
      el.style.borderLeftColor = accent || "transparent";
    }

    function appendLine(cls, text) {
      assistantEl = null;
      const line = document.createElement("div");
      line.className = "line " + cls;
      line.textContent = text;
      feed.appendChild(line);
      feed.scrollTop = feed.scrollHeight;
      return line;
    }

    function startAssistant() {
      assistantEl = document.createElement("div");
      assistantEl.className = "line assistant";
      feed.appendChild(assistantEl);
      feed.scrollTop = feed.scrollHeight;
    }

    function appendAssistant(text) {
      if (!assistantEl) startAssistant();
      assistantEl.textContent += text;
      feed.scrollTop = feed.scrollHeight;
    }

    // --- tts --------------------------------------------------------------

    function setSpeakerLabel() {
      speakerBtn.textContent = speakerOn ? "speaker on" : "speaker off";
      speakerBtn.setAttribute("aria-pressed", speakerOn ? "true" : "false");
      speakerBtn.classList.toggle("is-on", speakerOn);
    }

    function ttsAccumulate(chunk) {
      if (!speakerOn) return;
      ttsBuffer += chunk;
      if (ttsTimer) clearTimeout(ttsTimer);
      ttsTimer = setTimeout(flushTts, TTS_IDLE_MS);
    }

    async function flushTts() {
      if (ttsTimer) { clearTimeout(ttsTimer); ttsTimer = null; }
      const text = ttsBuffer.trim();
      ttsBuffer = "";
      if (!speakerOn || !text) return;
      try {
        const resp = await fetch("/voice/speak", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: text }),
        });
        if (!resp.ok) return;
        ttsQueue.push(URL.createObjectURL(await resp.blob()));
        playNextClip();
      } catch (e) {}
    }

    function playNextClip() {
      if (ttsPlaying) return;
      const url = ttsQueue.shift();
      if (!url) return;
      ttsPlaying = true;
      audio.src = url;
      audio.play().catch(function () { ttsPlaying = false; URL.revokeObjectURL(url); });
      audio.onended = function () {
        URL.revokeObjectURL(url);
        ttsPlaying = false;
        playNextClip();
      };
    }

    function silence() {
      ttsBuffer = "";
      if (ttsTimer) { clearTimeout(ttsTimer); ttsTimer = null; }
      ttsQueue.length = 0;
      try { audio.pause(); audio.src = ""; } catch (e) {}
      ttsPlaying = false;
    }

    speakerBtn.addEventListener("click", function () {
      speakerOn = !speakerOn;
      setSpeakerLabel();
      if (!speakerOn) silence();
      patchSession(id, { speaker: speakerOn }).catch(function () {});
    });

    // --- streaming --------------------------------------------------------

    function handleEvent(event) {
      const front = queue[0];
      if (front && front.sent && front.el.classList.contains("pending")) {
        front.el.classList.remove("pending");
      }
      if (event.kind === "text") {
        appendAssistant(event.text || "");
        ttsAccumulate(event.text || "");
      } else if (event.kind === "tool") {
        appendLine("tool", "· " + (event.name || "tool"));
      } else if (event.kind === "status") {
        appendLine("system", event.text || "");
      } else if (event.kind === "error") {
        appendLine("error", event.text || "error");
        finishTurn();
      } else if (event.kind === "result") {
        finishTurn();
      } else if (event.kind === "gap") {
        appendLine("system", "· some output was lost while disconnected");
      }
      // `session` and `raw` need no rendering
    }

    function finishTurn() {
      inFlight = false;
      assistantEl = null;
      flushTts();
      const front = queue.shift();
      if (front && front.pill) front.pill.remove();
      if (front && front.el) front.el.classList.remove("pending");
      sendFront();
    }

    // The last event seq this frame rendered. The server drops a socket that
    // falls behind rather than starving it, so a reconnect asks for the tail
    // from here and comes back whole.
    let lastSeq = 0;

    function connect() {
      if (destroyed) return;
      const path = "/sessions/" + id + "/stream" + (lastSeq ? "?since=" + lastSeq : "");
      socket = new WebSocket(socketUrl(path));
      socket.onmessage = function (msg) {
        let event;
        try { event = JSON.parse(msg.data); } catch (e) { return; }
        if (typeof event.seq === "number") lastSeq = event.seq;
        handleEvent(event);
      };
      socket.onclose = function () {
        socket = null;
        if (destroyed) return;
        setTimeout(connect, 3000);
      };
    }

    function sendFront() {
      if (inFlight || destroyed) return;
      const item = queue[0];
      if (!item || item.sent) return;
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        setTimeout(sendFront, 300);
        return;
      }
      item.sent = true;
      inFlight = true;
      if (!item.pill) {
        item.pill = makePill();
        item.el.appendChild(item.pill);
      }
      socket.send(JSON.stringify({ prompt: item.text }));
    }

    function makePill() {
      const pill = document.createElement("button");
      pill.type = "button";
      pill.className = "pill";
      pill.textContent = "interrupt";
      pill.addEventListener("click", async function () {
        pill.disabled = true;
        pill.textContent = "interrupting…";
        try {
          await api("POST", "/sessions/" + id + "/interrupt");
        } catch (e) {
          pill.disabled = false;
          pill.textContent = "interrupt";
        }
      });
      return pill;
    }

    // --- input ------------------------------------------------------------

    function autoGrow() {
      input.style.height = "auto";
      input.style.height = input.scrollHeight + "px";
    }

    function submit(text) {
      const line = document.createElement("div");
      line.className = "line user pending";
      line.textContent = "> " + text;
      feed.appendChild(line);
      feed.scrollTop = feed.scrollHeight;
      queue.push({ text: text, el: line, pill: null, sent: false });
      sendFront();
    }

    input.addEventListener("input", autoGrow);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
    });
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      if (micTarget === frame) { stopMic(); micBase = ""; }
      const text = input.value.trim();
      if (!text) return;
      input.value = "";
      autoGrow();
      submit(text);
    });

    if (SpeechCtor) {
      micBtn.hidden = false;
      micBtn.addEventListener("click", function () {
        if (micTarget === frame) stopMic(); else startMic(frame);
      });
    }

    // --- drag and drop ----------------------------------------------------

    el.addEventListener("dragover", function (e) {
      e.preventDefault();
      el.classList.add("is-drop");
    });
    el.addEventListener("dragleave", function () { el.classList.remove("is-drop"); });
    el.addEventListener("drop", async function (e) {
      e.preventDefault();
      el.classList.remove("is-drop");
      const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
      for (const file of files) {
        if (file.size > 512 * 1024) {
          appendLine("system", file.name + ": too large to inline (" + file.size + " bytes)");
          continue;
        }
        let text;
        try {
          text = await file.text();
        } catch (err) {
          appendLine("system", file.name + ": unreadable");
          continue;
        }
        if (/[\x00-\x08\x0e-\x1f]/.test(text)) {
          appendLine("system", file.name + ": binary, not inlined");
          continue;
        }
        const prefix = input.value.trim() ? input.value.trimEnd() + "\n\n" : "";
        input.value = prefix + "--- " + file.name + " ---\n" + text;
        autoGrow();
      }
      input.focus();
    });

    // --- sidecar panes ----------------------------------------------------

    function closeSidecar() {
      if (sidecar.dataset.pane === "tui" && sidecar._tui) {
        sidecar._tui.close();
        sidecar._tui = null;
      }
      delete sidecar.dataset.open;
      delete sidecar.dataset.pane;
      sidecarBody.innerHTML = "";
    }

    function openSidecar(pane) {
      if (sidecar.dataset.pane === pane) { closeSidecar(); return; }
      closeSidecar();
      sidecar.dataset.open = "1";
      sidecar.dataset.pane = pane;
      sidecarName.textContent = pane;
      if (pane === "browser") {
        const iframe = document.createElement("iframe");
        iframe.src = "/sessions/" + id + "/app/";
        sidecarBody.appendChild(iframe);
      } else if (pane === "diff") {
        const pre = document.createElement("pre");
        pre.textContent = "loading…";
        sidecarBody.appendChild(pre);
        api("GET", "/sessions/" + id + "/diff")
          .then(function (data) { pre.textContent = data.diff || "(no changes)"; })
          .catch(function (err) { pre.textContent = "diff failed: " + err.message; });
      } else if (pane === "tui") {
        sidecar._tui = openTui(id, sidecarBody);
      }
    }

    el.querySelector(".sc-close").addEventListener("click", closeSidecar);

    // --- frame menu -------------------------------------------------------

    menuBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      openMenu(menuBtn, frame);
    });

    el.querySelector(".f-min").addEventListener("click", function () { setState("minimized"); });
    el.querySelector(".f-max").addEventListener("click", function () { toggleMax(id); });
    el.querySelector(".f-close").addEventListener("click", function () { closeFrame(id); });
    el.querySelector(".f-back").addEventListener("click", function () { showView("list"); });

    function setState(next) {
      state = next;
      patchSession(id, { frame_state: next }).catch(function () {});
      relayout();
      renderList();
    }

    applyLabel();
    setSpeakerLabel();
    setTimeout(autoGrow, 0);
    connect();

    const frame = {
      el: el,
      id: id,
      session: session,
      get state() { return state; },
      setState: setState,
      openSidecar: openSidecar,
      get accent() { return accent; },
      focus: function () { input.focus(); },
      getInput: function () { return input.value; },
      setInput: function (v) { input.value = v; autoGrow(); },
      setMic: function (mode) {
        micBtn.textContent = mode === "on" ? "listening…" : mode === "error" ? "mic error" : "mic";
        micBtn.classList.toggle("is-on", mode === "on");
      },
      rename: function (name) {
        title = name;
        session.title = name;
        applyLabel();
        relayout();
        patchSession(id, { title: name }).then(refreshList).catch(function () {});
      },
      recolor: function (color) {
        accent = color;
        session.color = color;
        applyLabel();
        relayout();
        patchSession(id, { color: color }).then(refreshList).catch(function () {});
      },
      destroy: function () {
        destroyed = true;
        if (micTarget === frame) stopMic();
        closeSidecar();
        silence();
        if (socket) { socket.onclose = null; socket.close(); socket = null; }
        if (el.parentNode) el.parentNode.removeChild(el);
      },
    };
    return frame;
  }

  // --- the terminal pane ---------------------------------------------------

  const ANSI = /\x1b\[[0-9;?]*[ -\/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[()][0-9A-B]/g;

  /* Not a full emulator — escape sequences are stripped and output appended.
   * Enough to run commands and read output; a curses app will look wrong. */
  function openTui(sessionId, mount) {
    const out = document.createElement("pre");
    out.className = "term-out";
    out.tabIndex = 0;
    mount.appendChild(out);

    const socket = new WebSocket(
      (location.protocol === "https:" ? "wss:" : "ws:") + "//" + location.host +
      "/sessions/" + sessionId + "/tui"
    );

    socket.onmessage = function (msg) {
      out.textContent += String(msg.data).replace(ANSI, "");
      out.scrollTop = out.scrollHeight;
    };
    socket.onclose = function () { out.textContent += "\n[terminal closed]\n"; };
    socket.onopen = function () {
      socket.send(JSON.stringify({ resize: { rows: 24, cols: 100 } }));
      out.focus();
    };

    function send(data) {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ data: data }));
      }
    }

    out.addEventListener("keydown", function (e) {
      if (e.metaKey || e.ctrlKey) {
        if (e.key.length === 1) {
          e.preventDefault();
          const code = e.key.toUpperCase().charCodeAt(0) - 64;
          if (code > 0 && code < 32) send(String.fromCharCode(code));
        }
        return;
      }
      const keys = { Enter: "\r", Backspace: "\x7f", Tab: "\t", Escape: "\x1b",
                     ArrowUp: "\x1b[A", ArrowDown: "\x1b[B",
                     ArrowRight: "\x1b[C", ArrowLeft: "\x1b[D" };
      if (keys[e.key]) { e.preventDefault(); send(keys[e.key]); }
      else if (e.key.length === 1) { e.preventDefault(); send(e.key); }
    });

    return { close: function () { try { socket.close(); } catch (e) {} } };
  }

  // --- frame menu ----------------------------------------------------------

  let openMenuEl = null;

  function dismissMenu() {
    if (openMenuEl && openMenuEl.parentNode) openMenuEl.parentNode.removeChild(openMenuEl);
    openMenuEl = null;
  }

  function openMenu(anchor, frame) {
    dismissMenu();
    const menu = document.createElement("div");
    menu.className = "menu";
    const rect = anchor.getBoundingClientRect();
    menu.style.top = rect.bottom + 4 + "px";
    menu.style.left = Math.max(4, rect.left - 120) + "px";

    function item(label, handler) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.addEventListener("click", function () { dismissMenu(); handler(); });
      menu.appendChild(button);
    }

    item("browser pane", function () { frame.openSidecar("browser"); });
    item("diff pane", function () { frame.openSidecar("diff"); });
    item("full terminal", function () { frame.openSidecar("tui"); });
    item("rename…", function () {
      const name = prompt("session name", frame.session.title || "");
      if (name !== null) frame.rename(name.trim());
    });

    const swatches = document.createElement("div");
    swatches.className = "swatches";
    ACCENTS.forEach(function (color) {
      const swatch = document.createElement("button");
      swatch.type = "button";
      swatch.className = "swatch" + (frame.accent === color ? " is-selected" : "");
      swatch.style.background = color;
      swatch.setAttribute("aria-label", "accent " + color);
      swatch.addEventListener("click", function () {
        dismissMenu();
        frame.recolor(frame.accent === color ? "" : color);
      });
      swatches.appendChild(swatch);
    });
    menu.appendChild(swatches);

    item("pull to local…", async function () {
      try {
        const data = await api("GET", "/sessions/" + frame.id + "/clone-url");
        prompt("clone this session's branch:", data.command);
      } catch (e) {}
    });
    item("open code", async function () {
      try {
        const data = await api("GET", "/sessions/" + frame.id + "/clone-url");
        prompt("the repo is already on this host — open it with:", "code " + data.clone_url);
      } catch (e) {}
    });
    item("archive", async function () {
      await api("POST", "/sessions/" + frame.id + "/archive");
      closeFrame(frame.id, true);
      refreshList();
    });
    item("delete", async function () {
      if (!confirm("Delete this session? This cannot be undone.")) return;
      await api("DELETE", "/sessions/" + frame.id);
      closeFrame(frame.id, true);
      refreshList();
    });

    document.body.appendChild(menu);
    openMenuEl = menu;
  }

  document.addEventListener("click", function (e) {
    if (openMenuEl && !openMenuEl.contains(e.target)) dismissMenu();
  });

  // --- grid ----------------------------------------------------------------

  function docked() {
    return Array.from(frames.values()).filter(function (f) { return f.state === "docked"; });
  }

  function relayout() {
    // Maximize is an overlay: it hides the others without touching their state,
    // so un-maximizing is a no-op on the underlying layout.
    if (maximized && !frames.has(maximized)) maximized = null;
    if (maximized) app.dataset.max = maximized; else delete app.dataset.max;

    frames.forEach(function (frame) {
      frame.el.classList.toggle("is-max", frame.id === maximized);
      if (!maximized) frame.el.style.display = frame.state === "docked" ? "" : "none";
      else frame.el.style.display = "";
    });

    const count = maximized ? 1 : docked().length;
    gridEl.dataset.count = String(count);
    gridEl.style.gridTemplateColumns = count
      ? "repeat(" + Math.ceil(Math.sqrt(count)) + ", minmax(0, 1fr))"
      : "";

    renderDock();
  }

  function renderDock() {
    const mins = Array.from(frames.values()).filter(function (f) { return f.state === "minimized"; });
    dockEl.innerHTML = "";
    dockEl.hidden = mins.length === 0;
    mins.forEach(function (frame) {
      const tab = document.createElement("button");
      tab.type = "button";
      tab.className = "dock-tab";
      tab.textContent = frame.session.title || frame.id.slice(0, 8);
      tab.style.borderLeftColor = frame.accent || "transparent";
      tab.addEventListener("click", function () { frame.setState("docked"); });
      dockEl.appendChild(tab);
    });
  }

  function toggleMax(id) {
    maximized = maximized === id ? null : id;
    relayout();
  }

  function openFrame(session) {
    const existing = frames.get(session.id);
    if (existing) {
      if (existing.state === "minimized") existing.setState("docked");
      existing.focus();
      if (!isWide()) showView("stage");
      return existing;
    }
    const frame = createFrame(session);
    frames.set(session.id, frame);
    gridEl.appendChild(frame.el);
    if (session.frame_state !== "docked" && session.frame_state !== "minimized") {
      patchSession(session.id, { frame_state: "docked" }).catch(function () {});
    }
    relayout();
    renderList();
    frame.focus();
    if (!isWide()) showView("stage");
    return frame;
  }

  function closeFrame(id, gone) {
    const frame = frames.get(id);
    if (!frame) return;
    frames.delete(id);
    frame.destroy();
    if (maximized === id) maximized = null;
    if (!gone) patchSession(id, { frame_state: "closed" }).catch(function () {});
    relayout();
    renderList();
    if (!isWide() && frames.size === 0) showView("list");
  }

  function showView(name) { app.dataset.view = name; }

  // --- sidebar -------------------------------------------------------------

  function renderList() {
    listEl.innerHTML = "";
    listed.forEach(function (session) {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "card" + (frames.has(session.id) ? " is-open" : "");
      card.style.borderLeftColor = session.color || "transparent";

      const dot = document.createElement("span");
      dot.className = "card-dot" + (session.container_id ? " is-running" : "");
      if (session.color) dot.style.background = session.color;

      const body = document.createElement("span");
      body.className = "card-body";
      const name = document.createElement("span");
      name.className = "card-title";
      name.textContent = session.title || session.id.slice(0, 8);
      const meta = document.createElement("span");
      meta.className = "card-meta";
      meta.textContent = session.harness + " · " + session.model +
        (session.container_id ? " · running" : "");
      body.appendChild(name);
      body.appendChild(meta);

      card.appendChild(dot);
      card.appendChild(body);
      card.addEventListener("click", function () { openFrame(session); });
      listEl.appendChild(card);
    });
    listEmpty.hidden = listed.length > 0;
    listEmpty.textContent = filter === "active" ? "no active sessions." : "no archived sessions.";
  }

  async function refreshList() {
    try {
      listed = await api("GET", "/users/" + boot.user_id + "/sessions?status=" + filter);
    } catch (e) {
      listed = [];
    }
    renderList();
  }

  function setFilter(next) {
    filter = next;
    const active = next === "active";
    tabActive.classList.toggle("is-active", active);
    tabArchived.classList.toggle("is-active", !active);
    tabActive.setAttribute("aria-selected", String(active));
    tabArchived.setAttribute("aria-selected", String(!active));
    refreshList();
  }

  tabActive.addEventListener("click", function () { setFilter("active"); });
  tabArchived.addEventListener("click", function () { setFilter("archived"); });

  function setRail(collapsed) {
    app.dataset.rail = collapsed ? "collapsed" : "expanded";
    railExpand.hidden = !collapsed;
    api("PATCH", "/surfaces/web/" + boot.external_id + "/layout",
        { sidebar_collapsed: collapsed }).catch(function () {});
  }

  railCollapse.addEventListener("click", function () { setRail(true); });
  railExpand.addEventListener("click", function () { setRail(false); });

  // --- spawn ---------------------------------------------------------------

  spawnBtn.addEventListener("click", async function () {
    const body = { harness: spawnHarness.value, model: spawnModel.value.trim() || undefined };
    try {
      const session = await api("POST", "/users/" + boot.user_id + "/sessions", body);
      if (filter !== "active") setFilter("active"); else await refreshList();
      openFrame(session);
    } catch (e) {}
  });

  // --- boot ----------------------------------------------------------------

  (async function start() {
    boot = await api("GET", "/console/bootstrap");

    boot.harnesses.forEach(function (name) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      if (name === boot.default_harness) option.selected = true;
      spawnHarness.appendChild(option);
    });
    spawnModel.value = boot.default_model;

    if (boot.sidebar_collapsed && isWide()) {
      app.dataset.rail = "collapsed";
      railExpand.hidden = false;
    }

    await refreshList();

    // Restore the frames the server says are open. Mobile lands on the list.
    boot.frames.forEach(function (session) { openFrame(session); });
    showView(isWide() || frames.size ? "stage" : "list");
  })().catch(function (err) {
    document.body.textContent = "console failed to start: " + err.message;
  });
})();
