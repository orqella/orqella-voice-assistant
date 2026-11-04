(() => {
  const $ = (id) => document.getElementById(id);
  let context = { config: {} };
  let routes = { groups: [] };
  let dataRoot = "";
  let setup = { ready: false };
  let pollTimer = null;
  let setupPoll = null;
  let deleteArmed = "";
  let deleteTimer = null;
  const backend = (path, method = "GET", body) => window.orqella.call("backend.request", {
    method,
    path,
    ...(body !== undefined ? { body } : {}),
  });

  const routeCount = () => routes.groups.reduce((total, group) => total + group.items.length, 0);
  const setStatus = (text, kind = "") => {
    $("status").textContent = text;
    $("status").className = `va-status ${kind}`;
  };
  const setSetup = (state) => {
    setup = state || {};
    $("setupTitle").textContent = state?.ready ? "Voice engine ready" : "Voice engine setup";
    $("setupStatus").textContent = state?.ready ? "ready" : state?.job === "running" ? "setting up…" : "not initialized";
    $("setupButton").disabled = !!state?.ready || state?.job === "running";
    $("setupButton").textContent = state?.ready ? "Ready" : state?.job === "running" ? "Setting up…" : "Set up voice engine";
    if (state?.log) {
      $("setupLog").hidden = false;
      $("setupLog").textContent = state.log.slice(-5000);
      $("setupLog").scrollTop = $("setupLog").scrollHeight;
    }
  };

  async function loadSetup() {
    try {
      const state = await backend("/api/status");
      dataRoot = state.data_root || dataRoot;
      setSetup(state);
      if (state.job === "running") {
        clearTimeout(setupPoll);
        setupPoll = setTimeout(loadSetup, 2000);
      }
    } catch (error) {
      $("setupStatus").textContent = error.message || "unavailable";
    }
  }

  async function startSetup() {
    $("setupButton").disabled = true;
    $("setupLog").hidden = false;
    $("setupLog").textContent = "Starting setup…";
    try {
      const state = await backend("/api/setup/start", "POST", {});
      setSetup(state);
      clearTimeout(setupPoll);
      setupPoll = setTimeout(loadSetup, 1500);
    } catch (error) {
      $("setupLog").textContent = error.message || "Could not start setup";
      $("setupButton").disabled = false;
    }
  }

  async function loadRoutes(force = false) {
    setStatus(force ? "Refreshing routes…" : "Loading routes…");
    try {
      routes = await backend(`/api/routes${force ? "?refresh=1" : ""}`);
      renderRoutes();
      setStatus(`${routeCount()} routes`);
      return routes;
    } catch (error) {
      setStatus(error.message || "Could not read routes", "bad");
      return routes;
    }
  }

  async function toggle(keys, enabled) {
    try {
      const result = await backend("/api/toggle", "POST", { keys, enabled });
      const disabled = new Set(result.disabled || []);
      for (const group of routes.groups) {
        for (const item of group.items) item.enabled = !disabled.has(item.key);
      }
      renderRoutes();
    } catch (error) {
      setStatus(error.message || "Could not update route", "bad");
    }
  }

  function routeByKey(key) {
    for (const group of routes.groups) {
      const item = group.items.find((entry) => entry.key === key);
      if (item) return { group, item };
    }
    return null;
  }

  function resetDelete() {
    deleteArmed = "";
    clearTimeout(deleteTimer);
    document.querySelectorAll("[data-delete]").forEach((button) => {
      button.textContent = "Delete route";
      button.classList.remove("armed");
    });
  }

  async function deleteRoute(button, key) {
    const found = routeByKey(key);
    if (!found?.item?.delete) return;
    if (deleteArmed !== key) {
      resetDelete();
      deleteArmed = key;
      button.textContent = "Confirm delete";
      button.classList.add("armed");
      deleteTimer = setTimeout(resetDelete, 5000);
      return;
    }
    resetDelete();
    setStatus(`Deleting ${found.item.name}…`);
    try {
      await backend("/api/routes/delete", "POST", { route: found.item.delete, key });
      await loadRoutes(true);
      setStatus(`Deleted ${found.item.name} · ${routeCount()} routes`, "ok");
    } catch (error) {
      setStatus(error.message || "Could not delete route", "bad");
    }
  }

  function detailMarkup(group, item) {
    const phrases = item.phrases?.length
      ? `<div class="va-kv"><b>Say</b><span>${item.phrases.map(escapeHtml).join(" · ")}</span></div>`
      : "";
    const action = item.detail
      ? `<div class="va-kv"><b>Action</b><span>${escapeHtml(item.detail)}</span></div>`
      : "";
    const source = group.source ? `<div class="va-kv"><b>Source</b><span>${escapeHtml(group.source)}</span></div>` : "";
    const remove = item.delete
      ? `<button class="va-delete" type="button" data-delete="${escapeHtml(item.key)}">Delete route</button><span class="va-note">Removes this route from config.toml.</span>`
      : `<span class="va-note">Engine route. Use the checkbox to disable it.</span>`;
    return `<div class="va-route-detail" data-detail-panel="${escapeHtml(item.key)}" hidden>
      <p class="va-route-description">${escapeHtml(item.description || "No description.")}</p>
      ${phrases}${action}${source}
      <div class="va-route-footer">${remove}</div>
    </div>`;
  }

  function renderRoutes() {
    const root = $("routes");
    if (!routes.groups?.length) {
      root.innerHTML = `<div class="va-status">No routes yet. Add routes with the agent below.</div>`;
      return;
    }
    root.innerHTML = routes.groups.map((group) => {
      const count = group.items.length;
      const enabled = group.items.filter((item) => item.enabled).length;
      return `<section class="va-group" data-group="${escapeHtml(group.id)}">
        <div class="va-group-head"><label class="va-group-title"><input type="checkbox" data-group-toggle="${escapeHtml(group.id)}" ${enabled === count ? "checked" : ""} ${enabled > 0 && enabled < count ? 'data-mixed="1"' : ""}>${escapeHtml(group.label)}</label><span class="va-status">${enabled}/${count}</span></div>
        <div class="va-items">${group.items.map((item) => `<div class="va-route" data-route-item="${escapeHtml(item.key)}">
          <div class="va-route-line"><label class="va-route-toggle" title="${escapeHtml(item.description || item.name)}"><input type="checkbox" data-route="${escapeHtml(item.key)}" ${item.enabled ? "checked" : ""}><span>${escapeHtml(item.name)}</span></label><button class="va-detail-toggle" type="button" data-detail="${escapeHtml(item.key)}" aria-expanded="false">Details</button></div>
          ${detailMarkup(group, item)}
        </div>`).join("")}</div>
      </section>`;
    }).join("");
    resetDelete();
    root.querySelectorAll("[data-route]").forEach((input) => input.onchange = () => toggle([input.dataset.route], input.checked));
    root.querySelectorAll("[data-detail]").forEach((button) => button.onclick = () => {
      const panel = root.querySelector(`[data-detail-panel="${button.dataset.detail}"]`);
      if (!panel) return;
      const opening = panel.hidden;
      panel.hidden = !opening;
      button.textContent = opening ? "Hide" : "Details";
      button.setAttribute("aria-expanded", String(opening));
    });
    root.querySelectorAll("[data-delete]").forEach((button) => button.onclick = () => deleteRoute(button, button.dataset.delete));
    root.querySelectorAll("[data-group-toggle]").forEach((input) => {
      input.indeterminate = input.dataset.mixed === "1";
      input.onchange = () => {
        const group = routes.groups.find((entry) => entry.id === input.dataset.groupToggle);
        if (group) toggle(group.items.map((item) => item.key), input.checked);
      };
    });
  }

  async function startAgent() {
    const prompt = $("prompt").value.trim();
    if (!prompt) return;
    $("run").disabled = true;
    $("agentLog").hidden = false;
    $("agentLog").textContent = "";
    $("agentStatus").textContent = "Starting agent…";
    const instruction = `Work in this Voice Assistant data directory. Add or update routes in config.toml according to this request:\n\n${prompt}\n\nRead config.toml first. Preserve existing routes and behavior. The route catalog reads config.toml and the vendored engine source. Do not modify engine source files; add modes and applications through config.toml where possible.`;
    try {
      const job = await window.orqella.call("agent.start", { prompt: instruction, root: dataRoot });
      $("agentStatus").textContent = `Agent ${job.id.slice(0, 8)} running…`;
      poll(job.id);
    } catch (error) {
      $("agentStatus").textContent = error.message || "Could not start agent";
      $("run").disabled = false;
    }
  }

  async function poll(id) {
    clearTimeout(pollTimer);
    try {
      const job = await window.orqella.call("agent.status", { id });
      $("agentLog").textContent = (job.log || "").slice(-6000);
      $("agentLog").scrollTop = $("agentLog").scrollHeight;
      $("agentStatus").textContent = `${job.status}…`;
      if (job.status === "succeeded") {
        $("agentStatus").textContent = "Routes updated.";
        $("run").disabled = false;
        $("prompt").value = "";
        await loadRoutes(true);
        return;
      }
      if (job.status === "failed") {
        $("agentStatus").textContent = job.error || "Agent failed.";
        $("run").disabled = false;
        return;
      }
      pollTimer = setTimeout(() => poll(id), 1500);
    } catch (error) {
      $("agentStatus").textContent = error.message || "Could not read agent job";
      $("run").disabled = false;
    }
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[character]));
  }

  $("refresh").onclick = () => loadRoutes(true);
  $("setupButton").onclick = startSetup;
  $("run").onclick = startAgent;
  window.orqella.ready((ctx) => {
    context = ctx;
    loadSetup();
    loadRoutes(true);
  });
})();
