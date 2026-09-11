/* Rally Finder — map + list over the Roadsurfer Rally one-way routes. */

const API = "/api";

const state = {
  offers: [],
  stations: [],
  models: [],
  filtered: [],
  selected: null,     // offer opened in the detail modal
  focus: null,        // {id, kind} — station whose connections are shown
  hoverKey: null,     // offer key highlighted from the list
  map: null,
  layers: { arcs: null, markers: null },
  markerIndex: new Map(),
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/* Why a listed window turns out not to be bookable. Roadsurfer's calendar
   advertises these; the booking step is where they fall over. */
const DEAD_REASONS = {
  invalid_booking_end_station: {
    label: "Route refused",
    detail:
      "Roadsurfer lists this destination for this departure, but the booking " +
      "step rejects the pair outright (invalid_booking_end_station). The " +
      "route is advertised in the calendar yet cannot be booked at all.",
  },
  no_vehicles_offered: {
    label: "No camper offered",
    detail:
      "The route and dates are valid, but the booking step returns no vehicle " +
      "at all for this window — nothing is allocated to reposition.",
  },
  all_vehicles_taken: {
    label: "All taken",
    detail:
      "Campers exist for this window but every one is already booked.",
  },
};

/* ── helpers ───────────────────────────────────────────────────────────── */

const flag = (cc) =>
  !cc || cc.length !== 2
    ? ""
    : String.fromCodePoint(
        ...[...cc.toUpperCase()].map((c) => 0x1f1a5 + c.charCodeAt(0))
      );

const fmtDate = (iso) =>
  new Date(iso + "T00:00:00").toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
  });

const fmtDateLong = (iso) =>
  new Date(iso + "T00:00:00").toLocaleDateString("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "long",
    year: "numeric",
  });

const monthKey = (iso) =>
  new Date(iso + "T00:00:00").toLocaleDateString("en-GB", {
    month: "long",
    year: "numeric",
  });

const fmtPrice = (v, cur) =>
  v == null ? "—" : `${Math.round(v)} ${cur === "EUR" ? "€" : cur || "€"}`;

const hhmm = (t) => (t ? t.slice(0, 5) : null);

const isNew = (offer, hours = 24) =>
  offer.first_seen &&
  Date.now() - new Date(offer.first_seen).getTime() < hours * 3600 * 1000;

const daysUntil = (iso) =>
  Math.round((new Date(iso + "T00:00:00") - new Date()) / 86400000);

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

/* ── data ──────────────────────────────────────────────────────────────── */

async function load() {
  // Dead ends are fetched too, then filtered client-side, so toggling them on
  // is instant and costs no extra round trip.
  const [offers, stations, stats, models] = await Promise.all([
    fetch(`${API}/offers?include_unbookable=true`).then((r) => r.json()),
    fetch(`${API}/stations`).then((r) => r.json()),
    fetch(`${API}/stats`).then((r) => r.json()),
    fetch(`${API}/models`).then((r) => r.json()),
  ]);
  state.offers = offers;
  state.stations = stations;
  state.models = models;
  renderStats(stats);
  populateFilters();
  populateModels();
  applyFilters();
}

/** Camper checkboxes, shared shape between the filter bar and the alert form. */
function modelCheckboxes(container, namePrefix) {
  container.innerHTML = state.models
    .map((m, i) => {
      const spec = [
        m.seats ? `${m.seats} seats` : null,
        m.beds ? `${m.beds} beds` : null,
      ]
        .filter(Boolean)
        .join(" · ");
      const none = m.available_now === 0;
      return `<label class="model-opt ${none ? "is-none" : ""}" title="${
        none ? "Not on any trip right now" : `${m.available_now} trips right now`
      }">
        <input type="checkbox" value="${escapeHtml(m.name)}" id="${namePrefix}-${i}">
        <span class="model-name">${escapeHtml(m.name)}</span>
        <span class="model-spec">${spec}${
        none ? "" : ` · ${m.available_now}`
      }</span>
      </label>`;
    })
    .join("");
}

/** Place a fixed-position dropdown panel under its summary, kept on screen. */
function positionDropdown(dd) {
  const panel = dd.querySelector(".dropdown-panel");
  const summary = dd.querySelector("summary");
  if (!panel || !summary) return;
  const r = summary.getBoundingClientRect();
  const margin = 8;

  if (window.innerWidth < 860) {
    panel.style.left = margin + "px";
    panel.style.right = margin + "px";
    panel.style.width = "auto";
  } else {
    panel.style.right = "auto";
    panel.style.width = "";
    const width = panel.offsetWidth || 250;
    // Keep the panel inside the viewport when its field sits near the edge.
    const left = Math.min(r.left, window.innerWidth - width - margin);
    panel.style.left = Math.max(margin, left) + "px";
  }

  // Flip above the field if there is not enough room below.
  const below = window.innerHeight - r.bottom - margin;
  const height = panel.offsetHeight;
  if (below < height && r.top > below) {
    panel.style.top = Math.max(margin, r.top - height - 4) + "px";
  } else {
    panel.style.top = r.bottom + 4 + "px";
  }
}

function wireDropdown(dd) {
  dd.addEventListener("toggle", () => {
    if (dd.open) positionDropdown(dd);
  });
  // Reposition rather than drift when the page moves underneath it.
  for (const evt of ["resize", "scroll"]) {
    window.addEventListener(
      evt,
      () => dd.open && positionDropdown(dd),
      { passive: true, capture: evt === "scroll" }
    );
  }
  document.addEventListener("click", (e) => {
    if (dd.open && !dd.contains(e.target)) dd.open = false;
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && dd.open) dd.open = false;
  });
}

function populateModels() {
  modelCheckboxes($("#f-models"), "fm");
  $$("#f-models input").forEach((cb) =>
    cb.addEventListener("change", () => {
      syncModelSummary();
      applyFilters();
    })
  );
  syncModelSummary();
}

function selectedModels(root) {
  return $$(`${root} input:checked`).map((cb) => cb.value);
}

function syncModelSummary() {
  const picked = selectedModels("#f-models");
  $("#f-models-toggle").textContent = !picked.length
    ? "Any camper"
    : picked.length === 1
    ? picked[0]
    : `${picked.length} campers`;
}

function renderStats(stats) {
  const updated = stats.last_crawl?.finished_at;
  const ago = updated
    ? Math.max(0, Math.round((Date.now() - new Date(updated)) / 60000))
    : null;
  const agoText =
    ago == null ? "—" : ago < 60 ? `${ago}m ago` : `${Math.round(ago / 60)}h ago`;

  $("#stats").innerHTML = `
    <div><dt>Bookable</dt><dd>${stats.offers}</dd></div>
    <div><dt>Departures</dt><dd>${stats.departure_stations}</dd></div>
    <div><dt>New 24h</dt><dd class="${
      stats.new_last_24h ? "is-new" : ""
    }">${stats.new_last_24h}</dd></div>
    <div><dt>Updated</dt><dd style="font-size:13px">${agoText}</dd></div>`;

  state.unbookable = stats.filtered_unbookable || 0;
}

function populateFilters() {
  const used = new Set();
  state.offers.forEach((o) => {
    used.add(`s${o.start_station_id}`);
    used.add(`e${o.end_station_id}`);
  });

  const starts = new Map();
  const ends = new Map();
  state.offers.forEach((o) => {
    starts.set(o.start_station_id, `${flag(o.start_country)} ${o.start_name}`);
    ends.set(o.end_station_id, `${flag(o.end_country)} ${o.end_name}`);
  });

  const fill = (sel, entries, placeholder) => {
    const el = $(sel);
    el.innerHTML = `<option value="">${placeholder}</option>`;
    [...entries.entries()]
      .sort((a, b) => a[1].localeCompare(b[1]))
      .forEach(([id, label]) => {
        const opt = document.createElement("option");
        opt.value = id;
        opt.textContent = label;
        el.appendChild(opt);
      });
  };
  fill("#f-from", starts, "Any departure");
  fill("#f-to", ends, "Any destination");

  const countries = new Map();
  state.offers.forEach((o) =>
    countries.set(o.start_country, o.start_country_name || o.start_country)
  );
  const cc = $("#f-from-country");
  cc.innerHTML = `<option value="">Any country</option>`;
  [...countries.entries()]
    .sort((a, b) => (a[1] || "").localeCompare(b[1] || ""))
    .forEach(([code, name]) => {
      const opt = document.createElement("option");
      opt.value = code;
      opt.textContent = `${flag(code)} ${name}`;
      cc.appendChild(opt);
    });
}

/* ── filtering ─────────────────────────────────────────────────────────── */

function readFilters() {
  const country = $("#f-from-country").value;
  return {
    fromCountries: country ? [country] : [],
    from: $("#f-from").value,
    to: $("#f-to").value,
    earliest: $("#f-earliest").value,
    latest: $("#f-latest").value,
    models: selectedModels("#f-models"),
    onlyNew: $("#f-new").checked,
    onlyWide: $("#f-wide").checked,
    showDead: $("#f-dead").checked,
    sort: $("#f-sort").value,
  };
}

/** Every offer touching the focused station, in either direction.
 *  Hub cities are both a departure and a destination, so keying focus on the
 *  station rather than the role keeps the other direction reachable. */
function focusedOffers(list) {
  if (state.focus == null) return list;
  return list.filter(
    (o) =>
      o.start_station_id === state.focus || o.end_station_id === state.focus
  );
}

function applyFilters() {
  const f = readFilters();
  let out = state.offers.filter((o) => {
    if (f.fromCountries.length && !f.fromCountries.includes(o.start_country))
      return false;
    if (f.from && String(o.start_station_id) !== f.from) return false;
    if (f.to && String(o.end_station_id) !== f.to) return false;
    if (f.earliest && o.start_date < f.earliest) return false;
    if (f.latest && o.start_date > f.latest) return false;
    if (f.models.length) {
      const have = (o.vehicles || [])
        .filter((v) => v.available)
        .map((v) => v.name);
      if (!have.some((n) => f.models.includes(n))) return false;
    }
    if (f.onlyNew && !isNew(o)) return false;
    // "Flexible" means the bookable window is longer than the 7-day rental,
    // so you get to choose where your week sits inside it.
    if (f.onlyWide && o.window_days <= 7) return false;
    return true;
  });

  // Counted within the current filters, so the note matches what you are
  // looking at rather than quoting a global total.
  state.deadInScope = out.filter((o) => o.bookable === 0).length;
  if (!f.showDead) out = out.filter((o) => o.bookable !== 0);

  const sorters = {
    date: (a, b) => a.start_date.localeCompare(b.start_date),
    new: (a, b) => (b.first_seen || "").localeCompare(a.first_seen || ""),
    distance: (a, b) => (b.distance_km || 0) - (a.distance_km || 0),
    "distance-asc": (a, b) => (a.distance_km || 0) - (b.distance_km || 0),
    days: (a, b) => b.window_days - a.window_days,
    from: (a, b) => a.start_name.localeCompare(b.start_name),
    to: (a, b) => a.end_name.localeCompare(b.end_name),
  };
  out.sort(sorters[f.sort] || sorters.date);

  state.filtered = out;
  renderList();
  renderMap();
  updateAlertSummary();
}

/* ── list ──────────────────────────────────────────────────────────────── */

function renderList() {
  const list = $("#list");
  const shown = focusedOffers(state.filtered);
  const n = shown.length;

  const dead = state.deadInScope || 0;
  const deadNote = !dead
    ? ""
    : $("#f-dead").checked
    ? ` · <span class="count-dead">${dead} dead end${dead === 1 ? "" : "s"} shown</span>`
    : ` · <span class="count-muted" title="Roadsurfer lists these windows but the booking step refuses them. Tick 'Show dead ends' to inspect.">${dead} dead end${
        dead === 1 ? "" : "s"
      } hidden</span>`;

  $("#result-count").innerHTML =
    (n === 0 ? "No trips match" : `${n} trip${n === 1 ? "" : "s"}`) + deadNote;

  renderFocusBanner(n);

  if (!n) {
    list.innerHTML = `<div class="empty">
        <p><strong>Nothing matches these filters.</strong></p>
        <p class="small">Try widening the dates, or reset to see all
        ${state.offers.length} trips.</p>
      </div>`;
    return;
  }

  const grouped = $("#f-sort").value === "date";
  let html = "";
  let currentMonth = null;

  shown.forEach((o) => {
    if (grouped) {
      const m = monthKey(o.start_date);
      if (m !== currentMonth) {
        currentMonth = m;
        html += `<div class="month-label">${m}</div>`;
      }
    }
    html += cardHtml(o);
  });
  list.innerHTML = html;

  renderMapResults(shown);

  $$(".card").forEach((el) => {
    el.addEventListener("click", () => openDetail(el.dataset.key));
    // Hovering a trip traces it on the map, without moving the viewport.
    el.addEventListener("mouseenter", () => {
      state.hoverKey = el.dataset.key;
      renderArcs();
    });
    el.addEventListener("mouseleave", () => {
      if (state.hoverKey === el.dataset.key) {
        state.hoverKey = null;
        renderArcs();
      }
    });
  });
}

/** Mobile: the trips for the tapped point, rendered under the map itself. */
function renderMapResults(shown) {
  const box = $("#map-results");
  if (!box) return;

  if (state.focus == null) {
    box.innerHTML = `<p class="map-results-hint">
        Tap a point on the map to see its trips here.
      </p>`;
    return;
  }

  const pt = stationPoints(state.filtered).get(state.focus);
  const name = pt ? `${flag(pt.cc)} ${escapeHtml(pt.name)}` : "this station";
  box.innerHTML = `
    <div class="map-results-head">
      <strong>${shown.length} ${shown.length === 1 ? "trip" : "trips"}</strong>
      &middot; ${name}
      <button type="button" id="map-results-clear">Clear ✕</button>
    </div>
    ${shown.map(cardHtml).join("") ||
      `<p class="map-results-hint">Nothing matches the current filters.</p>`}`;

  $("#map-results-clear").addEventListener("click", clearFocus);
  box.querySelectorAll(".card").forEach((el) =>
    el.addEventListener("click", () => openDetail(el.dataset.key))
  );
}

function renderFocusBanner(count) {
  const bar = $("#focus-bar");
  if (state.focus == null) {
    bar.hidden = true;
    bar.innerHTML = "";
    return;
  }
  const pt = stationPoints(state.filtered).get(state.focus);
  const name = pt ? `${flag(pt.cc)} ${pt.name}` : `Station ${state.focus}`;

  let dir = "connected to";
  if (pt && pt.departures && !pt.arrivals) dir = "departing from";
  else if (pt && pt.arrivals && !pt.departures) dir = "arriving at";

  bar.hidden = false;
  bar.innerHTML = `<span><strong>${count}</strong> ${
    count === 1 ? "trip" : "trips"
  } ${dir} ${escapeHtml(name)}${
    pt && pt.departures && pt.arrivals
      ? ` <span class="focus-split">(${pt.departures} out · ${pt.arrivals} in)</span>`
      : ""
  }</span>
    <button type="button" id="focus-clear">Clear ✕</button>`;
  $("#focus-clear").addEventListener("click", clearFocus);
}

function cardHtml(o) {
  const vehicles = (o.vehicles || []).filter((v) => v.available);
  const until = daysUntil(o.start_date);
  const chips = [];
  const dead = o.bookable === 0;

  if (dead)
    chips.push(
      `<span class="chip chip-dead" title="${escapeHtml(
        DEAD_REASONS[o.unbookable_reason]?.detail || "Booking refuses this window."
      )}">${escapeHtml(
        DEAD_REASONS[o.unbookable_reason]?.label || "Dead end"
      )}</span>`
    );
  if (isNew(o)) chips.push(`<span class="chip chip-new">New</span>`);
  if (o.window_days > 7)
    chips.push(
      `<span class="chip chip-flex">${o.window_days}-day window</span>`
    );
  else if (o.window_days < 7)
    // Below the usual 6-7, so it is worth calling out rather than filtering.
    chips.push(
      `<span class="chip chip-short" title="Shorter than the usual 7-day rally">only ${o.window_days} days</span>`
    );
  else chips.push(`<span class="chip">${o.window_days} days</span>`);

  if (until <= 7)
    chips.push(
      `<span class="chip">${until <= 0 ? "today" : `in ${until}d`}</span>`
    );
  if (o.distance_km)
    chips.push(`<span class="chip">${Math.round(o.distance_km)} km</span>`);

  vehicles.slice(0, 2).forEach((v) => {
    const seats = v.seats ? ` · ${v.seats}p` : "";
    chips.push(
      `<span class="chip chip-vehicle">${escapeHtml(v.name)}${seats}</span>`
    );
  });
  if (vehicles.length > 2)
    chips.push(`<span class="chip">+${vehicles.length - 2} more</span>`);
  if (!vehicles.length && o.bookable == null)
    chips.push(`<span class="chip">checking availability…</span>`);

  return `
    <article class="card ${state.selected === o.key ? "is-active" : ""} ${
    dead ? "is-dead" : ""
  }" data-key="${escapeHtml(o.key)}">
      <div class="card-top">
        <div class="route">
          ${flag(o.start_country)} ${escapeHtml(o.start_name)}
          <span class="arrow">→</span>
          ${flag(o.end_country)} ${escapeHtml(o.end_name)}
        </div>
        <div class="price">${dead ? "—" : fmtPrice(o.price, o.currency)}</div>
      </div>
      <div class="card-dates">${fmtDate(o.start_date)} – ${fmtDate(
    o.end_date
  )}</div>
      <div class="card-meta">${chips.join("")}</div>
    </article>`;
}

/* ── map ───────────────────────────────────────────────────────────────── */

function initMap() {
  // Narrow viewports need a wider zoom to show the same span of Europe.
  const startZoom = window.innerWidth < 860 ? 3.4 : 5;
  state.map = L.map("map", {
    zoomControl: true,
    attributionControl: true,
    // Half-steps let fitBounds land on Europe instead of over/undershooting.
    zoomSnap: 0.25,
    zoomDelta: 0.5,
  }).setView([48.5, 10], startZoom);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(state.map);
  state.layers.arcs = L.layerGroup().addTo(state.map);
  state.layers.markers = L.layerGroup().addTo(state.map);
}

/** Quadratic bezier bulged perpendicular to the route, so overlapping
 *  city pairs stay visually distinct instead of collapsing onto one line. */
function arcPoints(a, b, bend = 0.18) {
  const [lat1, lon1] = a;
  const [lat2, lon2] = b;
  const mx = (lat1 + lat2) / 2;
  const my = (lon1 + lon2) / 2;
  const dx = lat2 - lat1;
  const dy = lon2 - lon1;
  const cx = mx - dy * bend;
  const cy = my + dx * bend;
  const pts = [];
  for (let t = 0; t <= 1.0001; t += 0.05) {
    const u = 1 - t;
    pts.push([
      u * u * lat1 + 2 * u * t * cx + t * t * lat2,
      u * u * lon1 + 2 * u * t * cy + t * t * lon2,
    ]);
  }
  return pts;
}

/** One point per station, counting how many trips leave and arrive there. */
function stationPoints(offers) {
  const pts = new Map();
  const touch = (id, name, cc, lat, lon, dir) => {
    if (lat == null) return;
    if (!pts.has(id))
      pts.set(id, {
        id, name, cc, latlng: [lat, lon], departures: 0, arrivals: 0,
      });
    pts.get(id)[dir]++;
  };
  offers.forEach((o) => {
    touch(o.start_station_id, o.start_name, o.start_country,
          o.start_lat, o.start_lon, "departures");
    touch(o.end_station_id, o.end_name, o.end_country,
          o.end_lat, o.end_lon, "arrivals");
  });
  return pts;
}

/** Markers only. Connections are drawn separately, on focus or hover. */
function renderMap(fit = true) {
  if (!state.map) return;
  state.layers.markers.clearLayers();
  state.markerIndex.clear();

  const pts = stationPoints(state.filtered);
  const bounds = [];

  // Draw arrival-only points first so departure points sit above them.
  const ordered = [...pts.values()].sort(
    (a, b) => (a.departures > 0) - (b.departures > 0)
  );
  ordered.forEach((pt) => {
    bounds.push(pt.latlng);
    marker(pt);
  });

  renderArcs();

  if (fit && bounds.length) {
    state.map.fitBounds(L.latLngBounds(bounds).pad(0.12), { animate: false });
  }
}

/** Arcs are transient: shown for the focused station, or the hovered trip. */
function renderArcs() {
  if (!state.map) return;
  state.layers.arcs.clearLayers();

  const draw = (o, style) => {
    if (o.start_lat == null || o.end_lat == null) return;
    L.polyline(arcPoints([o.start_lat, o.start_lon], [o.end_lat, o.end_lon]), {
      interactive: false,
      ...style,
    }).addTo(state.layers.arcs);
  };

  if (state.focus) {
    focusedOffers(state.filtered).forEach((o) =>
      draw(o, { color: "#c2410c", weight: 1.8, opacity: 0.55 })
    );
  }

  const hovered = state.hoverKey
    ? state.filtered.find((o) => o.key === state.hoverKey)
    : null;
  if (hovered) draw(hovered, { color: "#c2410c", weight: 3.5, opacity: 1 });

  const selected = state.selected
    ? state.filtered.find((o) => o.key === state.selected)
    : null;
  if (selected) draw(selected, { color: "#c2410c", weight: 3.5, opacity: 1 });
}

function marker(pt) {
  const focused = state.focus === pt.id;
  const total = pt.departures + pt.arrivals;
  // A city with any departures is somewhere you can start a trip, which is
  // the actionable role, so it takes the departure colour.
  const isStart = pt.departures > 0;

  const m = L.circleMarker(pt.latlng, {
    radius: Math.min(6 + Math.sqrt(total) * 2.6, 15),
    color: focused ? "#c2410c" : "#fff",
    weight: focused ? 3.5 : 1.5,
    fillColor: isStart ? "#c2410c" : "#1d4ed8",
    fillOpacity: focused ? 1 : 0.85,
  }).addTo(state.layers.markers);

  const bits = [];
  if (pt.departures)
    bits.push(`${pt.departures} departure${pt.departures === 1 ? "" : "s"}`);
  if (pt.arrivals)
    bits.push(`${pt.arrivals} arrival${pt.arrivals === 1 ? "" : "s"}`);

  m.bindTooltip(
    `<b>${flag(pt.cc)} ${escapeHtml(pt.name)}</b> — ${bits.join(" · ")}
     <br><span class="tt-hint">${
       focused ? "click again to clear" : "click to see its connections"
     }</span>`,
    { direction: "top", offset: [0, -6] }
  );

  m.on("click", (e) => {
    // Without this the click reaches the map's own handler, which clears the
    // focus this very click just set.
    L.DomEvent.stopPropagation(e);
    toggleFocus(pt.id);
  });
  state.markerIndex.set(pt.id, m);
}

function toggleFocus(id) {
  state.focus = state.focus === id ? null : id;
  state.selected = null;
  $("#detail-backdrop").hidden = true;
  renderList();
  renderMap(false);
}

function clearFocus() {
  if (state.focus == null) return;
  state.focus = null;
  renderList();
  renderMap(false);
}

/* ── detail modal ──────────────────────────────────────────────────────── */

function openDetail(key) {
  const o = state.offers.find((x) => x.key === key);
  if (!o) return;
  state.selected = key;
  // Arcs only — refitting the viewport here would yank the map mid-read.
  renderArcs();
  $$(".card").forEach((c) => c.classList.toggle("is-active", c.dataset.key === key));

  const vehicles = o.vehicles || [];
  const images = vehicles.flatMap((v) => v.images || []).slice(0, 8);
  const flexible = o.window_days > 7;
  const reason = o.bookable === 0 ? DEAD_REASONS[o.unbookable_reason] : null;

  const leg = (icon, title, station, cc, place, info, addr, gmap) => `
    <div class="leg">
      <div class="leg-icon">${icon}</div>
      <div>
        <h3>${title}</h3>
        <div class="leg-place">${flag(cc)} ${escapeHtml(station)}</div>
        <div class="leg-line">${place}</div>
        ${info ? `<div class="leg-line">${info}</div>` : ""}
        ${
          addr
            ? `<div class="leg-line">${escapeHtml(addr)}${
                gmap
                  ? ` · <a href="${escapeHtml(
                      gmap
                    )}" target="_blank" rel="noopener">map</a>`
                  : ""
              }</div>`
            : ""
        }
      </div>
    </div>`;

  const pickupTimes =
    o.pickup && hhmm(o.pickup.from)
      ? `Pick up between ${hhmm(o.pickup.from)} and ${hhmm(o.pickup.to)}`
      : "";
  const dropTimes =
    o.dropoff && hhmm(o.dropoff.from)
      ? `Return between ${hhmm(o.dropoff.from)} and ${hhmm(o.dropoff.to)}`
      : "";

  $("#detail-body").innerHTML = `
    <div class="detail-header">
      <h2>${flag(o.start_country)} ${escapeHtml(o.start_name)} →
          ${flag(o.end_country)} ${escapeHtml(o.end_name)}</h2>
      <p class="detail-sub">
        ${o.distance_km ? `${Math.round(o.distance_km)} km apart · ` : ""}
        ${
          daysUntil(o.start_date) <= 0
            ? "departs today"
            : `departs in ${daysUntil(o.start_date)} day${
                daysUntil(o.start_date) === 1 ? "" : "s"
              }`
        }
      </p>
      ${
        reason
          ? `<span class="detail-price is-dead">Not bookable</span>`
          : `<span class="detail-price">${fmtPrice(o.price, o.currency)} total</span>`
      }
    </div>

    ${
      reason
        ? `<div class="dead-note">
             <strong>${escapeHtml(reason.label)}.</strong>
             ${escapeHtml(reason.detail)}
             <span class="dead-code">API said: <code>${escapeHtml(
               o.unbookable_reason || "unknown"
             )}</code></span>
           </div>`
        : ""
    }

    ${
      images.length
        ? `<div class="gallery">${images
            .map(
              (src) =>
                `<img src="${escapeHtml(
                  src
                )}" alt="" loading="lazy" decoding="async">`
            )
            .join("")}</div>`
        : ""
    }

    <div class="legs">
      ${leg(
        "🅿️",
        "Pick up",
        o.start_name,
        o.start_country,
        fmtDateLong(o.start_date),
        pickupTimes,
        o.pickup?.address || o.start_address,
        o.start_google
      )}
      ${leg(
        "🏁",
        "Drop off",
        o.end_name,
        o.end_country,
        fmtDateLong(o.end_date),
        dropTimes,
        o.dropoff?.address || o.end_address,
        o.end_google
      )}
    </div>

    <p class="preview-line">
      ${
        flexible
          ? `<strong>${o.window_days}-day window.</strong> The rental itself is
             capped at 7 days, so you can choose where your week sits inside it.`
          : `<strong>Fixed ${o.window_days}-day trip.</strong> These dates are the
             whole window — you can return earlier, but not start later.`
      }
    </p>

    ${
      reason && !vehicles.length
        ? ""
        : vehicles.length
        ? `<table class="vehicle-table">
             <thead><tr><th>Camper</th><th>Seats</th><th>Beds</th><th>Price</th></tr></thead>
             <tbody>${vehicles
               .map(
                 (v) => `<tr>
                   <td>${escapeHtml(v.name)}${
                   v.available ? "" : ' <span class="tag-out">gone</span>'
                 }</td>
                   <td>${v.seats ?? "—"}</td>
                   <td>${v.beds ?? "—"}</td>
                   <td>${fmtPrice(v.price, o.currency)}</td>
                 </tr>`
               )
               .join("")}</tbody>
           </table>`
        : `<p class="preview-line">Vehicle details not fetched yet — they appear
           after the next hourly refresh.</p>`
    }

    <div class="detail-actions">
      <a class="btn ${reason ? "" : "btn-primary"}"
         href="https://booking.roadsurfer.com/en/rally"
         target="_blank" rel="noopener">${
           reason ? "Check on roadsurfer.com" : "Book on roadsurfer.com"
         }</a>
      <a class="btn" href="https://www.google.com/maps/dir/?api=1&origin=${encodeURIComponent(
        o.pickup?.address || o.start_address || o.start_name
      )}&destination=${encodeURIComponent(
    o.dropoff?.address || o.end_address || o.end_name
  )}" target="_blank" rel="noopener">Route on Google Maps</a>
    </div>`;

  $("#detail-backdrop").hidden = false;
}

/* ── saved searches ────────────────────────────────────────────────────── */

function criteriaFromFilters() {
  const f = readFilters();
  const c = {};
  if (f.fromCountries.length) c.start_countries = f.fromCountries;
  if (f.from) c.start_station_ids = [+f.from];
  if (f.to) c.end_station_ids = [+f.to];
  if (f.earliest) c.earliest_date = f.earliest;
  if (f.latest) c.latest_date = f.latest;
  if (f.onlyWide) c.min_days = 8;

  // Campers are picked inside the alert form, falling back to the filter bar
  // so an alert built straight from the filters keeps that choice.
  const picked = $("#a-models")
    ? selectedModels("#a-models")
    : [];
  const models = picked.length ? picked : f.models;
  if (models.length) c.models = models;
  return c;
}

function describeCriteria(c) {
  const nameOf = (id) => {
    const o = state.offers.find(
      (x) => x.start_station_id === id || x.end_station_id === id
    );
    if (!o) return `#${id}`;
    return o.start_station_id === id ? o.start_name : o.end_name;
  };
  const countryOf = (code) => {
    const o = state.offers.find((x) => x.start_country === code);
    return o?.start_country_name || code;
  };
  const parts = [];
  if (c.start_station_ids) parts.push(`from ${c.start_station_ids.map(nameOf).join(", ")}`);
  else if (c.start_countries)
    parts.push(`from ${c.start_countries.map(countryOf).join(", ")}`);
  if (c.end_station_ids) parts.push(`to ${c.end_station_ids.map(nameOf).join(", ")}`);
  if (c.earliest_date && c.latest_date)
    parts.push(`departing ${c.earliest_date} → ${c.latest_date}`);
  else if (c.earliest_date) parts.push(`departing from ${c.earliest_date}`);
  else if (c.latest_date) parts.push(`departing by ${c.latest_date}`);
  if (c.min_days >= 8) parts.push("flexible window only");
  if (c.models)
    parts.push(
      c.models.length === 1 ? c.models[0] : `${c.models.length} camper types`
    );
  return parts.length ? parts.join(" · ") : "anything new, anywhere in Europe";
}

function updateAlertSummary() {
  if ($("#alert-backdrop").hidden) return;
  const c = criteriaFromFilters();
  $("#a-summary").textContent = describeCriteria(c);

  // Count against the alert's own criteria, which can differ from the filter
  // bar once campers are ticked inside the form.
  const models = c.models || [];
  const n = state.filtered.filter((o) => {
    if (!models.length) return true;
    return (o.vehicles || [])
      .filter((v) => v.available)
      .some((v) => models.includes(v.name));
  }).length;

  $("#a-preview").textContent =
    `${n} current trip${n === 1 ? "" : "s"} match this. ` +
    `You'll only be alerted about trips that appear from now on.`;
}

function openAlert() {
  $("#alert-backdrop").hidden = false;
  // Seed the alert's camper picks from whatever the filter bar has selected.
  if ($("#a-models") && !$("#a-models").children.length) {
    modelCheckboxes($("#a-models"), "am");
    const fromFilter = selectedModels("#f-models");
    $$("#a-models input").forEach((cb) => {
      cb.checked = fromFilter.includes(cb.value);
      cb.addEventListener("change", updateAlertSummary);
    });
  }
  const c = criteriaFromFilters();
  if (!$("#a-name").value) {
    $("#a-name").value =
      describeCriteria(c) === "anything new, anywhere in Europe"
        ? "Anything new"
        : describeCriteria(c).slice(0, 60);
  }
  updateAlertSummary();
}

async function createAlert() {
  const btn = $("#a-create");
  btn.disabled = true;
  btn.textContent = "Creating…";
  try {
    const resp = await fetch(`${API}/watches`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: $("#a-name").value || "My search",
        criteria: criteriaFromFilters(),
      }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const watch = await resp.json();

    const saved = JSON.parse(localStorage.getItem("rally.watches") || "[]");
    saved.push({ id: watch.id, name: watch.name });
    localStorage.setItem("rally.watches", JSON.stringify(saved));

    $("#alert-body").innerHTML = `
      <h2>Almost there</h2>
      <p class="muted">Your search <strong>${escapeHtml(
        watch.name
      )}</strong> is saved:</p>
      <div class="alert-criteria">
        <p class="criteria-summary">${escapeHtml(watch.summary)}</p>
      </div>
      ${
        watch.telegram_link
          ? `<div class="telegram-box">
               <strong>Tap to connect Telegram</strong>
               <p class="muted small">This binds the search to your chat.
                  Nobody else can see or edit it.</p>
               <a href="${escapeHtml(
                 watch.telegram_link
               )}" target="_blank" rel="noopener">Open Telegram</a>
               <code>${escapeHtml(watch.telegram_link)}</code>
             </div>`
          : `<div class="telegram-box">
               <strong>Telegram is not configured yet</strong>
               <p class="muted small">Set TELEGRAM_BOT_TOKEN on the server, then
                  open this link to connect:</p>
               <code>/start ${escapeHtml(watch.id)}</code>
             </div>`
      }`;
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "Create alert";
    $("#a-preview").textContent = `Could not create the alert: ${err.message}`;
  }
}

/* ── wiring ────────────────────────────────────────────────────────────── */

function closeModal(which) {
  $(`#${which}-backdrop`).hidden = true;
  if (which === "detail") {
    state.selected = null;
    renderArcs();
    $$(".card").forEach((c) => c.classList.remove("is-active"));
  }
  if (which === "alert") {
    // Restore the form so a second alert can be created without a reload.
    location.hash = "";
  }
}

function init() {
  initMap();
  $$(".dropdown").forEach(wireDropdown);

  $$("#filters select, #filters input").forEach((el) => {
    el.addEventListener("change", applyFilters);
  });

  $("#btn-reset").addEventListener("click", () => {
    $$("#filters select").forEach((s) => (s.value = ""));
    $$("#filters input").forEach((i) => {
      if (i.type === "checkbox") i.checked = false;
      else i.value = "";
    });
    $("#f-sort").value = "date";
    state.focus = null;
    syncModelSummary();
    applyFilters();
  });

  // Clicking bare map clears a focused station, matching the ✕ in the banner.
  state.map.on("click", clearFocus);

  $("#btn-alert").addEventListener("click", openAlert);
  $("#alert-body").addEventListener("click", (e) => {
    if (e.target.id === "a-create") createAlert();
  });
  $("#alert-body").addEventListener("input", (e) => {
    if (["a-max-price", "a-min-seats"].includes(e.target.id)) updateAlertSummary();
  });

  $$("[data-close]").forEach((b) =>
    b.addEventListener("click", () => closeModal(b.dataset.close))
  );
  $$(".modal-backdrop").forEach((bd) =>
    bd.addEventListener("click", (e) => {
      if (e.target === bd) closeModal(bd.id.replace("-backdrop", ""));
    })
  );
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const open = $$(".modal-backdrop:not([hidden])");
    if (open.length) {
      open.forEach((bd) => closeModal(bd.id.replace("-backdrop", "")));
    } else {
      clearFocus();
    }
  });

  $$(".mobile-toggle button").forEach((b) =>
    b.addEventListener("click", () => {
      $$(".mobile-toggle button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      document.body.classList.toggle("view-map", b.dataset.view === "map");
      if (b.dataset.view === "map") {
        // The pane was display:none, so Leaflet sized itself to nothing.
        // Re-measure, then refit, or the map opens zoomed at the wrong place.
        setTimeout(() => {
          state.map.invalidateSize();
          renderMap(true);
        }, 60);
      }
    })
  );

  load().catch((err) => {
    $("#list").innerHTML = `<div class="empty">
      <p><strong>Could not load trips.</strong></p>
      <p class="small">${escapeHtml(err.message)}</p></div>`;
  });

  // Hourly server refresh; pick it up without a manual reload.
  setInterval(() => load().catch(() => {}), 10 * 60 * 1000);
}

document.addEventListener("DOMContentLoaded", init);
