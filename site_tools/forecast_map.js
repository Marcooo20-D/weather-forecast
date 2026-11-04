const geojson = __DATA__;
const colors = {
  safe: "#10b981",
  watch: "#f59e0b",
  rain: "#ff9d42",
  danger: "#ef4444",
  limited: "#94a3b8",
};
const features = ((geojson && geojson.features) || []).filter(
  (f) =>
    f.geometry &&
    f.geometry.type === "Point" &&
    f.geometry.coordinates.length === 2 &&
    f.geometry.coordinates.every(Number.isFinite),
);

// Find first coordinate center
const first = features[0] || {
  geometry: { coordinates: [106.8, -6.2] },
  properties: { location_name: "Lokasi" },
};
const center =
  first.geometry && first.geometry.coordinates
    ? [first.geometry.coordinates[1], first.geometry.coordinates[0]]
    : [-6.2, 106.8];

// Selected point and regional layer.
let weatherField = null;
const state = {
  windSpeed: null,
  cloudPct: null,
  tempC: null,
  humidityPct: null,
  rainProb: null,
  activeLayer: "wind",
};

function getTempColor(t) {
  if (t <= 18) return "#3b82f6";
  if (t <= 22) return "#60a5fa";
  if (t <= 25) return "#f59e0b";
  if (t <= 28) return "#f97316";
  return "#ef4444";
}

function getHumidityColor(h) {
  if (h <= 55) return "#93c5fd";
  if (h <= 70) return "#38bdf8";
  if (h <= 85) return "#0ea5e9";
  return "#0284c7";
}

function getCloudColor(c) {
  if (c <= 30) return "#475569";
  if (c <= 60) return "#94a3b8";
  return "#f1f5f9";
}

function getWindColor(w) {
  if (w <= 5) return "#10b981";
  if (w <= 12) return "#3b82f6";
  if (w <= 20) return "#f59e0b";
  return "#ef4444";
}

function updateHUDValues(p) {
  document.getElementById("hud-location-name").textContent =
    p.location_name || "Lokasi";
  const validDate = Date.parse((p.date_iso || "") + "T00:00:00+07:00");
  const validLabel = Number.isFinite(validDate)
    ? new Intl.DateTimeFormat("id-ID", {
        timeZone: "Asia/Jakarta",
        weekday: "short",
        day: "numeric",
        month: "short",
        year: "numeric",
      }).format(new Date(validDate))
    : p.date_iso || "";
  document.getElementById("hud-time-label").textContent =
    `${validLabel} · ${p.hour || ""} WIB`;
  const issueTime = Date.parse(p.issued_at);
  document.getElementById("hud-issue").textContent = Number.isFinite(issueTime)
    ? "Terbit: " +
      new Intl.DateTimeFormat("id-ID", {
        timeZone: "Asia/Jakarta",
        day: "numeric",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      }).format(new Date(issueTime)) +
      " WIB"
    : "Waktu terbit belum tersedia";
  document.getElementById("val-temp").textContent =
    p.temp_c != null ? `${Math.round(p.temp_c)}°C` : "—";
  document.getElementById("val-rain").textContent =
    p.rain_probability != null
      ? LangitPublicData.format(p.rain_probability, "%")
      : "—";
  document.getElementById("val-wind").textContent =
    p.wind_kmh != null ? `${p.wind_kmh.toFixed(1)} km/jam` : "—";
  document.getElementById("val-humidity").textContent =
    p.humidity_pct != null ? `${Math.round(p.humidity_pct)}%` : "—";
}

function makeSparkline(locationSlug, dateIso, activeHour) {
  const locFeatures = features.filter(
    (f) =>
      f.properties.slug === locationSlug && f.properties.date_iso === dateIso,
  );
  if (locFeatures.length < 2) return "";
  locFeatures.sort((a, b) => {
    const ha = (a.properties && a.properties.hour) || "";
    const hb = (b.properties && b.properties.hour) || "";
    return ha.localeCompare(hb);
  });
  const temps = locFeatures
    .map((f) => f.properties.temp_c)
    .filter((t) => t != null);
  if (temps.length !== locFeatures.length) return "";
  const minTemp = Math.min(...temps);
  const maxTemp = Math.max(...temps);
  const range = maxTemp - minTemp || 1;
  const points = locFeatures
    .map((f, i) => {
      const x = (i / (locFeatures.length - 1)) * 180;
      const y = 25 - ((f.properties.temp_c - minTemp) / range) * 20;
      return `${x},${y}`;
    })
    .join(" ");
  const activeIdx = locFeatures.findIndex(
    (f) => f.properties.hour === activeHour,
  );
  const activePt =
    activeIdx !== -1
      ? `${(activeIdx / (locFeatures.length - 1)) * 180},${25 - ((locFeatures[activeIdx].properties.temp_c - minTemp) / range) * 20}`
      : "";
  let activeMarker = "";
  if (activePt) {
    const [ax, ay] = activePt.split(",");
    activeMarker = `<circle cx="${ax}" cy="${ay}" r="3" fill="#32b7ff" stroke="#fff" stroke-width="1.5" />`;
  }
  return `
    <div class="sparkline-wrap" style="margin-top:12px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.06)">
      <div style="display:flex;justify-content:space-between;font-size:10px;color:#94a3b8;margin-bottom:4px">
        <span>Tren Temp Hari Ini</span>
        <span>${minTemp}°–${maxTemp}°C</span>
      </div>
      <svg width="180" height="30" style="overflow:visible">
        <polyline fill="none" stroke="rgba(50, 183, 255, 0.4)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" points="${points}" />
        ${activeMarker}
      </svg>
    </div>
  `;
}

function ptxt(p) {
  if (!p) return "";
  const escapeText = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  p = {
    ...p,
    location_name: escapeText(p.location_name),
    date: escapeText(p.date),
    hour: escapeText(p.hour),
    risk_label: escapeText(p.risk_label),
    condition: escapeText(p.condition),
  };
  const sparklineHtml = makeSparkline(p.slug, p.date_iso, p.hour);
  const riskColor = colors[p.risk_class || "limited"];
  return `
    <div class="popup-card">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px">
        <b style="font-family:'Outfit'; font-size:14px; color:#fff">${p.location_name}</b>
        <span class="popup-badge" style="background:${riskColor}20; color:${riskColor}; border:1px solid ${riskColor}30">${p.risk_label || "—"}</span>
      </div>
      <div style="font-size:11px; color:#94a3b8; margin-bottom:10px">${p.date} · ${p.hour} WIB</div>
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px; font-size:11px">
        <div style="background:rgba(255,255,255,0.02); padding:5px; border-radius:8px; border:1px solid rgba(255,255,255,0.04)">
          <div style="color:#94a3b8; font-size:9px">SUHU</div>
          <div style="font-weight:700; color:#f8fbff; margin-top:2px">${p.temp_c != null ? p.temp_c + "°C" : "—"}</div>
        </div>
        <div style="background:rgba(255,255,255,0.02); padding:5px; border-radius:8px; border:1px solid rgba(255,255,255,0.04)">
          <div style="color:#94a3b8; font-size:9px">HUJAN</div>
          <div style="font-weight:700; color:#f8fbff; margin-top:2px">${p.rain_probability != null ? LangitPublicData.format(p.rain_probability, "%") : "—"}</div>
        </div>
        <div style="background:rgba(255,255,255,0.02); padding:5px; border-radius:8px; border:1px solid rgba(255,255,255,0.04)">
          <div style="color:#94a3b8; font-size:9px">LEMBAP</div>
          <div style="font-weight:700; color:#f8fbff; margin-top:2px">${p.humidity_pct != null ? p.humidity_pct + "%" : "—"}</div>
        </div>
        <div style="background:rgba(255,255,255,0.02); padding:5px; border-radius:8px; border:1px solid rgba(255,255,255,0.04)">
          <div style="color:#94a3b8; font-size:9px">ANGIN</div>
          <div style="font-weight:700; color:#f8fbff; margin-top:2px">${p.wind_kmh != null ? p.wind_kmh + " km/jam" : "—"}</div>
        </div>
      </div>
      <div style="margin-top:10px; font-size:11px; color:#a8c4e0; font-style:italic; line-height:1.4">
        ${p.condition}
      </div>
      ${sparklineHtml}
    </div>
  `;
}

function updateLegend() {
  const legendEl = document.getElementById("map-legend");
  if (weatherField && weatherField.renderLegend(legendEl)) return;
  let legendHtml = "";
  if (state.activeLayer === "risiko") {
    legendHtml = `
      <div class="legend-title">Indeks aktivitas</div>
      <div><i class="dot" style="background:#10b981"></i>Gangguan rendah</div>
      <div><i class="dot" style="background:#f59e0b"></i>Dipantau</div>
      <div><i class="dot" style="background:#ff9d42"></i>Waspada</div>
      <div><i class="dot" style="background:#ef4444"></i>Gangguan tinggi</div>
    `;
  } else if (state.activeLayer === "temp") {
    legendHtml = `
      <div class="legend-title">Temperatur</div>
      <div><i class="dot" style="background:#3b82f6"></i>&le; 18°C (Sejuk)</div>
      <div><i class="dot" style="background:#60a5fa"></i>19°C - 22°C</div>
      <div><i class="dot" style="background:#f59e0b"></i>23°C - 25°C</div>
      <div><i class="dot" style="background:#f97316"></i>26°C - 28°C</div>
      <div><i class="dot" style="background:#ef4444"></i>&gt; 28°C (Hangat)</div>
    `;
  } else if (state.activeLayer === "rain") {
    legendHtml = `
      <div class="legend-title">Peluang Hujan</div>
      <div><i class="dot" style="background:#3b82f6"></i>Rendah (&le; 20%)</div>
      <div><i class="dot" style="background:#f59e0b"></i>Sedang (21% - 50%)</div>
      <div><i class="dot" style="background:#ef4444"></i>Tinggi (&gt; 50%)</div>
    `;
  } else if (state.activeLayer === "wind") {
    legendHtml = `
      <div class="legend-title">Kecepatan Angin</div>
      <div><i class="dot" style="background:#10b981"></i>&le; 5 km/jam (Teduh)</div>
      <div><i class="dot" style="background:#3b82f6"></i>6 - 12 km/jam (Sepoi)</div>
      <div><i class="dot" style="background:#f59e0b"></i>13 - 20 km/jam (Kencang)</div>
      <div><i class="dot" style="background:#ef4444"></i>&gt; 20 km/jam</div>
    `;
  } else if (state.activeLayer === "humidity") {
    legendHtml = `
      <div class="legend-title">Kelembapan Nisbi</div>
      <div><i class="dot" style="background:#93c5fd"></i>Kering (&le; 55%)</div>
      <div><i class="dot" style="background:#38bdf8"></i>Nyaman (56% - 70%)</div>
      <div><i class="dot" style="background:#0ea5e9"></i>Lembap (71% - 85%)</div>
      <div><i class="dot" style="background:#0284c7"></i>Sangat Lembap</div>
    `;
  } else if (state.activeLayer === "cloud") {
    legendHtml = `
      <div class="legend-title">Tutupan Awan</div>
      <div><i class="dot" style="background:#475569"></i>Cerah (&le; 30%)</div>
      <div><i class="dot" style="background:#94a3b8"></i>Berawan (31% - 60%)</div>
      <div><i class="dot" style="background:#f1f5f9"></i>Mendung (&gt; 60%)</div>
    `;
  }
  legendEl.innerHTML =
    legendHtml +
    '<div><i class="dot" style="background:#94a3b8"></i>Data terbatas</div>';
}

let activeTimeIndex = 0;

function drawForTime(dateIso, activeHour) {
  if (weatherField)
    weatherField.setTime(dateIso + "T" + activeHour + ":00+07:00");
  const range = document.getElementById("forecast-range");
  if (range) {
    range.value = String(activeTimeIndex);
    range.setAttribute("aria-valuetext", dateIso + " " + activeHour + " WIB");
  }
  if (layer) layer.clearLayers();
  const selected = features.filter(
    (f) =>
      f.properties.date_iso === dateIso && f.properties.hour === activeHour,
  );
  const activeLoc = (
    selected.find((f) => f.properties.slug === selectedSlug) || {}
  ).properties || {
    location_name: (
      features.find((f) => f.properties.slug === selectedSlug) || first
    ).properties.location_name,
    date_iso: dateIso,
    hour: activeHour,
    risk_class: "limited",
    condition: "Data jam ini belum tersedia",
  };
  state.windSpeed = activeLoc.wind_kmh ?? null;
  state.cloudPct = activeLoc.cloud_pct ?? null;
  state.tempC = activeLoc.temp_c ?? null;
  state.humidityPct = activeLoc.humidity_pct ?? null;
  state.rainProb = activeLoc.rain_probability ?? null;

  updateHUDValues(activeLoc);
  const selectedPoint = selected.find(
    (f) => f.properties.slug === selectedSlug,
  );
  if (weatherField && selectedPoint)
    weatherField.inspect(
      selectedPoint.geometry.coordinates[1],
      selectedPoint.geometry.coordinates[0],
    );
  if (!layer) return;

  selected.forEach((f) => {
    const p = f.properties;
    const coords = f.geometry.coordinates;
    const latlng = [coords[1], coords[0]];
    const field = {
      temp: "temp_c",
      rain: "rain_probability",
      wind: "wind_kmh",
      humidity: "humidity_pct",
      cloud: "cloud_pct",
    }[state.activeLayer];
    if (field && p[field] == null) {
      L.circleMarker(latlng, {
        radius: 8,
        color: colors.limited,
        fillOpacity: 0.3,
      })
        .bindPopup(ptxt(p))
        .addTo(layer);
      return;
    }

    if (state.activeLayer === "risiko") {
      const cls = p.risk_class || "limited";
      const color = colors[cls];
      const size = 12 + (p.risk_score || 0) * 0.18;
      const iconHtml = `
        <div class="pulse-marker" style="--marker-color:${color}; --pulse-size:${size * 3}px">
          <div class="pulse-dot" style="width:${size}px; height:${size}px"></div>
          <div class="pulse-ring"></div>
        </div>
      `;
      L.marker(latlng, {
        title: p.location_name + " " + p.date_iso + " " + p.hour + " WIB",
        icon: L.divIcon({
          html: iconHtml,
          className: "custom-leaflet-marker",
          iconSize: [size * 3, size * 3],
          iconAnchor: [size * 1.5, size * 1.5],
        }),
      })
        .bindPopup(ptxt(p))
        .on("click", () => selectForecast(p))
        .addTo(layer);
    } else if (state.activeLayer === "temp") {
      const temp = p.temp_c;
      const color = getTempColor(temp);
      const iconHtml = `
        <div class="temp-badge" style="background:${color}; box-shadow:0 0 15px ${color}80">
          ${Math.round(temp)}°C
        </div>
      `;
      L.marker(latlng, {
        title: p.location_name + " " + p.date_iso + " " + p.hour + " WIB",
        icon: L.divIcon({
          html: iconHtml,
          className: "custom-leaflet-marker",
          iconSize: [45, 24],
          iconAnchor: [22, 12],
        }),
      })
        .bindPopup(ptxt(p))
        .on("click", () => selectForecast(p))
        .addTo(layer);
    } else if (state.activeLayer === "rain") {
      const rain = p.rain_probability;
      const radarColor =
        rain > 50 ? "#ef4444" : rain > 20 ? "#f59e0b" : "#3b82f6";
      const iconHtml = `
        <div class="radar-marker" style="--radar-color:${radarColor}">
          <div class="radar-ping"></div>
          <div class="radar-core"></div>
        </div>
      `;
      L.marker(latlng, {
        title: p.location_name + " " + p.date_iso + " " + p.hour + " WIB",
        icon: L.divIcon({
          html: iconHtml,
          className: "custom-leaflet-marker",
          iconSize: [60, 60],
          iconAnchor: [30, 30],
        }),
      })
        .bindPopup(ptxt(p))
        .on("click", () => selectForecast(p))
        .addTo(layer);
    } else if (state.activeLayer === "wind") {
      const wind = p.wind_kmh;
      const color = getWindColor(wind);
      const rotation = LangitPublicData.windRotation(p.wind_dir, wind);
      const iconHtml = `
        <div class="wind-badge" style="background:${color}; box-shadow:0 0 15px ${color}80; display:flex; align-items:center; gap:4px; padding:4px 8px; border-radius:12px; border:1px solid rgba(255,255,255,0.2); color:#fff; font-family:'Outfit',sans-serif; font-size:11px; font-weight:800; white-space:nowrap;">
          ${rotation == null ? "" : `<span title="Arah aliran angin" style="display:inline-block; transform:rotate(${rotation}deg);">➔</span>`}
          ${wind.toFixed(1)} km/jam
        </div>
      `;
      L.marker(latlng, {
        title: p.location_name + " " + p.date_iso + " " + p.hour + " WIB",
        icon: L.divIcon({
          html: iconHtml,
          className: "custom-leaflet-marker",
          iconSize: [80, 24],
          iconAnchor: [40, 12],
        }),
      })
        .bindPopup(ptxt(p))
        .on("click", () => selectForecast(p))
        .addTo(layer);
    } else if (state.activeLayer === "humidity") {
      const hum = p.humidity_pct;
      const color = getHumidityColor(hum);
      const size = 15 + hum * 0.15;
      const iconHtml = `
        <div class="humidity-halo" style="background:${color}; opacity:${0.1 + hum / 200}; width:${size}px; height:${size}px; filter:blur(4px)">
          <div style="width:6px; height:6px; background:#fff; border-radius:50%; margin:auto; position:absolute; inset:0"></div>
        </div>
      `;
      L.marker(latlng, {
        title: p.location_name + " " + p.date_iso + " " + p.hour + " WIB",
        icon: L.divIcon({
          html: iconHtml,
          className: "custom-leaflet-marker",
          iconSize: [size, size],
          iconAnchor: [size / 2, size / 2],
        }),
      })
        .bindPopup(ptxt(p))
        .on("click", () => selectForecast(p))
        .addTo(layer);
    } else if (state.activeLayer === "cloud") {
      const cld = p.cloud_pct;
      const color = getCloudColor(cld);
      const opacity = 0.2 + cld / 150;
      const iconHtml = `
        <div class="cloud-glow" style="background:${color}; opacity:${opacity}; width:32px; height:32px; border-radius:50%; filter:blur(6px); box-shadow:0 0 10px ${color}">
          <div style="width:4px; height:4px; background:#fff; border-radius:50%; margin:auto; position:absolute; inset:0"></div>
        </div>
      `;
      L.marker(latlng, {
        title: p.location_name + " " + p.date_iso + " " + p.hour + " WIB",
        icon: L.divIcon({
          html: iconHtml,
          className: "custom-leaflet-marker",
          iconSize: [32, 32],
          iconAnchor: [16, 16],
        }),
      })
        .bindPopup(ptxt(p))
        .on("click", () => selectForecast(p))
        .addTo(layer);
    }
  });
}

function switchLayer(layerName) {
  if (
    !["risiko", "temp", "rain", "wind", "humidity", "cloud"].includes(layerName)
  )
    return;
  state.activeLayer = layerName;
  if (weatherField) weatherField.setLayer(layerName);
  document
    .querySelectorAll(".param-tab")
    .forEach((tab) =>
      tab.setAttribute("aria-pressed", String(tab.dataset.layer === layerName)),
    );
  document.querySelectorAll(".param-tab").forEach((tab) => {
    if (tab.getAttribute("data-layer") === layerName) {
      tab.classList.add("active");
    } else {
      tab.classList.remove("active");
    }
  });

  updateLegend();

  const pills = document.querySelectorAll(".time-pill");
  if (pills.length && pills[activeTimeIndex]) {
    const activePill = pills[activeTimeIndex];
    const dateIso = activePill.getAttribute("data-date");
    const hourVal = activePill.getAttribute("data-hour");
    drawForTime(dateIso, hourVal);
  }
}

let playInterval = null;
function togglePlay() {
  const playBtn = document.getElementById("play-btn");
  if (!document.querySelectorAll(".time-pill").length) return;
  playBtn.setAttribute(
    "aria-label",
    playInterval ? "Putar timeline" : "Jeda timeline",
  );
  playBtn.setAttribute("aria-pressed", String(!playInterval));
  if (playInterval) {
    clearInterval(playInterval);
    playInterval = null;
    playBtn.innerHTML = "&#9654;";
  } else {
    playBtn.innerHTML = "&#10074;&#10074;";
    const pills = document.querySelectorAll(".time-pill");
    playInterval = setInterval(() => {
      activeTimeIndex = (activeTimeIndex + 1) % pills.length;
      pills[activeTimeIndex].click();
      pills[activeTimeIndex].scrollIntoView({
        behavior: "smooth",
        block: "nearest",
        inline: "center",
      });
    }, 2500);
  }
}

let map = null,
  layer = null;
let selectedSlug = first.properties.slug || "";
const timeline = LangitPublicData.timeline(features);
const scrubber = document.getElementById("time-scrubber");
const locationSelect = document.getElementById("map-location");
const seenLocations = new Set();
features.forEach((f) => {
  const p = f.properties;
  if (seenLocations.has(p.slug)) return;
  seenLocations.add(p.slug);
  const option = document.createElement("option");
  option.value = p.slug;
  option.textContent = p.location_name;
  locationSelect.appendChild(option);
});
function selectForecast(p) {
  selectedSlug = p.slug;
  locationSelect.value = selectedSlug;
  updateHUDValues(p);
}
locationSelect.addEventListener("change", () => {
  selectedSlug = locationSelect.value;
  const slot = timeline[activeTimeIndex];
  if (slot) drawForTime(slot.date_iso, slot.hour);
});
const nowParts = new Intl.DateTimeFormat("sv-SE", {
  timeZone: "Asia/Jakarta",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
})
  .format(new Date())
  .replace(" ", "T");
const nextIndex = timeline.findIndex(
  (p) => p.date_iso + "T" + p.hour >= nowParts,
);
activeTimeIndex = Math.max(0, nextIndex);
timeline.forEach((slot, index) => {
  const pill = document.createElement("button");
  pill.className = "time-pill";
  pill.dataset.date = slot.date_iso;
  pill.dataset.hour = slot.hour;
  pill.type = "button";
  const label = document.createElement("span");
  label.className = "day-label";
  label.textContent = new Intl.DateTimeFormat("id-ID", {
    timeZone: "Asia/Jakarta",
    day: "numeric",
    month: "short",
  }).format(new Date(slot.date_iso + "T00:00:00+07:00"));
  pill.setAttribute("aria-label", slot.date_iso + " " + slot.hour + " WIB");
  const clock = document.createElement("strong");
  clock.textContent = slot.hour;
  pill.append(label, clock);
  pill.onclick = () => {
    document.querySelectorAll(".time-pill").forEach((button) => {
      button.classList.remove("active");
      button.setAttribute("aria-pressed", "false");
    });
    pill.classList.add("active");
    pill.setAttribute("aria-pressed", "true");
    activeTimeIndex = index;
    drawForTime(slot.date_iso, slot.hour);
  };
  scrubber.appendChild(pill);
});
function renderSelectedTime() {
  const pills = document.querySelectorAll(".time-pill");
  if (pills.length) pills[activeTimeIndex].click();
  else {
    document.getElementById("hud-location-name").textContent =
      "Data prakiraan belum tersedia";
    document.getElementById("hud-time-label").textContent =
      "Gunakan tautan prakiraan atau data di bawah.";
    document.getElementById("play-btn").disabled = true;
  }
}
updateLegend();
renderSelectedTime();
try {
  map = L.map("map", {
    scrollWheelZoom: true,
    maxBounds: [
      [-11.25, 94],
      [6.45, 141.25],
    ],
    maxBoundsViscosity: 0.8,
    minZoom: 5,
    zoomControl: false,
  });
  const tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>',
  });
  tiles.on("tileerror", () => {
    const warning = document.getElementById("map-fallback");
    warning.hidden = false;
    document.getElementById("field-inspector").hidden = true;
    warning.querySelector("span").textContent =
      "Peta dasar belum dapat dimuat. Data titik prakiraan tetap tersedia.";
  });
  tiles.addTo(map);
  L.control.zoom({ position: "bottomleft" }).addTo(map);
  layer = L.layerGroup().addTo(map);
  if (features.length)
    map.fitBounds(
      L.latLngBounds(
        features.map((f) => [
          f.geometry.coordinates[1],
          f.geometry.coordinates[0],
        ]),
      ),
      { padding: [50, 50], maxZoom: 13 },
    );
  else map.setView(center, 12);
  if (window.LangitWeatherField)
    weatherField = window.LangitWeatherField.create(map, L, {
      url: window.LANGIT_FIELD_URL,
      layer: state.activeLayer,
      status: document.getElementById("field-status"),
      toggle: document.getElementById("wind-animation"),
      inspector: document.getElementById("field-inspector"),
      onChange: updateLegend,
    });
  if (window.matchMedia && window.matchMedia("(max-width:768px)").matches)
    document.querySelector(".point-details").open = false;
  renderSelectedTime();
} catch (error) {
  console.error("Map initialization failed", error);
  document.getElementById("map-fallback").hidden = false;
  document.getElementById("field-status").textContent =
    "Lapisan peta tidak tersedia.";
}
function setMapPanel(open) {
  const body = document.getElementById("map-controls");
  body.hidden = !open;
  document
    .getElementById("map-panel-toggle")
    .setAttribute("aria-expanded", String(open));
}
if (window.matchMedia && window.matchMedia("(max-width:768px)").matches)
  setMapPanel(false);
document.getElementById("map-panel-toggle").addEventListener("click", () => {
  setMapPanel(document.getElementById("map-controls").hidden);
});
const forecastRange = document.getElementById("forecast-range");
forecastRange.max = String(Math.max(0, timeline.length - 1));
forecastRange.disabled = !timeline.length;
forecastRange.addEventListener("input", () => {
  activeTimeIndex = Number(forecastRange.value);
  renderSelectedTime();
});
document.addEventListener("keydown", (event) => {
  if (!event.target.closest(".timeline-container") || !timeline.length) return;
  if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
  event.preventDefault();
  activeTimeIndex =
    (activeTimeIndex + (event.key === "ArrowRight" ? 1 : timeline.length - 1)) %
    timeline.length;
  renderSelectedTime();
});
window.addEventListener("pagehide", () => {
  if (playInterval) clearInterval(playInterval);
});

// Sync with postMessage from parent dashboard window
window.addEventListener("message", (e) => {
  if (e.origin !== window.location.origin || e.source !== window.parent) return;
  if (e.data && e.data.type === "invalidateSize" && map) map.invalidateSize();
  if (e.data && e.data.type === "switchLayer") {
    switchLayer(e.data.layer);
  }
});

// Hide the back button if embedded inside an iframe
if (window.self !== window.top) {
  const backBtn = document.getElementById("hud-back-btn");
  if (backBtn) backBtn.style.display = "none";
}
