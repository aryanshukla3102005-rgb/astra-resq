/* Rescue dashboard: polls the server, listens to the local mirror, merges, renders. */
(function () {
  "use strict";
  const A = window.ASTRA;
  const POLL_MS = 3000;
  const FLOW = A.STATUS_FLOW;
  const NEXT_LABEL = { assigned: "Mark en route", en_route: "Mark rescued" };
  const CREATE_FIELDS = ["id", "name", "phone", "message", "people_count", "severity", "lat", "lon",
    "accuracy_m", "navic_sats", "created_at", "source", "position_source"];

  const $ = (id) => document.getElementById(id);
  const listEl = $("sos-list");
  const records = new Map(); // id -> merged record
  const known = new Set();
  let teams = [];
  let initialised = false;
  let selectedId = null;
  let serverOk = null;
  let lastServerAt = 0;

  if (typeof L === "undefined") {
    $("map").innerHTML = '<div class="empty-state"><b>Map library failed to load</b>Leaflet is loaded from unpkg.com - check the internet connection.</div>';
  }
  const amap = typeof L !== "undefined" ? AstraMap.create($("map"), { onSelect: (id) => select(id, true) }) : null;

  // ---------------------------------------------------------------- merge
  function absorb(list) {
    const fresh = [];
    for (const rec of list || []) {
      if (!rec || !rec.id || typeof rec.lat !== "number" || typeof rec.lon !== "number") continue;
      const cur = records.get(rec.id);
      const win = A.pickNewer(cur, rec);
      if (win !== cur) records.set(rec.id, { ...(cur || {}), ...win, nearest_team: win.nearest_team || (cur && cur.nearest_team) });
      if (!known.has(rec.id)) { known.add(rec.id); fresh.push(rec.id); }
    }
    return fresh;
  }

  function announce(ids) {
    if (!initialised) return;
    ids.forEach((id) => {
      const s = records.get(id);
      if (!s || s.status === "rescued") return;
      A.toast(`New SOS: ${s.name} - ${s.severity.toUpperCase()}, ${s.people_count} ${s.people_count === 1 ? "person" : "people"}`, "sos", 7000);
      beep();
      setTimeout(() => {
        if (amap) amap.highlight(s);
        const card = listEl.querySelector(`[data-id="${CSS.escape(id)}"]`);
        if (card) card.classList.add("flash");
      }, 50);
    });
  }

  function sorted() {
    return [...records.values()].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  }

  // ---------------------------------------------------------------- server poll
  async function poll() {
    try {
      const list = await A.api("/api/sos", { timeout: 5000 });
      serverOk = true;
      lastServerAt = Date.now();
      const fresh = absorb(list);
      A.Sync.put(list, { broadcast: false }); // keep this browser's mirror current
      announce(fresh);
    } catch (e) {
      serverOk = false;
    }
    const freshLocal = absorb(Object.values(A.Sync.all()));
    announce(freshLocal);
    render();
    initialised = true;
    setTimeout(poll, POLL_MS);
  }

  A.Sync.onChange((recs) => {
    const fresh = absorb(recs || Object.values(A.Sync.all()));
    announce(fresh);
    render();
  });

  // ---------------------------------------------------------------- render
  function enrich(s) {
    if (!s.nearest_team && teams.length) s.nearest_team = A.nearestTeam(s.lat, s.lon, teams);
    return s;
  }

  function teamName(id) {
    const t = teams.find((x) => x.id === id);
    return t ? t.name : id;
  }

  function cardHtml(s) {
    const e = A.escapeHtml;
    const nearest = s.nearest_team;
    const byDist = teams.length ? A.teamsByDistance(s.lat, s.lon, teams) : [];
    const chosen = s.team_id || (nearest && nearest.id);
    const opts = byDist.map((t) => `<option value="${e(t.id)}"${t.id === chosen ? " selected" : ""}>${e(t.name)} (${t.distance_km.toFixed(1)} km)</option>`).join("");
    const assigned = s.team_id ? byDist.find((t) => t.id === s.team_id) : null;
    const src = s.position_source === "browser"
      ? `<span class="warnsrc">Source <b>Browser GPS</b></span>`
      : `<span class="navic">NavIC <b>${Number(s.navic_sats) || 0} sats</b></span>`;
    let actions = "";
    if (s.status !== "rescued") {
      actions = `<div class="actions">
        <select data-role="team" aria-label="Rescue team">${opts}</select>
        <button class="btn btn-sm${s.status === "new" ? " btn-primary" : ""}" data-act="assign">${s.team_id ? "Reassign" : "Assign"}</button>
        ${NEXT_LABEL[s.status] ? `<button class="btn btn-sm btn-teal" data-act="advance">${NEXT_LABEL[s.status]}</button>` : ""}
      </div>`;
    }
    return `
      <div class="top">
        <span class="pill sev-${e(s.severity)}">${e(s.severity)}</span>
        <span class="name">${e(s.name)}</span>
        <span class="ago" data-ago="${e(s.created_at)}">${A.timeAgo(s.created_at)}</span>
      </div>
      ${s.message ? `<div class="msg">${e(s.message)}</div>` : ""}
      <div class="meta">
        <span>People <b>${Number(s.people_count) || 1}</b></span>
        <span>Status <b class="st-${e(s.status)}">${e(A.STATUS_LABEL[s.status] || s.status)}</b></span>
        <span style="grid-column:1/-1"><b>${A.fmtCoord(s.lat, s.lon)}</b></span>
        <span>Accuracy <b>${s.accuracy_m != null ? "&plusmn;" + Number(s.accuracy_m).toFixed(1) + " m" : "-"}</b></span>
        ${src}
        ${nearest ? `<span style="grid-column:1/-1">Nearest <b>${e(nearest.name)}</b> ${Number(nearest.distance_km).toFixed(1)} km</span>` : ""}
        ${s.phone ? `<span style="grid-column:1/-1">Phone <b>${e(s.phone)}</b></span>` : ""}
      </div>
      ${assigned ? `<div class="assigned-to">Team: ${e(assigned.name)} &middot; ${assigned.distance_km.toFixed(1)} km away</div>` : ""}
      ${actions}`;
  }

  function render() {
    const all = sorted().map(enrich);
    // KPIs
    $("k-total").textContent = all.length;
    $("k-critical").textContent = all.filter((s) => s.severity === "critical" && s.status !== "rescued").length;
    $("k-unassigned").textContent = all.filter((s) => s.status === "new").length;
    $("k-rescued").textContent = all.filter((s) => s.status === "rescued").length;
    // sync indicator
    $("sync-server").className = "dot " + (serverOk === null ? "" : serverOk ? "ok" : "bad");
    $("sync-local").className = "dot " + (A.Sync.available ? "ok" : "bad");
    $("sync-time").textContent = lastServerAt ? `${Math.round((Date.now() - lastServerAt) / 1000)}s` : "";

    if (amap) amap.setSos(all);

    const filter = $("filter").value;
    const shown = all.filter((s) =>
      filter === "all" ? true :
      filter === "active" ? s.status !== "rescued" :
      s.status === filter);

    if (!shown.length) {
      listEl.innerHTML = all.length
        ? '<div class="empty-state"><b>Nothing in this view</b>Change the filter to see other alerts.</div>'
        : '<div class="empty-state"><b>No SOS received yet</b>Open the <a href="/sos" target="_blank" rel="noopener">victim device</a> in another window and press SOS.</div>';
      return;
    }
    listEl.querySelectorAll(".empty-state").forEach((n) => n.remove());

    const existing = new Map([...listEl.querySelectorAll(".sos-card")].map((n) => [n.dataset.id, n]));
    const wanted = new Set(shown.map((s) => s.id));
    existing.forEach((node, id) => { if (!wanted.has(id)) node.remove(); });

    let prev = null;
    for (const s of shown) {
      const sig = [s.status, s.team_id, s.severity, s.updated_at, teams.length, s.nearest_team && s.nearest_team.id].join("|");
      let node = existing.get(s.id);
      if (!node) {
        node = document.createElement("div");
        node.dataset.id = s.id;
      }
      const busy = node.contains(document.activeElement) && document.activeElement.tagName === "SELECT";
      if (node.dataset.sig !== sig && !busy) {
        node.innerHTML = cardHtml(s);
        node.dataset.sig = sig;
      }
      node.className = `sos-card sev-${s.severity} is-${s.status}${s.id === selectedId ? " selected" : ""}${node.classList.contains("flash") ? " flash" : ""}`;
      const want = prev ? prev.nextSibling : listEl.firstChild;
      if (node !== want) listEl.insertBefore(node, want);
      prev = node;
    }
  }

  setInterval(() => {
    listEl.querySelectorAll("[data-ago]").forEach((n) => { n.textContent = A.timeAgo(n.dataset.ago); });
    if (lastServerAt) $("sync-time").textContent = `${Math.round((Date.now() - lastServerAt) / 1000)}s`;
  }, 1000);

  // ---------------------------------------------------------------- actions
  function select(id, fromMap) {
    selectedId = id;
    const s = records.get(id);
    if (!s) return;
    if (amap && !fromMap) amap.flyTo(s);
    if (fromMap && $("filter").value !== "all" && !listEl.querySelector(`[data-id="${CSS.escape(id)}"]`)) $("filter").value = "all";
    render();
    const card = listEl.querySelector(`[data-id="${CSS.escape(id)}"]`);
    if (card && fromMap) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  /** Apply a change with the same rules as the server (core/store.py). Throws on invalid. */
  function localTransition(cur, change) {
    const next = { ...cur, history: [...(cur.history || [])] };
    let status = change.status;
    if (change.team_id != null) {
      if (cur.status === "rescued") throw new Error("cannot reassign a rescued SOS");
      next.team_id = change.team_id;
      if (!status && cur.status === "new") status = "assigned";
    }
    if (status && status !== cur.status) {
      const i = FLOW.indexOf(cur.status), j = FLOW.indexOf(status);
      if (j !== i + 1) throw new Error(`invalid transition ${cur.status} -> ${status}`);
      if (j >= 1 && !next.team_id) throw new Error("assign a team first");
      next.status = status;
      next.history.push({ status, at: A.nowIso() });
    }
    next.updated_at = A.nowIso();
    return next;
  }

  /** Bring the server's copy to `target` (re-creating it if this instance never saw it). */
  async function driveServer(target) {
    let srv;
    try {
      srv = await A.api(`/api/sos/${encodeURIComponent(target.id)}`);
    } catch (e) {
      if (e.status !== 404) throw e;
      const body = {};
      CREATE_FIELDS.forEach((k) => { if (target[k] != null && target[k] !== "") body[k] = target[k]; });
      srv = await A.api("/api/sos", { method: "POST", body });
    }
    if (target.team_id && srv.team_id !== target.team_id) {
      srv = await A.api(`/api/sos/${encodeURIComponent(target.id)}`, { method: "PATCH", body: { team_id: target.team_id } });
    }
    while (A.statusRank(srv.status) < A.statusRank(target.status)) {
      const step = FLOW[A.statusRank(srv.status) + 1];
      srv = await A.api(`/api/sos/${encodeURIComponent(target.id)}`, { method: "PATCH", body: { status: step } });
    }
    return srv;
  }

  async function change(id, delta) {
    const cur = records.get(id);
    if (!cur) return;
    let next;
    try { next = localTransition(cur, delta); } catch (e) { A.toast(e.message, "warn"); return; }
    // optimistic: local mirror first (reaches other tabs instantly), then the server
    records.set(id, next);
    A.Sync.put(next);
    render();
    try {
      let srv;
      try {
        srv = await A.api(`/api/sos/${encodeURIComponent(id)}`, { method: "PATCH", body: delta });
      } catch (e) {
        if (e.status !== 404 && e.status !== 409) throw e;
        srv = await driveServer(next); // serverless instance without this SOS, or stale copy
      }
      if (srv && srv.status === next.status && srv.team_id === next.team_id) {
        const adopted = { ...srv, updated_at: srv.updated_at > next.updated_at ? srv.updated_at : next.updated_at };
        records.set(id, adopted);
        A.Sync.put(adopted);
      }
      A.toast(delta.team_id ? `Assigned ${teamName(next.team_id)}` : `Status: ${A.STATUS_LABEL[next.status]}`, "ok", 2500);
    } catch (e) {
      A.toast(`Saved locally (server: ${e.message})`, "warn");
    }
    render();
  }

  listEl.addEventListener("click", (ev) => {
    const card = ev.target.closest(".sos-card");
    if (!card) return;
    const id = card.dataset.id;
    const btn = ev.target.closest("button[data-act]");
    if (btn) {
      ev.stopPropagation();
      const cur = records.get(id);
      if (btn.dataset.act === "assign") {
        const teamId = card.querySelector('select[data-role="team"]').value;
        if (cur.team_id === teamId) { A.toast("That team is already assigned", "warn", 2500); return; }
        change(id, { team_id: teamId });
      } else if (btn.dataset.act === "advance") {
        change(id, { status: FLOW[A.statusRank(cur.status) + 1] });
      }
      return;
    }
    if (ev.target.closest("select")) return;
    select(id, false);
  });

  $("filter").addEventListener("change", render);
  $("fit").addEventListener("click", () => amap && amap.fitAll(sorted()));
  $("reset-local").addEventListener("click", () => {
    if (!confirm("Clear this browser's local SOS mirror? Server data is not affected.")) return;
    A.Sync.clear();
    records.clear();
    known.clear();
    initialised = false; // don't toast for everything the server re-sends
    render();
  });

  // ---------------------------------------------------------------- sound (best effort)
  let audio = null;
  document.addEventListener("click", () => {
    if (!audio) try { audio = new (window.AudioContext || window.webkitAudioContext)(); } catch (_) { audio = null; }
  }, { once: true });
  function beep() {
    if (!audio) return;
    try {
      const o = audio.createOscillator(), g = audio.createGain();
      o.type = "square"; o.frequency.value = 880;
      g.gain.setValueAtTime(0.06, audio.currentTime);
      g.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + 0.35);
      o.connect(g).connect(audio.destination);
      o.start(); o.stop(audio.currentTime + 0.36);
    } catch (_) { /* ignore */ }
  }

  // ---------------------------------------------------------------- boot
  async function loadTeams() {
    try {
      teams = await A.api("/api/teams");
      $("team-count").textContent = teams.length;
      if (amap) amap.setTeams(teams);
      render();
    } catch (e) {
      setTimeout(loadTeams, 3000);
    }
  }
  loadTeams().then(poll);
})();
