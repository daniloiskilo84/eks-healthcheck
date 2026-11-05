// Small, dependency-free behaviour for the server-rendered pages.
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const storage = {
    get(key) { try { return sessionStorage.getItem(key) || ""; } catch (e) { return ""; } },
    set(key, value) { try { sessionStorage.setItem(key, value); } catch (e) { /* private mode */ } },
  };

  function toast(message, isError) {
    const el = $("#toast");
    if (!el) return;
    el.textContent = message;
    el.classList.toggle("err", !!isError);
    el.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { el.hidden = true; }, isError ? 7000 : 3500);
  }

  async function api(url, options = {}) {
    const res = await fetch(url, { credentials: "same-origin", ...options });
    const type = res.headers.get("content-type") || "";
    const body = type.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
      const msg = typeof body === "string" ? body : (body.error || body.detail || res.statusText);
      throw new Error(msg);
    }
    return body;
  }

  // ---- auto refresh (paused while a dialog is open or someone is typing) ----
  const refresh = parseInt(document.body.dataset.refresh || "0", 10);
  if (refresh > 0) {
    setInterval(() => {
      const dialogOpen = $$("dialog").some((d) => d.open);
      const typing = document.activeElement && ["INPUT", "SELECT"].includes(document.activeElement.tagName);
      if (!dialogOpen && !typing && document.visibilityState === "visible") location.reload();
    }, refresh * 1000);
  }

  // ---- table filter (kept across refreshes) ----
  const filter = $("#filter");
  if (filter) {
    const table = document.getElementById(filter.dataset.filterTable);
    const key = "filter:" + location.pathname;
    const apply = () => {
      const q = filter.value.trim().toLowerCase();
      $$("tbody tr", table).forEach((tr) => { tr.hidden = q && !tr.textContent.toLowerCase().includes(q); });
      storage.set(key, filter.value);
    };
    filter.value = storage.get(key);
    filter.addEventListener("input", apply);
    if (table) apply();
  }

  // ---- TV clock ----
  const clock = $("#clock");
  if (clock) {
    const tick = () => { clock.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); };
    tick();
    setInterval(tick, 10000);
  }

  // ---- rollout restart ----
  $$("[data-rollout]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const target = btn.dataset.rollout;
      if (!confirm(`Rollout restart ${target}?\n\nEvery pod is recreated, one at a time, following the deployment strategy.`)) return;
      btn.disabled = true;
      try {
        const [ns, name] = target.split("/");
        const res = await api(`/api/deployments/${encodeURIComponent(ns)}/${encodeURIComponent(name)}/rollout`, {
          method: "POST",
          headers: { "X-Requested-With": "healthcheck" },
        });
        toast(`Rollout started at ${res.restartedAt}`);
        setTimeout(() => location.reload(), 2500);
      } catch (err) {
        toast(`Rollout failed: ${err.message}`, true);
        btn.disabled = false;
      }
    });
  });

  // ---- events / logs viewer ----
  const viewer = $("#viewer");
  if (!viewer) return;
  const body = $("#viewer-body");
  const title = $("#viewer-title");
  const logControls = $("#log-controls");
  const containerSel = $("#log-container");
  const tailSel = $("#log-tail");
  const previousBox = $("#log-previous");
  let current = null;

  const message = (text, cls) => {
    body.replaceChildren();
    const div = document.createElement("div");
    div.className = cls;
    div.textContent = text;
    body.appendChild(div);
  };

  async function showEvents(target) {
    const [ns, pod] = target.split("/");
    title.textContent = `Events · ${pod}`;
    logControls.hidden = true;
    message("Loading events…", "loading");
    viewer.showModal();
    try {
      const events = await api(`/api/pods/${encodeURIComponent(ns)}/${encodeURIComponent(pod)}/events`);
      if (!events.length) {
        message("No events. Kubernetes keeps events for about one hour, so a healthy, older pod usually has none.", "loading");
        return;
      }
      const table = document.createElement("table");
      const head = table.createTHead().insertRow();
      ["Type", "Reason", "Age", "Count", "From", "Message"].forEach((h) => {
        const th = document.createElement("th");
        th.textContent = h;
        head.appendChild(th);
      });
      const tb = table.createTBody();
      events.forEach((e) => {
        const tr = tb.insertRow();
        if (e.type === "Warning") tr.className = "ev-warning";
        [e.type, e.reason, e.age, `×${e.count}`, e.source, e.message].forEach((v) => { tr.insertCell().textContent = v; });
      });
      body.replaceChildren(table);
    } catch (err) {
      message(err.message, "err");
    }
  }

  async function loadLogs() {
    const { ns, pod } = current;
    const params = new URLSearchParams({ container: containerSel.value, tail: tailSel.value, previous: previousBox.checked });
    message("Loading logs…", "loading");
    try {
      const text = await api(`/api/pods/${encodeURIComponent(ns)}/${encodeURIComponent(pod)}/logs?${params}`);
      const pre = document.createElement("pre");
      pre.textContent = text;
      body.replaceChildren(pre);
      body.scrollTop = body.scrollHeight;
    } catch (err) {
      message(err.message, "err");
    }
  }

  function showLogs(btn) {
    const [ns, pod] = btn.dataset.logs.split("/");
    current = { ns, pod };
    title.textContent = `Logs · ${pod}`;
    containerSel.replaceChildren(...btn.dataset.containers.split(",").filter(Boolean).map((c) => new Option(c, c)));
    previousBox.checked = false;
    previousBox.parentElement.hidden = btn.dataset.restarts === "0";
    logControls.hidden = false;
    viewer.showModal();
    loadLogs();
  }

  $$("[data-events]").forEach((b) => b.addEventListener("click", () => showEvents(b.dataset.events)));
  $$("[data-logs]").forEach((b) => b.addEventListener("click", () => showLogs(b)));
  [containerSel, tailSel, previousBox].forEach((el) => el.addEventListener("change", loadLogs));
  $("#log-reload").addEventListener("click", loadLogs);
  $("#viewer-close").addEventListener("click", () => viewer.close());
  viewer.addEventListener("click", (e) => { if (e.target === viewer) viewer.close(); });
})();
