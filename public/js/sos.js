/* Victim device simulator: NMEA replay, SOS form, offline store-and-forward queue. */
(function () {
  "use strict";
  const A = window.ASTRA;
  const $ = (id) => document.getElementById(id);

  const QUEUE_KEY = "astra.queue";
  const MINE_KEY = "astra.mine";
  const NET_KEY = "astra.network";
  const PROFILE_KEY = "astra.profile";
  const TERMINAL_LINES = 40;

  let fixes = [];
  let idx = 0;
  let firstValid = 0;
  let navicFix = null;     // latest replayed fix
  let browserFix = null;   // latest browser geolocation
  let source = "navic";
  let geoWatch = null;
  let teams = [];
  let networkOn = A.lsGet(NET_KEY, "on") !== "off";
  let flushing = false;

  // ================================================================= receiver / replay
  const term = $("terminal");
  function termLine(text, cls) {
    const div = document.createElement("div");
    const star = text.lastIndexOf("*");
    div.className = cls;
    if (star > 0) {
      div.appendChild(document.createTextNode(text.slice(0, star)));
      const cs = document.createElement("span");
      cs.className = "cs";
      cs.textContent = text.slice(star);
      div.appendChild(cs);
    } else {
      div.textContent = text;
    }
    term.appendChild(div);
    while (term.childElementCount > TERMINAL_LINES) term.firstElementChild.remove();
  }

  async function loadReplay() {
    termLine("> opening NavIC receiver (replay: data/navic_sample.nmea)", "t-sys");
    try {
      fixes = await A.api("/api/nmea/replay", { timeout: 10000 });
      if (!fixes.length) throw new Error("empty replay");
      firstValid = Math.max(0, fixes.findIndex((f) => f.valid));
      termLine(`> ${fixes.length} epochs loaded, streaming at 1 Hz`, "t-sys");
      tick();
      setInterval(tick, 1000);
    } catch (e) {
      termLine(`> receiver error: ${e.message} - retrying in 3 s`, "t-sys");
      setTimeout(loadReplay, 3000);
    }
  }

  function tick() {
    const f = fixes[idx];
    f.sentences.forEach((s) => termLine(s, "t-" + s.slice(1, 3)));
    navicFix = f;
    $("epoch").textContent = `epoch ${idx + 1}/${fixes.length}`;
    idx++;
    if (idx >= fixes.length) {
      // loop, but skip the cold-start acquisition epochs after the first pass
      idx = firstValid;
      termLine("> end of replay file - looping (warm start)", "t-sys");
    }
    renderSats(f);
    if (source === "navic") renderFix();
  }

  function currentFix() {
    if (source === "browser") return browserFix;
    if (!navicFix) return null;
    return {
      valid: navicFix.valid,
      lat: navicFix.lat,
      lon: navicFix.lon,
      accuracy_m: navicFix.accuracy_m,
      navic_sats: navicFix.navic_in_view,
      gps_sats: navicFix.gps_in_view,
      sats_used: navicFix.sats_used,
      hdop: navicFix.hdop,
      altitude_m: navicFix.altitude_m,
      timestamp: navicFix.timestamp,
      position_source: "navic",
    };
  }

  function renderFix() {
    const fx = currentFix();
    const banner = $("fix-banner");
    const nf = navicFix;
    $("s-navic").textContent = nf ? nf.navic_in_view : "-";
    $("s-gps").textContent = nf ? nf.gps_in_view : "-";
    if (source === "browser") {
      $("fix-src").textContent = "Browser / OS location (not NavIC)";
      $("s-used").textContent = "-";
      $("s-hdop").textContent = "-";
      $("s-alt").textContent = fx && fx.altitude_m != null ? `${Math.round(fx.altitude_m)} m` : "-";
      $("s-time").textContent = fx ? new Date().toISOString().slice(11, 19) : "-";
    } else {
      $("fix-src").textContent = "NavIC + GPS";
      $("s-used").textContent = nf ? nf.sats_used : "-";
      $("s-hdop").textContent = nf && nf.valid && nf.hdop != null ? nf.hdop.toFixed(2) : "-";
      $("s-alt").textContent = nf && nf.altitude_m != null ? `${Math.round(nf.altitude_m)} m` : "-";
      $("s-time").textContent = nf && nf.timestamp ? nf.timestamp.slice(11, 19) : "-";
    }
    if (fx && fx.valid) {
      banner.className = "fix-banner ok";
      $("fix-text").textContent = source === "browser" ? "Position fix (browser)" : `3D fix - NavIC ${fx.navic_sats} sats`;
      $("s-pos").textContent = A.fmtCoord(fx.lat, fx.lon, 5);
      $("s-acc").textContent = fx.accuracy_m != null ? `±${Number(fx.accuracy_m).toFixed(1)} m` : "-";
    } else {
      banner.className = "fix-banner acq";
      $("fix-text").textContent = source === "browser" ? "Waiting for browser location..." : "Acquiring satellites - no fix yet";
      $("s-pos").textContent = "-";
      $("s-acc").textContent = "-";
    }
    updateSosButton();
  }

  function renderSats(f) {
    const navic = f.navic_sats.map((s) => ({ ...s, sys: "navic", label: "I" + String(s.prn).padStart(2, "0") }));
    const gps = f.gps_sats.map((s) => ({ ...s, sys: "gps", label: "G" + String(s.prn).padStart(2, "0") }));
    const all = navic.concat(gps);

    // sky plot: centre = zenith, outer ring = horizon
    const C = 100, R = 88;
    let svg = "";
    [0, 30, 60].forEach((el) => { svg += `<circle class="ring" cx="${C}" cy="${C}" r="${(R * (90 - el)) / 90}"/>`; });
    svg += `<line x1="${C}" y1="${C - R}" x2="${C}" y2="${C + R}"/><line x1="${C - R}" y1="${C}" x2="${C + R}" y2="${C}"/>`;
    svg += `<text x="${C - 3}" y="9">N</text><text x="${C + R + 3}" y="${C + 3}">E</text><text x="${C - 3}" y="199">S</text><text x="1" y="${C + 3}">W</text>`;
    all.forEach((s) => {
      if (s.elev == null || s.az == null) return;
      const r = (R * (90 - s.elev)) / 90;
      const a = (s.az * Math.PI) / 180;
      const x = C + r * Math.sin(a), y = C - r * Math.cos(a);
      const weak = s.snr == null || s.snr < 25 ? " sat-weak" : "";
      svg += `<circle class="sat-${s.sys}${weak}" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="5.5"/>`;
      svg += `<text class="sat-label" x="${(x + 7).toFixed(1)}" y="${(y + 3).toFixed(1)}">${s.label}</text>`;
    });
    $("skyplot").innerHTML = svg;

    $("snr-bars").innerHTML = all.map((s) => {
      const h = s.snr == null ? 0 : Math.min(100, (s.snr / 50) * 100);
      return `<div class="snr-bar ${s.sys}${s.snr == null ? " empty" : ""}" title="${s.label}: ${s.snr == null ? "no signal" : s.snr + " dB-Hz"}"><small>${s.snr == null ? "" : s.snr}</small><i style="height:${h}%"></i></div>`;
    }).join("");
    $("snr-labels").innerHTML = all.map((s) => `<span>${s.label}</span>`).join("");
  }

  // browser geolocation fallback
  function startBrowserGeo() {
    if (!("geolocation" in navigator)) { setMsg("This browser has no geolocation API.", "err"); return; }
    if (!window.isSecureContext) {
      setMsg("Browser location needs HTTPS or localhost. Over plain http on a LAN IP, use the NavIC replay.", "warn");
    }
    geoWatch = navigator.geolocation.watchPosition(
      (pos) => {
        browserFix = {
          valid: true, lat: pos.coords.latitude, lon: pos.coords.longitude,
          accuracy_m: Math.round(pos.coords.accuracy * 10) / 10,
          altitude_m: pos.coords.altitude, navic_sats: 0, position_source: "browser",
        };
        renderFix();
      },
      (err) => { setMsg(`Browser location unavailable: ${err.message}`, "err"); renderFix(); },
      { enableHighAccuracy: true, maximumAge: 5000, timeout: 20000 }
    );
  }
  function stopBrowserGeo() {
    if (geoWatch != null) navigator.geolocation.clearWatch(geoWatch);
    geoWatch = null;
  }

  document.querySelectorAll('input[name="src"]').forEach((r) => r.addEventListener("change", (e) => {
    source = e.target.value;
    $("browser-note").classList.toggle("hidden", source !== "browser");
    if (source === "browser") startBrowserGeo(); else { stopBrowserGeo(); setMsg(""); }
    renderFix();
  }));

  // ================================================================= form
  const form = $("sos-form");
  const btn = $("sos-btn");

  function setMsg(text, kind) {
    const m = $("form-msg");
    m.textContent = text || "";
    m.className = "form-msg " + (kind || "");
  }

  function updateSosButton() {
    const fx = currentFix();
    const ok = !!(fx && fx.valid);
    btn.disabled = !ok;
    $("sos-hint").textContent = ok
      ? (networkOn ? "Press to send your position to rescue teams" : "No network - SOS will be queued on this device")
      : "Waiting for a position fix...";
  }

  // remember name/phone for repeat demos
  const profile = A.lsGet(PROFILE_KEY, {});
  if (profile.name) $("f-name").value = profile.name;
  if (profile.phone) $("f-phone").value = profile.phone;

  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const fx = currentFix();
    if (!fx || !fx.valid) { setMsg("No position fix yet.", "err"); return; }
    const name = $("f-name").value.trim();
    const phone = $("f-phone").value.trim();
    const people = parseInt($("f-people").value, 10);
    if (!name) { setMsg("Please enter your name.", "err"); $("f-name").focus(); return; }
    if (phone && !/^[0-9+()\- ]{3,20}$/.test(phone)) { setMsg("Phone may contain only digits, spaces and + - ( ).", "err"); return; }
    if (!Number.isInteger(people) || people < 1 || people > 500) { setMsg("People count must be 1-500.", "err"); return; }
    A.lsSet(PROFILE_KEY, { name, phone });

    const body = {
      id: A.uuid(),
      name, phone,
      message: $("f-msg").value.trim(),
      people_count: people,
      severity: form.querySelector('input[name="severity"]:checked').value,
      lat: Math.round(fx.lat * 1e7) / 1e7,
      lon: Math.round(fx.lon * 1e7) / 1e7,
      accuracy_m: fx.accuracy_m != null ? Number(fx.accuracy_m) : null,
      navic_sats: source === "navic" ? fx.navic_sats : 0,
      created_at: A.nowIso(),
      source: "simulator",
      position_source: fx.position_source,
    };
    if (body.accuracy_m == null) delete body.accuracy_m;

    enqueue(body);
    addMine(body);
    $("f-msg").value = "";
    if (networkOn) {
      setMsg("Sending SOS...", "ok");
      mirror(body);
      flushQueue();
    } else {
      setMsg("Queued - will send when connectivity returns.", "warn");
      A.toast("No network: SOS stored on device", "warn");
    }
    renderLog();
  });

  // ================================================================= offline queue
  function getQueue() { const q = A.lsGet(QUEUE_KEY, []); return Array.isArray(q) ? q : []; }
  function setQueue(q) { A.lsSet(QUEUE_KEY, q); renderQueueBadge(); }
  function enqueue(body) { const q = getQueue(); q.push({ body, attempts: 0, next_at: 0 }); setQueue(q); }

  function getMine() { const m = A.lsGet(MINE_KEY, []); return Array.isArray(m) ? m : []; }
  function setMine(m) { A.lsSet(MINE_KEY, m.slice(-20)); }
  function addMine(body) {
    const m = getMine();
    m.push({ id: body.id, name: body.name, severity: body.severity, created_at: body.created_at, state: "queued" });
    setMine(m);
  }
  function setMineState(id, patch) {
    const m = getMine();
    const it = m.find((x) => x.id === id);
    if (it) { Object.assign(it, patch); setMine(m); }
  }

  /** Client mirror: localStorage + BroadcastChannel (reaches dashboards in this browser). */
  function mirror(body) {
    A.Sync.put({
      ...body,
      status: "new", team_id: null,
      updated_at: body.created_at,
      history: [{ status: "new", at: body.created_at }],
    });
  }

  async function flushQueue() {
    if (flushing || !networkOn) return;
    flushing = true;
    try {
      let q = getQueue();
      for (const item of q.slice()) {
        if (!networkOn) break;
        if (item.next_at > Date.now()) continue;
        setMineState(item.body.id, { state: "sending" });
        renderLog();
        try {
          const rec = await A.api("/api/sos", { method: "POST", body: item.body, timeout: 8000 });
          q = getQueue().filter((x) => x.body.id !== item.body.id);
          setQueue(q);
          setMineState(item.body.id, { state: "delivered", delivered_at: A.nowIso() });
          A.Sync.put(rec);
          setMsg("SOS delivered to rescue control.", "ok");
          A.toast(`SOS delivered (${item.body.severity})`, "ok");
        } catch (e) {
          q = getQueue();
          const it = q.find((x) => x.body.id === item.body.id);
          const permanent = e.status >= 400 && e.status < 500 && e.status !== 408 && e.status !== 429;
          if (permanent) {
            q = q.filter((x) => x.body.id !== item.body.id);
            setMineState(item.body.id, { state: "failed", error: e.message });
            setMsg(`SOS rejected: ${e.message}`, "err");
          } else if (it) {
            it.attempts += 1;
            const wait = Math.min(30, 2 ** it.attempts);
            it.next_at = Date.now() + wait * 1000;
            setMineState(item.body.id, { state: "queued", error: `retry in ${wait}s (${e.message})` });
          }
          setQueue(q);
        }
        renderLog();
      }
    } finally {
      flushing = false;
    }
  }

  // ================================================================= network toggle
  function renderNet() {
    $("net").classList.toggle("off", !networkOn);
    $("net-toggle").checked = networkOn;
    $("net-state").innerHTML = networkOn
      ? "NETWORK: ONLINE<small>Messages are delivered immediately</small>"
      : "NETWORK: NO SIGNAL<small>SOS is stored on the device and forwarded later</small>";
    updateSosButton();
  }

  $("net-toggle").addEventListener("change", (e) => {
    networkOn = e.target.checked;
    A.lsSet(NET_KEY, networkOn ? "on" : "off");
    renderNet();
    if (networkOn) {
      const q = getQueue();
      if (q.length) {
        A.toast(`Connectivity restored - forwarding ${q.length} queued SOS`, "ok");
        setMsg(`Connectivity restored - sending ${q.length} queued SOS...`, "ok");
        q.forEach((item) => { item.next_at = 0; mirror(item.body); });
        setQueue(q);
      }
      flushQueue();
    } else {
      setMsg("Network off. New SOS will be queued.", "warn");
    }
  });
  window.addEventListener("online", () => { if (networkOn) flushQueue(); });

  // ================================================================= my alerts log
  function renderQueueBadge() {
    const n = getQueue().length;
    const b = $("queue-badge");
    b.textContent = `${n} queued`;
    b.classList.toggle("has", n > 0);
  }

  const STATE_LABEL = { queued: "Queued", sending: "Sending", delivered: "Delivered", failed: "Failed" };
  function renderLog() {
    renderQueueBadge();
    const mine = getMine().slice().reverse();
    const el = $("mylog");
    if (!mine.length) { el.innerHTML = '<div class="mylog-empty">No alerts sent from this device yet.</div>'; return; }
    const e = A.escapeHtml;
    el.innerHTML = mine.map((m) => {
      const rec = networkOn ? A.Sync.get(m.id) : null;
      if (rec && rec.status) { m.last_status = rec.status; m.team_id = rec.team_id; }
      const st = m.last_status || "new";
      const team = teams.find((t) => t.id === m.team_id);
      let help = "";
      if (m.state === "delivered" || rec) {
        help = st === "new" ? "Received by rescue control - awaiting team assignment"
          : st === "assigned" ? `Team assigned: ${team ? team.name : m.team_id}`
          : st === "en_route" ? `Help is on the way: ${team ? team.name : m.team_id}`
          : "Marked rescued";
      }
      const pillCls = m.state === "delivered" ? "st-rescued" : m.state === "failed" ? "sev-critical" : "st-new";
      return `<div class="mylog-item">
        <div class="row"><span><span class="pill sev-${e(m.severity)}">${e(m.severity)}</span> ${e(m.name)} <span class="dim mono">${A.timeAgo(m.created_at)}</span></span>
        <span class="pill ${pillCls}">${STATE_LABEL[m.state] || m.state}</span></div>
        ${help ? `<div class="help">${e(help)}</div>` : ""}
        ${m.error && m.state !== "delivered" ? `<div class="dim" style="font-size:12px;margin-top:4px">${e(m.error)}</div>` : ""}
      </div>`;
    }).join("");
    setMine(getMine().map((x) => { const r = mine.find((y) => y.id === x.id); return r ? { ...x, last_status: r.last_status, team_id: r.team_id } : x; }));
  }

  // status updates: dashboard in this browser (mirror) + server poll (other devices)
  A.Sync.onChange(() => { if (networkOn) renderLog(); });

  async function pollMine() {
    if (networkOn) {
      const ids = getMine().filter((m) => m.state === "delivered" && m.last_status !== "rescued").map((m) => m.id);
      for (const id of ids) {
        try { A.Sync.put(await A.api(`/api/sos/${encodeURIComponent(id)}`), { broadcast: false }); } catch (_) { /* serverless miss / offline */ }
      }
      renderLog();
    }
    setTimeout(pollMine, 5000);
  }

  // ================================================================= boot
  renderNet();
  renderLog();
  loadReplay();
  A.api("/api/teams").then((t) => { teams = t; renderLog(); }).catch(() => {});
  setInterval(() => { if (networkOn && getQueue().length) flushQueue(); }, 3000);
  if (networkOn && getQueue().length) flushQueue();
  setTimeout(pollMine, 5000);
})();
