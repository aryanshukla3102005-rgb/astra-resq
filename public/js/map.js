/* Leaflet map for the rescue dashboard: Bhuvan (ISRO) base layer with OSM fallback. */
(function () {
  "use strict";

  // Base-layer config in one place.
  // NOTE: the Bhuvan WMS endpoint / layer name below was correct at the time of writing,
  // but NRSC changes these from time to time - verify at https://bhuvan.nrsc.gov.in if
  // tiles stop loading. The dashboard auto-falls back to OSM after repeated tile errors.
  const MAP_CONFIG = {
    center: [30.62, 79.0],
    zoom: 9,
    bhuvan: {
      url: "https://bhuvan-vec1.nrsc.gov.in/bhuvan/gwc/service/wms/",
      options: {
        layers: "india3",
        format: "image/png",
        transparent: true,
        version: "1.1.1",
        attribution: 'Map &copy; <a href="https://bhuvan.nrsc.gov.in" target="_blank" rel="noopener">Bhuvan, ISRO/NRSC</a>',
      },
    },
    osm: {
      url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      options: {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
      },
    },
    fallback: { minErrors: 6 }, // switch to OSM once errors >= this and errors outnumber loads
  };

  const SEV_COLOR = { critical: "#ff4d4d", medium: "#ff8a1f", low: "#e2c84b" };
  const TEAM_ABBR = { ndrf: "NDRF", sdrf: "SDRF", itbp: "ITBP", police: "POL", medical: "MED", air: "AIR" };

  function create(el, { onSelect } = {}) {
    const map = L.map(el, { zoomControl: true, preferCanvas: false }).setView(MAP_CONFIG.center, MAP_CONFIG.zoom);

    const bhuvan = L.tileLayer.wms(MAP_CONFIG.bhuvan.url, MAP_CONFIG.bhuvan.options);
    const osm = L.tileLayer(MAP_CONFIG.osm.url, MAP_CONFIG.osm.options);
    bhuvan.addTo(map);
    L.control.layers({ "Bhuvan (ISRO)": bhuvan, "OpenStreetMap": osm }, null, { position: "topright" }).addTo(map);
    L.control.scale({ imperial: false }).addTo(map);

    // --- automatic fallback when Bhuvan is unreachable
    let errors = 0, loads = 0, fellBack = false;
    bhuvan.on("tileload", () => { loads++; });
    bhuvan.on("tileerror", () => {
      errors++;
      if (!fellBack && map.hasLayer(bhuvan) && errors >= MAP_CONFIG.fallback.minErrors && errors > loads) {
        fellBack = true;
        map.removeLayer(bhuvan);
        osm.addTo(map);
        notice("Bhuvan tiles unavailable - switched to OpenStreetMap");
      }
    });

    function notice(text) {
      const n = document.createElement("div");
      n.className = "map-notice";
      n.textContent = text;
      el.parentElement.appendChild(n);
      setTimeout(() => n.remove(), 8000);
    }

    const teamLayer = L.layerGroup().addTo(map);
    const lineLayer = L.layerGroup().addTo(map);
    const sosLayer = L.layerGroup().addTo(map);
    const sosMarkers = new Map(); // id -> {marker, sig}
    const teamPos = new Map();
    let fitted = false;

    function setTeams(teams) {
      teamLayer.clearLayers();
      teams.forEach((t) => {
        teamPos.set(t.id, [t.lat, t.lon]);
        const icon = L.divIcon({ className: "team-pin", html: TEAM_ABBR[t.type] || "TEAM", iconSize: [34, 22] });
        L.marker([t.lat, t.lon], { icon, zIndexOffset: -100, keyboard: false })
          .bindPopup(`<b>${ASTRA.escapeHtml(t.name)}</b><br>${ASTRA.escapeHtml(t.base)} &middot; ${t.members} personnel<br><span class="mono">${ASTRA.escapeHtml(t.phone || "")}</span>`)
          .addTo(teamLayer);
      });
    }

    function sosIcon(s, highlight) {
      const color = s.status === "rescued" ? "#3ccf7a" : SEV_COLOR[s.severity] || "#ff8a1f";
      return L.divIcon({
        className: "",
        iconSize: [24, 24],
        iconAnchor: [12, 12],
        html: `<div class="sos-pin status-${s.status}${highlight ? " highlight" : ""}" style="color:${color};width:24px;height:24px"><span class="ring"></span><span class="core"></span></div>`,
      });
    }

    function popupHtml(s) {
      const e = ASTRA.escapeHtml;
      return `<b>${e(s.name)}</b> <span class="sev-${s.severity}">${e(s.severity.toUpperCase())}</span><br>` +
        `${s.people_count} people &middot; ${e(ASTRA.STATUS_LABEL[s.status] || s.status)}<br>` +
        `<span class="mono">${ASTRA.fmtCoord(s.lat, s.lon)}</span>` +
        (s.message ? `<br><i>${e(s.message)}</i>` : "");
    }

    /** Render the SOS list (already merged). Keeps markers keyed so pulsing isn't reset each poll. */
    function setSos(list) {
      const seen = new Set();
      lineLayer.clearLayers();
      list.forEach((s) => {
        seen.add(s.id);
        const sig = `${s.status}|${s.severity}|${s.lat}|${s.lon}`;
        let entry = sosMarkers.get(s.id);
        if (!entry) {
          const marker = L.marker([s.lat, s.lon], { icon: sosIcon(s), zIndexOffset: 500 }).addTo(sosLayer);
          marker.on("click", () => onSelect && onSelect(s.id));
          entry = { marker, sig };
          sosMarkers.set(s.id, entry);
        } else if (entry.sig !== sig) {
          entry.marker.setLatLng([s.lat, s.lon]).setIcon(sosIcon(s));
          entry.sig = sig;
        }
        const html = popupHtml(s);
        if (entry.popup !== html) {
          if (entry.marker.getPopup()) entry.marker.setPopupContent(html);
          else entry.marker.bindPopup(html);
          entry.popup = html;
        }
        if (s.team_id && teamPos.has(s.team_id) && s.status !== "rescued") {
          L.polyline([[s.lat, s.lon], teamPos.get(s.team_id)], {
            color: s.status === "en_route" ? "#1ec8b4" : "#8ab4ff",
            weight: 2, dashArray: "6 6", opacity: 0.9,
          }).addTo(lineLayer);
        }
      });
      for (const [id, entry] of sosMarkers) {
        if (!seen.has(id)) { sosLayer.removeLayer(entry.marker); sosMarkers.delete(id); }
      }
      if (!fitted && list.length) {
        fitted = true;
        const pts = list.map((s) => [s.lat, s.lon]).concat([...teamPos.values()]);
        map.fitBounds(L.latLngBounds(pts).pad(0.15), { maxZoom: 12 });
      }
    }

    function flyTo(s) {
      map.flyTo([s.lat, s.lon], Math.max(map.getZoom(), 14), { duration: 0.8 });
      const entry = sosMarkers.get(s.id);
      if (entry) setTimeout(() => entry.marker.openPopup(), 850);
    }

    function highlight(s) {
      const entry = sosMarkers.get(s.id);
      if (!entry) return;
      entry.marker.setIcon(sosIcon(s, true));
      setTimeout(() => entry.marker.setIcon(sosIcon(s)), 2600);
    }

    function fitAll(list) {
      const pts = list.map((s) => [s.lat, s.lon]).concat([...teamPos.values()]);
      if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.15), { maxZoom: 12 });
    }

    // Leaflet needs a size recalculation if the container changes (responsive layout)
    window.addEventListener("resize", () => map.invalidateSize());

    return { map, setTeams, setSos, flyTo, highlight, fitAll, config: MAP_CONFIG };
  }

  window.AstraMap = { create, MAP_CONFIG };
})();
