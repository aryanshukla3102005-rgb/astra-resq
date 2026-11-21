/* ASTRA RESQ shared helpers: API, geo, formatting, toasts and the client sync mirror.
 *
 * Sync model (no database): the server keeps SOS records in memory. Because serverless
 * instances don't share memory, every SOS / status change is ALSO mirrored into
 * localStorage["astra.sos"] and broadcast on BroadcastChannel("astra-resq"). Consumers
 * merge both sources by id; the record with the later `updated_at` wins.
 */
(function () {
  "use strict";

  const SOS_KEY = "astra.sos";
  const CHANNEL_NAME = "astra-resq";
  const STATUS_FLOW = ["new", "assigned", "en_route", "rescued"];
  const STATUS_LABEL = { new: "New", assigned: "Assigned", en_route: "En route", rescued: "Rescued" };

  // ------------------------------------------------------------------ utils
  function uuid() {
    // crypto.randomUUID only exists in secure contexts (https / localhost). Phones on
    // http://192.168.x.x need the getRandomValues fallback.
    if (window.crypto && typeof crypto.randomUUID === "function") return crypto.randomUUID();
    const b = new Uint8Array(16);
    crypto.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  }

  function nowIso() {
    return new Date().toISOString();
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function timeAgo(iso) {
    const t = Date.parse(iso);
    if (isNaN(t)) return "-";
    const s = Math.max(0, Math.round((Date.now() - t) / 1000));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }

  function fmtCoord(lat, lon, digits = 5) {
    if (lat == null || lon == null) return "-";
    const ns = lat >= 0 ? "N" : "S";
    const ew = lon >= 0 ? "E" : "W";
    return `${Math.abs(lat).toFixed(digits)}°${ns} ${Math.abs(lon).toFixed(digits)}°${ew}`;
  }

  // ------------------------------------------------------------------ geo
  function haversineKm(lat1, lon1, lat2, lon2) {
    const R = 6371.0088;
    const r = Math.PI / 180;
    const dp = (lat2 - lat1) * r;
    const dl = (lon2 - lon1) * r;
    const a = Math.sin(dp / 2) ** 2 + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(dl / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
  }

  function teamsByDistance(lat, lon, teams) {
    return teams
      .map((t) => ({ ...t, distance_km: Math.round(haversineKm(lat, lon, t.lat, t.lon) * 100) / 100 }))
      .sort((a, b) => a.distance_km - b.distance_km);
  }

  function nearestTeam(lat, lon, teams) {
    const t = teamsByDistance(lat, lon, teams)[0];
    return t ? { id: t.id, name: t.name, distance_km: t.distance_km } : null;
  }

  // ------------------------------------------------------------------ API
  async function api(path, { method = "GET", body, timeout = 6000 } = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    try {
      const res = await fetch(path, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        signal: ctrl.signal,
        cache: "no-store",
      });
      let data = null;
      try { data = await res.json(); } catch (_) { /* non-JSON */ }
      if (!res.ok) {
        const err = new Error((data && data.error) || `HTTP ${res.status}`);
        err.status = res.status;
        err.data = data;
        throw err;
      }
      return data;
    } catch (e) {
      if (e.name === "AbortError") { const err = new Error("request timed out"); err.status = 0; throw err; }
      if (e.status === undefined) e.status = 0; // network failure
      throw e;
    } finally {
      clearTimeout(timer);
    }
  }

  // ------------------------------------------------------------------ storage (always try/catch)
  function lsGet(key, fallback) {
    try {
      const v = localStorage.getItem(key);
      return v == null ? fallback : JSON.parse(v);
    } catch (_) {
      return fallback;
    }
  }
  function lsSet(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch (_) { return false; }
  }

  // ------------------------------------------------------------------ sync mirror
  function statusRank(s) { return Math.max(0, STATUS_FLOW.indexOf(s)); }

  /** Pick the winner of two versions of the same SOS (later updated_at; tie -> further status). */
  function pickNewer(a, b) {
    if (!a) return b;
    if (!b) return a;
    const ta = Date.parse(a.updated_at || a.created_at || 0) || 0;
    const tb = Date.parse(b.updated_at || b.created_at || 0) || 0;
    if (ta !== tb) return tb > ta ? b : a;
    return statusRank(b.status) > statusRank(a.status) ? b : a;
  }

  const listeners = new Set();
  let channel = null;
  try { channel = "BroadcastChannel" in window ? new BroadcastChannel(CHANNEL_NAME) : null; } catch (_) { channel = null; }

  const Sync = {
    available: (() => { try { const k = "__astra"; localStorage.setItem(k, "1"); localStorage.removeItem(k); return true; } catch (_) { return false; } })(),
    channel: !!channel,

    all() {
      const m = lsGet(SOS_KEY, {});
      return m && typeof m === "object" && !Array.isArray(m) ? m : {};
    },

    get(id) { return this.all()[id] || null; },

    /** Merge records into the mirror. Returns the ids that changed. broadcast=false for passive writes. */
    put(records, { broadcast = true } = {}) {
      const list = Array.isArray(records) ? records : [records];
      const map = this.all();
      const changed = [];
      for (const rec of list) {
        if (!rec || !rec.id) continue;
        const win = pickNewer(map[rec.id], rec);
        if (win !== map[rec.id] && JSON.stringify(win) !== JSON.stringify(map[rec.id])) {
          map[rec.id] = win;
          changed.push(rec.id);
        }
      }
      if (changed.length) {
        lsSet(SOS_KEY, map);
        if (broadcast && channel) {
          try { channel.postMessage({ type: "sos", records: changed.map((id) => map[id]) }); } catch (_) { /* ignore */ }
        }
      }
      return changed;
    },

    clear() {
      try { localStorage.removeItem(SOS_KEY); } catch (_) { /* ignore */ }
      if (channel) try { channel.postMessage({ type: "clear" }); } catch (_) { /* ignore */ }
    },

    /** cb(records|null) - called when another tab/window changes the mirror. null = re-read all. */
    onChange(cb) { listeners.add(cb); return () => listeners.delete(cb); },
  };

  function emit(records) { listeners.forEach((cb) => { try { cb(records); } catch (e) { console.error(e); } }); }

  if (channel) {
    channel.onmessage = (ev) => {
      const msg = ev.data || {};
      if (msg.type === "sos" && Array.isArray(msg.records)) emit(msg.records);
      else if (msg.type === "clear") emit(null);
    };
  }
  window.addEventListener("storage", (ev) => {
    if (ev.key === SOS_KEY || ev.key === null) emit(null);
  });

  // ------------------------------------------------------------------ toasts
  function toast(text, kind = "", ms = 4500) {
    let box = document.getElementById("toasts");
    if (!box) {
      box = document.createElement("div");
      box.id = "toasts";
      document.body.appendChild(box);
    }
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = text;
    box.appendChild(el);
    setTimeout(() => { el.classList.add("fade"); setTimeout(() => el.remove(), 350); }, ms);
  }

  window.ASTRA = {
    STATUS_FLOW, STATUS_LABEL, SOS_KEY, CHANNEL_NAME,
    uuid, nowIso, escapeHtml, timeAgo, fmtCoord,
    haversineKm, teamsByDistance, nearestTeam,
    api, lsGet, lsSet, pickNewer, statusRank, Sync, toast,
  };
})();
