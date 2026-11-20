/* SmartPad phone app.
 * Modules: storage/settings · API client · real-time connection · router ·
 *          touchpad · handwriting canvas · review/organize/export · history · settings UI */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  // -------------------------------------------------------------------------
  // Storage helpers (all wrapped: private mode / blocked storage must not break the app)
  // -------------------------------------------------------------------------
  const store = {
    get(k, d = null) { try { const v = localStorage.getItem("sp." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("sp." + k, JSON.stringify(v)); } catch {} },
    del(k) { try { localStorage.removeItem("sp." + k); } catch {} },
  };
  const session = {
    get(k, d = null) { try { const v = sessionStorage.getItem("sp." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { sessionStorage.setItem("sp." + k, JSON.stringify(v)); } catch {} },
  };

  const DEFAULTS = {
    sensitivity: 1.6, acceleration: 0.8, scrollSpeed: 1, naturalScroll: true, tapToClick: true, haptics: true,
    penOnly: false, keepDrafts: false, language: "en", style: "academic", saveImage: false,
  };
  const settings = Object.assign({}, DEFAULTS, store.get("settings", {}));
  const saveSettings = () => store.set("settings", settings);

  const STYLES = {
    simple: "Simple Text", professional: "Professional Notes", academic: "Academic Notes",
    assignment: "Assignment Format", report: "Report Format", meeting: "Meeting Notes", outline: "Structured Outline",
  };

  // -------------------------------------------------------------------------
  // UI helpers
  // -------------------------------------------------------------------------
  let toastTimer;
  function toast(msg, isError = false, ms = 2600) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.toggle("error", isError);
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), isError ? Math.max(ms, 4500) : ms);
  }
  function busy(on, text = "Working…") {
    $("#busy").hidden = !on;
    $("#busyText").textContent = text;
  }
  const haptic = (ms = 8) => { if (settings.haptics && navigator.vibrate) try { navigator.vibrate(ms); } catch {} };

  // -------------------------------------------------------------------------
  // Pairing token
  // -------------------------------------------------------------------------
  let token = store.get("token", "");
  (function readPairingFromUrl() {
    const m = location.hash.match(/pair=([\w-]+)/);
    if (m) {
      token = m[1];
      store.set("token", token);
      history.replaceState(null, "", location.pathname + "#home");
    }
  })();

  // -------------------------------------------------------------------------
  // REST client
  // -------------------------------------------------------------------------
  class ApiError extends Error {}
  async function api(path, { method = "GET", body, timeout = 30000, raw = false } = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    let res;
    try {
      res = await fetch(path, {
        method,
        headers: { "X-SmartPad-Token": token, ...(body ? { "Content-Type": "application/json" } : {}) },
        body: body ? JSON.stringify(body) : undefined,
        signal: ctrl.signal,
      });
    } catch (e) {
      throw new ApiError(e.name === "AbortError" ? "The computer took too long to respond." : "Cannot reach the computer. Check Wi-Fi and that SmartPad is running.");
    } finally {
      clearTimeout(timer);
    }
    if (res.status === 401) {
      conn.unauthorized();
      throw new ApiError("Not paired. Scan the QR code on your computer.");
    }
    if (!res.ok) {
      let detail = `Error ${res.status}`;
      try { const j = await res.json(); detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
      throw new ApiError(detail);
    }
    return raw ? res : res.json();
  }

  // -------------------------------------------------------------------------
  // Real-time connection with heartbeat + automatic reconnection
  // -------------------------------------------------------------------------
  const conn = {
    ws: null, state: "offline", backoff: 500, timer: null, pingTimer: null, lastPong: 0, latency: null,
    info: null, stopped: false,

    deviceName() {
      const ua = navigator.userAgent;
      const m = ua.match(/Android [\d.]+; ([^;)]+)/);
      return m ? m[1].replace(/Build.*/, "").trim() : /iPhone|iPad/.test(ua) ? "iPhone" : "Phone";
    },
    connect() {
      clearTimeout(this.timer);
      if (!token) { this.set("offline", "Not paired"); return; }
      if (this.ws && (this.ws.readyState === 0 || this.ws.readyState === 1)) return;
      this.stopped = false;
      this.set("connecting");
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const url = `${proto}://${location.host}/ws?token=${encodeURIComponent(token)}&device=${encodeURIComponent(this.deviceName())}`;
      let ws;
      try { ws = new WebSocket(url); } catch { return this.scheduleReconnect(); }
      this.ws = ws;
      ws.onopen = () => { this.backoff = 500; this.lastPong = Date.now(); };
      ws.onmessage = (e) => {
        let m; try { m = JSON.parse(e.data); } catch { return; }
        if (m.t === "hello") {
          this.info = m;
          this.set("online");
          refreshStatus();
          this.startHeartbeat();
        } else if (m.t === "pong") {
          this.lastPong = Date.now();
          this.latency = Date.now() - m.ts;
          $("#connLatency").textContent = `${this.latency} ms`;
          this.sendNow({ t: "latency", ms: this.latency });
        } else if (m.t === "notice") {
          toast(m.detail, true);
        } else if (m.t === "error") {
          if (m.detail === "unauthorized") this.unauthorized();
          else console.warn("server:", m.detail);
        }
      };
      ws.onclose = (e) => {
        this.stopHeartbeat();
        if (this.ws === ws) this.ws = null;
        if (e.code === 4001) return this.unauthorized();
        if (!this.stopped) this.scheduleReconnect();
      };
      ws.onerror = () => {};
    },
    scheduleReconnect() {
      this.set("connecting", `Reconnecting in ${Math.round(this.backoff / 1000)}s…`);
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.connect(), this.backoff);
      this.backoff = Math.min(this.backoff * 2, 8000);
    },
    startHeartbeat() {
      this.stopHeartbeat();
      const beat = () => {
        if (Date.now() - this.lastPong > 7000) { // dead connection (e.g. Wi-Fi switched)
          try { this.ws && this.ws.close(); } catch {}
          return;
        }
        this.sendNow({ t: "ping", ts: Date.now() });
      };
      beat();
      this.pingTimer = setInterval(beat, 2000);
    },
    stopHeartbeat() { clearInterval(this.pingTimer); },
    unauthorized() {
      this.stopped = true;
      store.del("token");
      token = "";
      this.set("offline", "Pairing expired — scan the QR code again");
      try { this.ws && this.ws.close(); } catch {}
    },
    sendNow(obj) {
      if (this.ws && this.ws.readyState === 1) { this.ws.send(JSON.stringify(obj)); return true; }
      return false;
    },
    set(state, detail = "") {
      this.state = state;
      document.body.dataset.conn = state;
      const label = { online: "Connected", connecting: "Connecting…", offline: token ? "Offline" : "Not paired" }[state];
      $("#statusChip .label").textContent = label;
      $("#connState").textContent = label;
      $("#connDetail").textContent = detail;
      $("#tileConnect").textContent = state === "online" ? `Connected to ${(this.info && this.info.computer) || location.hostname}` : label;
      $("#homeHint").textContent = state === "online"
        ? "Connected. Choose Touchpad or Handwriting."
        : token ? "Connecting to your computer…" : "Scan the QR code on your computer to connect.";
      if (state !== "online") $("#connLatency").textContent = "–";
    },
  };
  $("#connHost").textContent = location.host;

  let caps = null;
  async function refreshStatus() {
    if (!token) return;
    try {
      const s = await api("/api/status", { timeout: 8000 });
      $("#connInput").textContent = s.input_control ? "Enabled" : (s.input_error || "Disabled on computer");
      caps = s.ai;
      const stateText = { ready: "ready", loading: "loading model…", not_loaded: "loads on first use", error: "error", unavailable: "not installed" }[s.ai.model_state] || s.ai.model_state;
      $("#connAI").textContent = s.ai.provider === "anthropic"
        ? `Claude (${s.ai.model})`
        : s.ai.recognition_engine === "trocr" ? `On this computer (TrOCR) — ${stateText}` : s.ai.recognition_engine || "None — type text manually";
      $("#versionInfo").textContent = `SmartPad ${s.version} · ${location.host}`;
      $("#useAILabel").textContent = s.ai.correction === "claude" ? "AI spelling & grammar correction" : "Spelling correction (offline)";
      $("#useAI").disabled = !s.ai.correction;
      if (!s.ai.correction) $("#useAI").checked = false;
    } catch (e) { /* status is informational */ }
  }

  // -------------------------------------------------------------------------
  // Router
  // -------------------------------------------------------------------------
  const screens = Object.fromEntries($$(".screen").map((s) => [s.dataset.screen, s]));
  let current = null;
  function show(name) {
    let scrollTo = null;
    if (name === "export") { name = "organize"; scrollTo = "#exportCard"; }
    if (!screens[name]) name = "home";
    if (current === name && !scrollTo) return;
    const prev = current;
    current = name;
    Object.values(screens).forEach((s) => s.classList.toggle("active", s.dataset.screen === name));
    $("#screenTitle").textContent = screens[name].dataset.title;
    $("#backBtn").hidden = name === "home";
    if (prev === "touchpad") touchpad.leave();
    if (name === "touchpad") touchpad.enter();
    if (name === "canvas") requestAnimationFrame(() => ink.resize());
    if (name === "organize") docView.render();
    if (name === "review") syncReview();
    if (name === "history") history_.load();
    if (name === "connect") refreshStatus();
    if (scrollTo) setTimeout(() => $(scrollTo).scrollIntoView({ behavior: "smooth" }), 50);
    else window.scrollTo(0, 0);
  }
  function go(name) { if (location.hash !== "#" + name) location.hash = name; else show(name); }
  window.addEventListener("hashchange", () => show(location.hash.slice(1) || "home"));
  $("#backBtn").onclick = () => (history.length > 1 ? history.back() : go("home"));
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-go]");
    if (t) go(t.dataset.go);
  });

  // -------------------------------------------------------------------------
  // Connect screen
  // -------------------------------------------------------------------------
  $("#reconnectBtn").onclick = () => { try { conn.ws && conn.ws.close(); } catch {} conn.backoff = 500; conn.connect(); };
  $("#forgetBtn").onclick = () => {
    if (!confirm("Forget this computer? You'll need to scan the QR code again.")) return;
    conn.unauthorized();
    toast("Pairing removed");
  };
  $("#codeBtn").onclick = async () => {
    const code = $("#codeInput").value.trim();
    if (!code) return toast("Enter the pairing code", true);
    const old = token;
    token = code;
    try {
      await api("/api/status", { timeout: 8000 });
      store.set("token", token);
      $("#codeInput").value = "";
      toast("Paired ✓");
      conn.stopped = false;
      conn.connect();
    } catch (e) {
      token = old;
      toast(e.message, true);
    }
  };

  // -------------------------------------------------------------------------
  // Touchpad
  // -------------------------------------------------------------------------
  const touchpad = (() => {
    const pad = $("#pad");
    const pts = new Map(); // pointerId -> {x, y, sx, sy, t}
    let qx = 0, qy = 0, sx = 0, sy = 0, rafPending = false;
    let mode = null; // null | "move" | "scroll"
    let moved = false, maxPointers = 0, downAt = 0, longTimer = null, longFired = false;
    let lastTapAt = 0, lastTapX = 0, lastTapY = 0, dragArmed = false, dragging = false;
    let wakeLock = null;
    const TAP_MS = 230, TAP_SLOP = 9, LONG_MS = 600;

    function flush() {
      rafPending = false;
      const evs = [];
      if (qx || qy) { evs.push({ t: "move", dx: +qx.toFixed(2), dy: +qy.toFixed(2) }); qx = qy = 0; }
      if (sx || sy) { evs.push({ t: "scroll", dx: +sx.toFixed(3), dy: +sy.toFixed(3) }); sx = sy = 0; }
      if (evs.length) conn.sendNow(evs.length === 1 ? evs[0] : evs);
    }
    const schedule = () => { if (!rafPending) { rafPending = true; requestAnimationFrame(flush); } };
    function send(ev) { flush(); if (!conn.sendNow(ev)) toast("Not connected to the computer", true, 1200); }

    function queueMove(dx, dy, dt) {
      const speed = Math.hypot(dx, dy) / Math.max(dt, 1); // px per ms
      const f = settings.sensitivity * (1 + settings.acceleration * Math.min(speed, 3));
      qx += dx * f; qy += dy * f;
      schedule();
    }
    // Wheel units: +y = scroll up, +x = scroll right (pynput convention). ~28px of finger travel = 1 notch.
    function queueScroll(dx, dy) {
      const k = settings.scrollSpeed / 28;
      if (settings.naturalScroll) { sy += dy * k; sx += -dx * k; } // content follows the fingers (like a phone)
      else { sy += -dy * k; sx += dx * k; }                         // classic wheel direction
      schedule();
    }
    function queueStripScroll(dy) { sy += -dy * (settings.scrollSpeed / 18); schedule(); } // drag down = scroll down

    pad.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      try { pad.setPointerCapture(e.pointerId); } catch {}
      pts.set(e.pointerId, { x: e.clientX, y: e.clientY, sx: e.clientX, sy: e.clientY, t: e.timeStamp });
      pad.classList.add("touching");
      $("#padHint").style.opacity = 0;
      maxPointers = Math.max(maxPointers, pts.size);
      if (pts.size === 1) {
        downAt = e.timeStamp; moved = false; longFired = false; mode = "move";
        dragArmed = e.timeStamp - lastTapAt < 320 && Math.hypot(e.clientX - lastTapX, e.clientY - lastTapY) < 50;
        clearTimeout(longTimer);
        longTimer = setTimeout(() => {
          if (!moved && pts.size === 1 && !dragArmed) { longFired = true; haptic(25); send({ t: "click", b: "right" }); }
        }, LONG_MS);
      } else {
        clearTimeout(longTimer);
        if (pts.size === 2) mode = "scroll";
      }
    });

    pad.addEventListener("pointermove", (e) => {
      const p = pts.get(e.pointerId);
      if (!p) return;
      const events = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
      for (const ce of events.length ? events : [e]) {
        const dx = ce.clientX - p.x, dy = ce.clientY - p.y, dt = ce.timeStamp - p.t;
        p.x = ce.clientX; p.y = ce.clientY; p.t = ce.timeStamp;
        if (!moved && Math.hypot(p.x - p.sx, p.y - p.sy) > TAP_SLOP) { moved = true; clearTimeout(longTimer); }
        if (!moved) continue;
        if (mode === "move" && pts.size === 1) {
          if (dragArmed && !dragging) { dragging = true; haptic(15); send({ t: "down", b: "left" }); }
          queueMove(dx, dy, dt);
        } else if (mode === "scroll") {
          queueScroll(dx / pts.size, dy / pts.size); // average across fingers
        }
      }
    });

    function end(e) {
      if (!pts.has(e.pointerId)) return;
      pts.delete(e.pointerId);
      if (pts.size) return;
      clearTimeout(longTimer);
      pad.classList.remove("touching");
      const dur = e.timeStamp - downAt;
      if (dragging) {
        dragging = false; send({ t: "up", b: "left" });
      } else if (!moved && !longFired && dur < 350 && e.type !== "pointercancel") {
        if (maxPointers === 2) { haptic(); send({ t: "click", b: "right" }); }
        else if (maxPointers >= 3) { haptic(); send({ t: "click", b: "middle" }); }
        else if (settings.tapToClick && dur < TAP_MS) {
          haptic(); send({ t: "click", b: "left" });
          lastTapAt = e.timeStamp; lastTapX = e.clientX; lastTapY = e.clientY;
        }
      }
      dragArmed = false; maxPointers = 0; mode = null;
    }
    pad.addEventListener("pointerup", end);
    pad.addEventListener("pointercancel", end);
    pad.addEventListener("contextmenu", (e) => e.preventDefault());

    // Scroll strip
    const strip = $("#scrollStrip");
    let stripY = null;
    strip.addEventListener("pointerdown", (e) => { e.preventDefault(); strip.setPointerCapture(e.pointerId); stripY = e.clientY; });
    strip.addEventListener("pointermove", (e) => { if (stripY === null) return; queueStripScroll(e.clientY - stripY); stripY = e.clientY; });
    const stripEnd = () => { stripY = null; };
    strip.addEventListener("pointerup", stripEnd);
    strip.addEventListener("pointercancel", stripEnd);

    // Physical-style buttons: press = down, release = up (hold Left + move on pad = drag)
    for (const [id, b] of [["#leftBtn", "left"], ["#rightBtn", "right"]]) {
      const el = $(id);
      el.addEventListener("pointerdown", (e) => { e.preventDefault(); el.setPointerCapture(e.pointerId); el.classList.add("down"); haptic(); send({ t: "down", b }); });
      const up = () => { if (!el.classList.contains("down")) return; el.classList.remove("down"); send({ t: "up", b }); };
      el.addEventListener("pointerup", up);
      el.addEventListener("pointercancel", up);
      el.addEventListener("contextmenu", (e) => e.preventDefault());
    }

    // Keyboard panel
    $("#kbBtn").onclick = () => { const p = $("#kbPanel"); p.hidden = !p.hidden; if (!p.hidden) $("#kbInput").focus(); };
    const sendText = () => {
      const v = $("#kbInput").value;
      if (!v) return;
      send({ t: "type", text: v });
      $("#kbInput").value = "";
    };
    $("#kbSend").onclick = sendText;
    $("#kbInput").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); sendText(); } });
    $$(".keys button").forEach((b) => (b.onclick = () => { haptic(); send({ t: "key", k: b.dataset.key }); }));

    return {
      async enter() {
        if (!token) toast("Pair with your computer first", true);
        else setTimeout(() => { if (current === "touchpad" && conn.state !== "online") toast("Waiting for connection to the computer…"); }, 2000);
        try { if ("wakeLock" in navigator) wakeLock = await navigator.wakeLock.request("screen"); } catch {}
      },
      leave() {
        if (dragging) { dragging = false; send({ t: "up", b: "left" }); }
        try { wakeLock && wakeLock.release(); } catch {}
        wakeLock = null;
        $("#padHint").style.opacity = "";
      },
    };
  })();

  // -------------------------------------------------------------------------
  // Handwriting canvas
  // -------------------------------------------------------------------------
  const ink = (() => {
    const wrap = $("#canvasWrap");
    const cv = $("#ink");
    const ctx = cv.getContext("2d");
    let ops = store.get("draft", []) || []; // stroke ops and {clear:true}
    if (!settings.keepDrafts) ops = [];
    let redo = [];
    let tool = "pen", color = "#111111", size = 3;
    let active = null, activeId = null;
    let dpr = 1, W = 0, H = 0;

    function resize() {
      const r = wrap.getBoundingClientRect();
      if (!r.width || !r.height) return;
      dpr = Math.min(window.devicePixelRatio || 1, 3);
      W = r.width; H = r.height;
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
      redraw();
    }
    function visibleOps() {
      let start = 0;
      ops.forEach((o, i) => { if (o.clear) start = i + 1; });
      return ops.slice(start);
    }
    function widthAt(s, p) { return s.tool === "eraser" ? s.size * 5 : s.pressure ? s.size * (0.35 + p * 1.3) : s.size; }

    function drawStroke(c, s) {
      const pts = s.pts;
      c.save();
      c.globalCompositeOperation = s.tool === "eraser" ? "destination-out" : "source-over";
      c.strokeStyle = s.color; c.fillStyle = s.color;
      c.lineCap = "round"; c.lineJoin = "round";
      if (pts.length === 1) {
        c.beginPath(); c.arc(pts[0][0], pts[0][1], widthAt(s, pts[0][2]) / 2, 0, Math.PI * 2); c.fill();
      } else {
        for (let i = 1; i < pts.length; i++) drawSegment(c, s, i);
      }
      c.restore();
    }
    // Quadratic smoothing through midpoints
    function drawSegment(c, s, i) {
      const p = s.pts, a = p[i - 1], b = p[i];
      const prev = i > 1 ? p[i - 2] : a;
      const m0 = [(prev[0] + a[0]) / 2, (prev[1] + a[1]) / 2];
      const m1 = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
      c.lineWidth = widthAt(s, (a[2] + b[2]) / 2);
      c.beginPath();
      c.moveTo(i > 1 ? m0[0] : a[0], i > 1 ? m0[1] : a[1]);
      c.quadraticCurveTo(a[0], a[1], m1[0], m1[1]);
      c.stroke();
    }
    function redraw() {
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, cv.width, cv.height);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      for (const s of visibleOps()) drawStroke(ctx, s);
      updateButtons();
    }
    function updateButtons() {
      $("#undoBtn").disabled = !ops.length;
      $("#redoBtn").disabled = !redo.length;
      const n = visibleOps().length;
      $("#pageInfo").textContent = n ? `${n} stroke${n > 1 ? "s" : ""}` : "Write here";
    }
    let draftTimer;
    function changed() {
      updateButtons();
      if (!settings.keepDrafts) return;
      clearTimeout(draftTimer);
      draftTimer = setTimeout(() => store.set("draft", ops.slice(-3000)), 400);
    }

    function pos(e) {
      const r = cv.getBoundingClientRect();
      const p = e.pointerType === "pen" ? (e.pressure || 0.5) : 0.5;
      return [+(e.clientX - r.left).toFixed(1), +(e.clientY - r.top).toFixed(1), +p.toFixed(2)];
    }
    cv.addEventListener("pointerdown", (e) => {
      if (settings.penOnly && e.pointerType === "touch") return; // palm rejection: stylus only
      if (active) return; // one stroke at a time (a resting palm cannot start a second stroke)
      e.preventDefault();
      try { cv.setPointerCapture(e.pointerId); } catch {}
      activeId = e.pointerId;
      active = { tool, color, size, pressure: e.pointerType === "pen", pts: [pos(e)] };
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      drawStroke(ctx, active);
    });
    cv.addEventListener("pointermove", (e) => {
      if (!active || e.pointerId !== activeId) return;
      const evs = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
      ctx.save();
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.globalCompositeOperation = active.tool === "eraser" ? "destination-out" : "source-over";
      ctx.strokeStyle = active.color; ctx.lineCap = "round"; ctx.lineJoin = "round";
      for (const ce of evs.length ? evs : [e]) {
        const p = pos(ce), last = active.pts[active.pts.length - 1];
        if (Math.hypot(p[0] - last[0], p[1] - last[1]) < 0.8) continue;
        active.pts.push(p);
        drawSegment(ctx, active, active.pts.length - 1);
      }
      ctx.restore();
    });
    function endStroke(e) {
      if (!active || e.pointerId !== activeId) return;
      ops.push(active); redo = [];
      active = null; activeId = null;
      changed();
    }
    cv.addEventListener("pointerup", endStroke);
    cv.addEventListener("pointercancel", endStroke);
    cv.addEventListener("contextmenu", (e) => e.preventDefault());

    // Toolbar
    $$("[data-tool]").forEach((b) => (b.onclick = () => {
      tool = b.dataset.tool;
      $$("[data-tool]").forEach((x) => x.classList.toggle("active", x === b));
    }));
    $$(".swatch").forEach((b) => (b.onclick = () => {
      color = b.dataset.color; tool = "pen";
      $$(".swatch").forEach((x) => x.classList.toggle("active", x === b));
      $$("[data-tool]").forEach((x) => x.classList.toggle("active", x.dataset.tool === "pen"));
    }));
    $("#penSize").oninput = (e) => { size = +e.target.value; $("#penSizeVal").textContent = size; };
    $("#undoBtn").onclick = () => { if (ops.length) { redo.push(ops.pop()); redraw(); changed(); } };
    $("#redoBtn").onclick = () => { if (redo.length) { ops.push(redo.pop()); redraw(); changed(); } };
    $("#clearBtn").onclick = () => clear();
    function clear() {
      if (!visibleOps().length) return;
      ops.push({ clear: true }); redo = []; redraw(); changed();
    }

    // Image of the ink, cropped to the written area, on white
    function toImage(maxDim = 1600) {
      const vis = visibleOps().filter((s) => s.pts);
      if (!vis.some((s) => s.tool === "pen")) return null;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const s of vis) for (const [x, y] of s.pts) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
      const padPx = 24;
      x0 -= padPx; y0 -= padPx; x1 += padPx; y1 += padPx;
      const w = x1 - x0, h = y1 - y0;
      const scale = Math.min(2, maxDim / Math.max(w, h));
      const layer = document.createElement("canvas");
      layer.width = Math.ceil(w * scale); layer.height = Math.ceil(h * scale);
      const lc = layer.getContext("2d");
      lc.setTransform(scale, 0, 0, scale, -x0 * scale, -y0 * scale);
      for (const s of vis) drawStroke(lc, s);
      const out = document.createElement("canvas");
      out.width = layer.width; out.height = layer.height;
      const oc = out.getContext("2d");
      oc.fillStyle = "#fff"; oc.fillRect(0, 0, out.width, out.height);
      oc.drawImage(layer, 0, 0);
      return out.toDataURL("image/png");
    }

    $("#saveInkBtn").onclick = () => {
      const url = toImage(2400);
      if (!url) return toast("Nothing written yet", true);
      const a = document.createElement("a");
      a.href = url;
      a.download = `handwriting-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-")}.png`;
      document.body.appendChild(a); a.click(); a.remove();
      toast("Handwriting image saved to Downloads");
    };

    window.addEventListener("resize", () => { if (current === "canvas") resize(); });
    return { resize, toImage, clear, hasInk: () => visibleOps().some((s) => s.tool === "pen") };
  })();

  // -------------------------------------------------------------------------
  // Work state (current text + document) – kept for this browser session only
  // -------------------------------------------------------------------------
  const work = Object.assign(
    { raw: "", engine: "", style: settings.style, doc: null, image: null },
    session.get("work", {})
  );
  const saveWork = () => session.set("work", { ...work, image: null });

  // Convert handwriting -> text
  $("#langSelect").value = settings.language;
  $("#convertBtn").onclick = async () => {
    const image = ink.toImage();
    if (!image) return toast("Write something on the canvas first", true);
    await refreshStatus();
    const lang = $("#langSelect").value;
    if (lang === "ur" && caps && !caps.urdu_recognition) {
      toast("Urdu handwriting can't be read offline. Type the Urdu text on the next screen instead.", true);
      go("review");
      return;
    }
    busy(true, caps && caps.model_state !== "ready" && caps.provider !== "anthropic"
      ? "Loading the handwriting model on your computer (first time can take a minute)…"
      : "Recognising handwriting…");
    try {
      const r = await api("/api/recognize", { method: "POST", body: { image, language: lang }, timeout: 600000 });
      if (!r.text.trim()) { toast("No handwriting detected — try writing larger", true); return; }
      work.raw = work.raw.trim() ? work.raw.trim() + "\n\n" + r.text : r.text;
      work.engine = r.engine;
      work.image = image;
      saveWork();
      go("review");
      toast("Converted ✓ — review the text");
    } catch (e) {
      toast(e.message, true);
      if (/No handwriting recognition engine/.test(e.message)) go("review");
    } finally {
      busy(false);
    }
  };

  // Review screen
  const rawText = $("#rawText");
  function syncReview() {
    rawText.value = work.raw;
    rawText.dir = /[؀-ۿ]/.test(work.raw) ? "rtl" : "auto";
    $("#engineBadge").textContent = work.engine ? `via ${work.engine}` : "";
  }
  rawText.addEventListener("input", () => { work.raw = rawText.value; saveWork(); });
  $("#writeMoreBtn").onclick = () => { ink.clear(); go("canvas"); };
  $("#clearTextBtn").onclick = () => { if (!rawText.value || confirm("Clear the recognised text?")) { work.raw = ""; work.engine = ""; saveWork(); syncReview(); } };

  const chips = $("#styleChips");
  chips.innerHTML = Object.entries(STYLES).map(([k, v]) => `<button class="chip" data-style="${k}">${v}</button>`).join("");
  const markStyle = () => $$(".chip", chips).forEach((c) => c.classList.toggle("active", c.dataset.style === work.style));
  chips.onclick = (e) => { const c = e.target.closest(".chip"); if (c) { work.style = c.dataset.style; saveWork(); markStyle(); } };
  markStyle();

  $("#organizeBtn").onclick = async () => {
    const text = rawText.value.trim();
    if (!text) return toast("There is no text to organise yet", true);
    busy(true, "Organising your notes…");
    try {
      const r = await api("/api/organize", { method: "POST", body: { text, style: work.style, use_ai: $("#useAI").checked }, timeout: 180000 });
      const h1 = r.markdown.match(/^#\s+(.+)$/m);
      work.doc = { id: null, markdown: r.markdown, style: r.style, engine: r.engine, title: h1 ? h1[1].replace(/[*_`]/g, "").trim() : "Notes" };
      saveWork();
      go("organize");
      if (r.corrections && r.corrections.length) {
        const list = r.corrections.slice(0, 4).map((c) => `${c.from}→${c.to}`).join(", ");
        toast(`Fixed ${r.corrections.length} spelling${r.corrections.length > 1 ? "s" : ""}: ${list}${r.corrections.length > 4 ? "…" : ""}`, false, 4500);
      }
    } catch (e) {
      toast(e.message, true);
    } finally {
      busy(false);
    }
  };

  // Document view
  const docView = {
    render() {
      const has = !!(work.doc && work.doc.markdown);
      $("#emptyDoc").hidden = has;
      $("#docArea").hidden = !has;
      if (!has) return;
      $("#docTitle").value = work.doc.title || "";
      $("#mdEdit").value = work.doc.markdown;
      $("#preview").innerHTML = SmartMarkdown.render(work.doc.markdown);
      $("#fmtBadge").textContent = `${STYLES[work.doc.style] || ""}${work.doc.engine ? " · " + work.doc.engine : ""}`;
    },
  };
  $$(".seg button").forEach((b) => (b.onclick = () => {
    $$(".seg button").forEach((x) => x.classList.toggle("active", x === b));
    const edit = b.dataset.view === "edit";
    $("#mdEdit").hidden = !edit;
    $("#preview").hidden = edit;
    if (!edit) $("#preview").innerHTML = SmartMarkdown.render(work.doc.markdown);
  }));
  $("#mdEdit").addEventListener("input", (e) => { work.doc.markdown = e.target.value; saveWork(); });
  $("#docTitle").addEventListener("input", (e) => { work.doc.title = e.target.value; saveWork(); });

  const plainText = (md) => md
    .replace(/^#{1,6}\s+/gm, "").replace(/\*\*([^*]+)\*\*/g, "$1").replace(/__([^_]+)__/g, "$1")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1$2").replace(/`([^`]+)`/g, "$1").replace(/^\s*-\s+\[( |x)\]\s+/gim, "• ")
    .replace(/^(\s*)[-*+]\s+/gm, "$1• ").replace(/^\s*\|?\s*:?-{2,}.*$/gm, "").trim();

  $$("[data-export]").forEach((b) => (b.onclick = async () => {
    if (!work.doc) return toast("Nothing to export", true);
    const format = b.dataset.export;
    const body = { markdown: work.doc.markdown, format, title: work.doc.title || "SmartPad Document" };
    busy(true, `Creating ${format.toUpperCase()}…`);
    try {
      if ($("#exportToPC").checked) {
        const r = await api("/api/export/save", { method: "POST", body, timeout: 60000 });
        toast(`Saved on computer: ${r.name}`);
      } else {
        const res = await api("/api/export", { method: "POST", body, timeout: 60000, raw: true });
        const blob = await res.blob();
        const cd = res.headers.get("Content-Disposition") || "";
        const name = (cd.match(/filename="([^"]+)"/) || [])[1] || `document.${format}`;
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url; a.download = name;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
        toast(`Downloaded ${name}`);
      }
    } catch (e) {
      toast(e.message, true);
    } finally {
      busy(false);
    }
  }));

  $("#saveDocBtn").onclick = async () => {
    if (!work.doc) return;
    try {
      const r = await api("/api/documents", {
        method: "POST",
        body: { id: work.doc.id, title: work.doc.title, style: work.doc.style, raw_text: work.raw, markdown: work.doc.markdown, image: settings.saveImage ? work.image : null, save_image: settings.saveImage },
        timeout: 20000,
      });
      work.doc.id = r.id; saveWork();
      toast("Saved to history ✓");
    } catch (e) { toast(e.message, true); }
  };
  $("#typeBtn").onclick = async () => {
    if (!work.doc) return;
    if (!confirm("Click into the target window on your computer (e.g. Word) first. Type the document there now?")) return;
    try {
      await api("/api/type", { method: "POST", body: { text: plainText(work.doc.markdown) }, timeout: 120000 });
      toast("Typed on computer ✓");
    } catch (e) { toast(e.message, true); }
  };
  $("#copyBtn").onclick = async () => {
    if (!work.doc) return;
    const text = work.doc.markdown;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); } catch {}
      ta.remove();
    }
    toast("Copied");
  };

  // -------------------------------------------------------------------------
  // History
  // -------------------------------------------------------------------------
  const history_ = {
    async load() {
      const list = $("#historyList");
      if (!token) { list.innerHTML = `<div class="card">Pair with your computer to see saved documents.</div>`; return; }
      list.innerHTML = `<div class="card muted">Loading…</div>`;
      try {
        const docs = await api("/api/documents");
        if (!docs.length) { list.innerHTML = `<div class="card muted">No saved documents yet. Use <b>Save to history</b> on an organised document.</div>`; return; }
        list.innerHTML = docs.map((d) => `
          <div class="item" data-id="${d.id}">
            <div class="meta"><b>${SmartMarkdown.escape(d.title)}</b>
              <small>${STYLES[d.style] || d.style} · ${new Date(d.updated * 1000).toLocaleString()}${d.has_image ? " · 🖼" : ""}</small></div>
            <button data-act="open">Open</button><button data-act="del" aria-label="Delete">🗑️</button>
          </div>`).join("");
      } catch (e) {
        list.innerHTML = `<div class="card">${SmartMarkdown.escape(e.message)}</div>`;
      }
    },
  };
  $("#historyList").onclick = async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const id = btn.closest(".item").dataset.id;
    try {
      if (btn.dataset.act === "open") {
        const d = await api(`/api/documents/${id}`);
        work.doc = { id: d.id, title: d.title, style: d.style, markdown: d.markdown, engine: "" };
        work.raw = d.raw_text || "";
        saveWork();
        go("organize");
      } else if (confirm("Delete this document from the computer?")) {
        await api(`/api/documents/${id}`, { method: "DELETE" });
        toast("Deleted");
        history_.load();
      }
    } catch (err) { toast(err.message, true); }
  };

  // -------------------------------------------------------------------------
  // Settings UI
  // -------------------------------------------------------------------------
  $("#defaultStyle").innerHTML = Object.entries(STYLES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
  $$("[data-setting]").forEach((el) => {
    const key = el.dataset.setting;
    const out = el.parentElement.querySelector("output");
    if (el.type === "checkbox") el.checked = !!settings[key];
    else el.value = settings[key];
    if (out) out.textContent = el.value;
    el.addEventListener(el.type === "range" ? "input" : "change", () => {
      settings[key] = el.type === "checkbox" ? el.checked : el.type === "range" ? +el.value : el.value;
      if (out) out.textContent = el.value;
      if (key === "keepDrafts" && !el.checked) store.del("draft");
      if (key === "language") $("#langSelect").value = el.value;
      saveSettings();
    });
  });

  // -------------------------------------------------------------------------
  // Boot
  // -------------------------------------------------------------------------
  syncReview();
  conn.set("offline");
  conn.connect();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && !conn.ws && token) { conn.backoff = 500; conn.connect(); }
  });
  window.addEventListener("online", () => { conn.backoff = 500; conn.connect(); });
  show(location.hash.slice(1) || "home");
  if ("serviceWorker" in navigator && window.isSecureContext) navigator.serviceWorker.register("/sw.js").catch(() => {});
})();
