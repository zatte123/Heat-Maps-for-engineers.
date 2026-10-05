(function () {
  "use strict";
  const DATA = JSON.parse(document.getElementById("data").textContent);
  const P = window.Planner;
  const $ = (id) => document.getElementById(id);
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get(key, fallback) { try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch (e) { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* storage unavailable */ } },
  };

  const office = DATA.office;
  const engineers = DATA.engineers;
  const engById = new Map(engineers.map((e) => [e.id, e]));
  const settings = Object.assign({}, DATA.settings, store.get("heatmap.settings", {}));
  let adhoc = store.get("heatmap.adhoc", []).filter((j) => j.date >= DATA.windowStart);
  const materials = new Map(); // job key -> needs materials (PM override for this session)

  const allBookings = () => DATA.bookings.concat(adhoc);
  let index = P.indexBookings(allBookings());
  const ctx = () => ({ office, settings, index });

  // ---------- header ----------
  $("meta").textContent = `${engineers.filter((e) => e.lat != null).length} engineers mapped · board ${fmtDate(DATA.windowStart)} – ${fmtDate(DATA.windowEnd)} · generated ${DATA.generatedAt.replace("T", " ")}`;
  if (DATA.warnings.length) {
    const box = $("warnings");
    box.hidden = false;
    box.querySelector("summary").textContent = `${DATA.warnings.length} data warning${DATA.warnings.length > 1 ? "s" : ""}`;
    box.querySelector("ul").innerHTML = DATA.warnings.map((w) => `<li>${esc(w)}</li>`).join("");
  }

  function fmtDate(iso, long) {
    const d = new Date(iso + "T12:00:00");
    return d.toLocaleDateString("en-GB", long ? { weekday: "long", day: "numeric", month: "long" } : { weekday: "short", day: "numeric", month: "short" });
  }
  function fmtDur(min) {
    const m = Math.round(min);
    return m >= 60 ? `+${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m` : `+${m} min`;
  }
  function localToday() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  // ---------- dates ----------
  function workingDates() {
    const out = new Set(allBookings().map((b) => b.date));
    const d = new Date(DATA.windowStart + "T12:00:00Z");
    const end = new Date(DATA.windowEnd + "T12:00:00Z");
    for (; d <= end; d.setUTCDate(d.getUTCDate() + 1)) {
      if (d.getUTCDay() % 6 !== 0) out.add(d.toISOString().slice(0, 10));
    }
    return [...out].sort();
  }
  let dates = workingDates();
  let day = dates.find((d) => d >= localToday()) || dates[dates.length - 1];

  function fillDates() {
    dates = workingDates();
    $("daySelect").innerHTML = dates.map((d) => {
      const n = allBookings().filter((b) => b.date === d && !b.engineerId).length;
      return `<option value="${d}">${esc(fmtDate(d))}${n ? ` · ${n} unassigned` : ""}</option>`;
    }).join("");
    $("daySelect").value = day;
  }

  // ---------- map ----------
  const map = L.map("map", { zoomControl: true, preferCanvas: true });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18, attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);

  const hasHeat = typeof L.heatLayer === "function";
  const homePts = engineers.filter((e) => e.lat != null).map((e) => [e.lat, e.lon, 1]);
  const homesHeat = hasHeat ? L.heatLayer(homePts, {
    radius: 28, blur: 22, minOpacity: 0.2, maxZoom: 10, max: 2.5,
    gradient: { 0.2: "#cde2fb", 0.45: "#86b6ef", 0.7: "#2a78d6", 1: "#104281" },
  }) : L.layerGroup();
  const sitesHeat = hasHeat ? L.heatLayer([], {
    radius: 28, blur: 22, minOpacity: 0.2, maxZoom: 10, max: 2.5,
    gradient: { 0.2: "#fbd9c9", 0.45: "#f39a73", 0.7: "#eb6834", 1: "#a8401a" },
  }) : L.layerGroup();
  const homesLayer = L.layerGroup();
  const linksLayer = L.layerGroup();
  const sitesLayer = L.layerGroup().addTo(map);
  const selLayer = L.layerGroup().addTo(map);

  for (const e of engineers) {
    if (e.lat == null) continue;
    L.circleMarker([e.lat, e.lon], { radius: 4, weight: 1, color: cssVar("--surface"), fillColor: cssVar("--home"), fillOpacity: 0.9 })
      .bindTooltip(`${esc(e.name)}<br>${esc(e.postcode)}${e.approx ? " (approx.)" : ""}`)
      .addTo(homesLayer);
  }
  L.marker([office.lat, office.lon], {
    icon: L.divIcon({ className: "", html: '<div class="pin pin-office" style="width:22px;height:22px">HQ</div>', iconSize: [22, 22] }),
    zIndexOffset: 1000,
  }).bindTooltip(`Head office (${esc(office.postcode)})`).addTo(map);

  let currentJob = null;
  let selectedCand = 0;
  let showAll = false;

  const toggles = [["lyHomesHeat", homesHeat], ["lySitesHeat", sitesHeat], ["lyHomes", homesLayer], ["lyLinks", linksLayer]];
  for (const [id, layer] of toggles) {
    const box = $(id);
    // The day's home->site lines would bury the selected job's routes, so they hide while one is open.
    const sync = () => (box.checked && !(layer === linksLayer && currentJob) ? layer.addTo(map) : map.removeLayer(layer));
    box.addEventListener("change", sync);
    box.addEventListener("sync", sync);
    sync();
  }
  const syncLayers = () => toggles.forEach(([id]) => $(id).dispatchEvent(new Event("sync")));

  const fitPts = homePts.map((p) => [p[0], p[1]]).concat([[office.lat, office.lon]]);
  if (fitPts.length > 1) map.fitBounds(fitPts, { padding: [30, 30] });
  else map.setView([office.lat, office.lon], 10);

  // ---------- jobs for the day ----------
  function jobsForDay(date) {
    const groups = new Map();
    for (const b of allBookings()) {
      if (b.date !== date) continue;
      const key = b.engineerId ? `${b.postcode}|${b.ref}` : b.id; // crews share a site; open jobs stay separate
      if (!groups.has(key)) groups.set(key, Object.assign({}, b, { key, crew: [], ids: [] }));
      const g = groups.get(key);
      g.ids.push(b.id);
      if (b.engineerId) g.crew.push(b.engineerId);
      if (!g.engineerId && b.engineerId) g.engineerId = b.engineerId;
    }
    for (const g of groups.values()) {
      if (materials.has(g.key)) g.needsMaterials = materials.get(g.key);
      else if (g.needsMaterials == null) g.needsMaterials = !!settings.default_needs_materials;
    }
    return [...groups.values()].sort((a, b) => (!!a.engineerId - !!b.engineerId) || String(a.postcode).localeCompare(String(b.postcode)));
  }

  function siteMarker(job, selected) {
    const open = !job.engineerId;
    return L.circleMarker([job.lat, job.lon], {
      radius: selected ? 10 : open ? 7 : 6,
      weight: 2,
      color: cssVar("--surface"),
      fillColor: cssVar(open ? "--open" : "--site"),
      fillOpacity: 1,
    });
  }

  function crewNames(job) {
    return job.crew.map((id) => (engById.get(id) || { name: id }).name).join(", ");
  }

  function renderDay() {
    $("daySelect").value = day;
    const jobs = jobsForDay(day);
    const placed = jobs.filter((j) => j.lat != null);

    if (hasHeat) sitesHeat.setLatLngs(placed.map((j) => [j.lat, j.lon, Math.max(1, j.crew.length)]));
    sitesLayer.clearLayers();
    linksLayer.clearLayers();
    for (const j of placed) {
      siteMarker(j, false)
        .bindTooltip(`<b>${esc(j.ref || j.postcode)}</b><br>${esc(j.postcode)}${j.address ? "<br>" + esc(j.address) : ""}<br>${j.engineerId ? esc(crewNames(j)) : "Unassigned"}`)
        .on("click", () => selectJob(j))
        .addTo(sitesLayer);
      for (const id of j.crew) {
        const e = engById.get(id);
        if (e && e.lat != null) {
          L.polyline([[e.lat, e.lon], [j.lat, j.lon]], { color: cssVar("--muted") || "#898781", weight: 1.5, opacity: 0.7 }).addTo(linksLayer);
        }
      }
    }

    $("listTitle").textContent = fmtDate(day, true);
    const open = jobs.filter((j) => !j.engineerId).length;
    $("listCount").textContent = `${jobs.length} job${jobs.length === 1 ? "" : "s"}${open ? ` · ${open} unassigned` : ""}`;
    const list = $("jobList");
    list.innerHTML = jobs.length ? "" : '<li class="empty">Nothing on the board for this day.</li>';
    for (const j of jobs) {
      const li = document.createElement("li");
      li.tabIndex = 0;
      li.innerHTML = `<span class="dot ${j.engineerId ? "dot-site" : "dot-open"}"></span>
        <span><b>${esc(j.ref || j.postcode || "No postcode")}</b>${j.ref ? ` <span class="muted">${esc(j.postcode || "")}</span>` : ""}
        <div class="who">${j.engineerId ? esc(crewNames(j)) : "Unassigned"}${j.lat == null ? " · can't be mapped" : ""}</div></span>
        <span>${j.adhoc ? '<span class="tag">planned here</span>' : !j.engineerId ? '<span class="tag open">pick engineer</span>' : ""}</span>`;
      if (j.lat != null) {
        li.addEventListener("click", () => selectJob(j));
        li.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); selectJob(j); } });
      }
      list.appendChild(li);
    }
    if (currentJob) {
      const again = jobs.find((j) => j.key === currentJob.key);
      if (again) selectJob(again, true); else closeJob();
    }
  }

  // ---------- candidates ----------
  const HOW = {
    direct: () => "Straight from home to site",
    office_morning: () => `Office pickup at ${settings.target_office}, then to site`,
    collect_before: (o) => o.prevDay.atOffice
      ? `Already at the office ${fmtDate(o.prevDay.date)} – takes materials home, straight to site`
      : `Collects materials after ${fmtDate(o.prevDay.date)} job (${o.prevDay.postcode}), straight to site`,
  };

  function flagsHtml(c, o) {
    const f = [];
    const late = o.siteLate, homeLate = o.homeLate, prevLate = o.prevDay ? o.prevDay.addedLate : 0;
    if (o.onTime) f.push('<span class="st good">&#10003; on time</span>');
    if (late) f.push(`<span class="st critical">&#9888;&#xFE0E; ${Math.round(late)} min late on site</span>`);
    if (homeLate) f.push(`<span class="st serious">&#9888;&#xFE0E; home ${Math.round(homeLate)} min after target</span>`);
    if (prevLate) f.push(`<span class="st serious">&#9888;&#xFE0E; pickup makes ${fmtDate(o.prevDay.date)} ${Math.round(prevLate)} min late home</span>`);
    if (o.overtimeMin >= 1) f.push(`<span class="st serious">${fmtDur(o.overtimeMin)} over ${o.contractMin / 60}h contract day</span>`);
    if (c.assigned) f.push('<span class="tag">booked on this job</span>');
    if (c.sameSiteYesterday) f.push('<span class="tag">on this site the day before</span>');
    if (c.engineer.approx) f.push('<span class="tag">home postcode approx.</span>');
    return f.join("");
  }

  function timesHtml(o) {
    const parts = [`Leave home ${P.fmt(o.leaveHome)}`];
    if (o.arriveOffice != null) parts.push(`office ${P.fmt(o.arriveOffice)}`);
    parts.push(`on site ${P.fmt(o.arriveSite)}`, `home ${P.fmt(o.arriveHome)}`);
    return parts.join(" · ");
  }

  function selectJob(job, keepSelection) {
    if (!keepSelection || !currentJob || currentJob.key !== job.key) { selectedCand = 0; showAll = false; }
    currentJob = job;
    $("listView").hidden = true;
    $("jobView").hidden = false;
    const atOffice = P.isOffice(job.postcode, office.prefix);
    $("jobMaterials").checked = !!job.needsMaterials && !atOffice;
    $("jobMaterials").disabled = atOffice;
    $("jobMaterials").parentElement.lastChild.textContent = atOffice
      ? ` Site is at the office (${office.prefix}) – no pickup needed` : " Needs materials from the office";
    $("jobHead").innerHTML = `<h2>${esc(job.ref || job.postcode)}</h2>
      <p class="muted">${esc(fmtDate(job.date, true))} · ${esc(job.postcode)}${job.approx ? " (approx.)" : ""}${job.address ? " · " + esc(job.address) : ""}</p>
      <p class="muted">${job.engineerId ? "Booked: " + esc(crewNames(job)) : "Unassigned"}
      ${job.adhoc ? ' · <button type="button" class="link" id="removeAdhoc">remove</button>' : ""}</p>`;
    if (job.adhoc) $("removeAdhoc").addEventListener("click", () => removeAdhoc(job));

    const res = P.rankCandidates(job, engineers, null, ctx());
    for (const c of res.ranked) c.assigned = job.crew.includes(c.engineer.id);
    renderCandidates(job, res);
    syncLayers();
  }

  function renderCandidates(job, res) {
    const list = $("candList");
    const shown = showAll ? res.ranked : res.ranked.slice(0, 8);
    list.innerHTML = res.ranked.length ? "" : '<li class="empty">No free engineers with a known home location.</li>';
    shown.forEach((c, i) => {
      const o = c.best;
      const li = document.createElement("li");
      li.tabIndex = 0;
      if (i === selectedCand) li.classList.add("sel");
      li.innerHTML = `<span class="rank">${i + 1}</span><span class="name">${esc(c.engineer.name)}</span>
        <span class="drive"><b>${Math.round(o.driveMin)}</b> <span class="muted">min driving</span></span>
        <span class="how">${esc(HOW[o.kind](o))}${o.extraMin >= 1 ? ` <span class="muted">(+${Math.round(o.extraMin)} min for pickup)</span>` : ""}</span>
        <span class="times">${timesHtml(o)}</span>
        <span class="flags">${flagsHtml(c, o)}</span>`;
      const pick = () => { selectedCand = i; renderCandidates(job, res); };
      li.addEventListener("click", pick);
      li.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(); } });
      list.appendChild(li);
    });
    const more = $("moreBtn");
    more.hidden = res.ranked.length <= 8;
    more.textContent = showAll ? "Show top 8" : `Show all ${res.ranked.length}`;

    const busy = $("busyBox");
    const others = res.busy.length + res.unplaceable.length;
    busy.hidden = !others;
    busy.querySelector("summary").textContent = `${res.busy.length} booked elsewhere${res.unplaceable.length ? `, ${res.unplaceable.length} without a home postcode` : ""}`;
    busy.querySelector("ul").innerHTML =
      res.busy.map((b) => `<li>${esc(b.engineer.name)} – ${esc(b.bookings.map((x) => x.ref || x.postcode).join(", "))}</li>`).join("") +
      res.unplaceable.map((u) => `<li>${esc(u.engineer.name)} – no home location</li>`).join("");

    drawSelection(job, shown, shown[selectedCand]);
  }

  function drawSelection(job, shown, chosen) {
    selLayer.clearLayers();
    const site = [job.lat, job.lon];
    siteMarker(job, true).bindTooltip(esc(job.ref || job.postcode)).addTo(selLayer);
    const pts = [site];
    shown.forEach((c, i) => {
      const e = c.engineer;
      if (c !== chosen) L.polyline([[e.lat, e.lon], site], { color: cssVar("--home"), weight: 1, opacity: 0.35, dashArray: "2 6" }).addTo(selLayer);
      L.marker([e.lat, e.lon], {
        icon: L.divIcon({ className: "", html: `<div class="pin pin-cand" style="width:20px;height:20px">${i + 1}</div>`, iconSize: [20, 20] }),
        zIndexOffset: 900 - i,
      }).bindTooltip(`${i + 1}. ${esc(e.name)} – ${Math.round(c.best.driveMin)} min driving`).on("click", () => { selectedCand = i; selectJob(job, true); }).addTo(selLayer);
      pts.push([e.lat, e.lon]);
    });
    if (chosen) {
      const o = chosen.best, e = chosen.engineer, home = [e.lat, e.lon], hq = [office.lat, office.lon];
      const route = o.kind === "office_morning" ? [home, hq, site] : [home, site];
      L.polyline(route, { color: cssVar("--home"), weight: 3, opacity: 0.95 }).addTo(selLayer);
      if (o.kind === "collect_before" && !o.prevDay.atOffice && chosen.prevSite) {
        const prev = [chosen.prevSite.lat, chosen.prevSite.lon];
        L.polyline([prev, hq, home], { color: cssVar("--home"), weight: 2, opacity: 0.8, dashArray: "6 6" })
          .bindTooltip(`Day before: ${esc(chosen.prevSite.postcode)} → office → home`).addTo(selLayer);
        pts.push(prev);
      }
      if (o.kind !== "direct") pts.push(hq);
    }
    map.fitBounds(pts, { padding: [40, 40], maxZoom: 13 });
  }

  function closeJob() {
    currentJob = null;
    selLayer.clearLayers();
    $("jobView").hidden = true;
    $("listView").hidden = false;
    syncLayers();
  }

  $("backBtn").addEventListener("click", closeJob);
  $("moreBtn").addEventListener("click", () => { showAll = !showAll; selectJob(currentJob, true); });
  $("jobMaterials").addEventListener("change", (ev) => {
    materials.set(currentJob.key, ev.target.checked);
    currentJob.needsMaterials = ev.target.checked;
    selectJob(currentJob, true);
  });

  function setDay(d) {
    if (!d) return;
    day = d;
    closeJob();
    renderDay();
  }
  $("daySelect").addEventListener("change", (ev) => setDay(ev.target.value));
  $("prevDay").addEventListener("click", () => setDay(dates[dates.indexOf(day) - 1]));
  $("nextDay").addEventListener("click", () => setDay(dates[dates.indexOf(day) + 1]));

  // ---------- plan an ad-hoc job ----------
  async function geocode(pc) {
    const clean = pc.replace(/\s+/g, "").toUpperCase();
    const r = await fetch(`https://api.postcodes.io/postcodes/${encodeURIComponent(clean)}`);
    if (r.ok) { const j = await r.json(); return { postcode: j.result.postcode, lat: j.result.latitude, lon: j.result.longitude, approx: false }; }
    const out = clean.length > 4 ? clean.slice(0, -3) : clean;
    const r2 = await fetch(`https://api.postcodes.io/outcodes/${encodeURIComponent(out)}`);
    if (r2.ok) { const j = await r2.json(); return { postcode: pc.toUpperCase(), lat: j.result.latitude, lon: j.result.longitude, approx: true }; }
    return null;
  }

  function refreshBookings() {
    store.set("heatmap.adhoc", adhoc);
    index = P.indexBookings(allBookings());
    fillDates();
  }

  function removeAdhoc(job) {
    adhoc = adhoc.filter((a) => a.id !== job.id);
    refreshBookings();
    closeJob();
    renderDay();
  }

  const planForm = $("planForm");
  planForm.date.value = day;
  planForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const msg = $("planMsg");
    msg.textContent = "Looking up postcode…";
    let loc = null;
    try { loc = await geocode(planForm.postcode.value.trim()); } catch (e) { loc = null; }
    if (!loc) { msg.textContent = "Couldn't find that postcode (postcodes.io)."; return; }
    const job = {
      id: `a${Date.now()}`, adhoc: true, date: planForm.date.value, engineerId: null,
      ref: planForm.ref.value.trim(), address: "", needsMaterials: planForm.needsMaterials.checked, ...loc,
    };
    adhoc.push(job);
    refreshBookings();
    msg.textContent = "";
    planForm.reset();
    day = job.date;
    planForm.date.value = day;
    renderDay();
    const j = jobsForDay(day).find((g) => g.id === job.id);
    if (j) selectJob(j);
  });

  // ---------- settings ----------
  const FIELDS = [
    ["avg_speed_kmh", "Avg speed (km/h)", "number"], ["road_factor", "Road factor", "number"],
    ["target_office", "At office by", "time"], ["target_site", "On site by", "time"],
    ["target_work_end_weekday", "Finish Mon–Thu", "time"], ["target_work_end_friday", "Finish Fri", "time"],
    ["target_home_weekday", "Home Mon–Thu", "time"], ["target_home_friday", "Home Fri", "time"],
    ["min_stop_min", "Office stop (min)", "number"], ["late_tolerance", "Late tolerance (min)", "number"],
    ["contract_hours_mon_thu", "Contract h Mon–Thu", "number"], ["contract_hours_fri", "Contract h Fri", "number"],
  ];
  const sf = $("settingsForm");
  sf.innerHTML = FIELDS.map(([k, label, type]) =>
    `<label>${label}<input name="${k}" type="${type}" step="${type === "number" ? "any" : "60"}" value="${esc(settings[k])}"></label>`).join("") +
    '<button type="button" id="resetSettings" class="link">Reset to defaults</button>';
  sf.addEventListener("input", (ev) => {
    const { name, type, value } = ev.target;
    if (!name || value === "") return;
    settings[name] = type === "number" ? Number(value) : value;
    store.set("heatmap.settings", Object.fromEntries(FIELDS.map(([k]) => [k, settings[k]])));
    if (currentJob) selectJob(currentJob, true);
  });
  $("resetSettings").addEventListener("click", () => {
    Object.assign(settings, DATA.settings);
    store.set("heatmap.settings", {});
    for (const [k] of FIELDS) sf[k].value = settings[k];
    if (currentJob) selectJob(currentJob, true);
  });

  fillDates();
  renderDay();
})();
