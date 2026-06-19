# Script to modify langit_v65_cinematic_rebuild.py
import re
from pathlib import Path

target_file = Path('c:/Users/itsme/Documents/Projects/weather-forecast/langit_v65_cinematic_rebuild.py')
content = target_file.read_text(encoding='utf-8')

start_match = re.search(r'^def v65_map_page\(', content, re.MULTILINE)
if not start_match:
    print("Could not find def v65_map_page")
    exit(1)

start_idx = start_match.start()

# Find the next top-level function definition
end_match = re.search(r'^def v65_', content[start_idx + 16:], re.MULTILINE)
if not end_match:
    print("Could not find next function")
    exit(1)

end_idx = start_idx + 16 + end_match.start()

# Now let's define our replacement v65_map_page code
replacement_code = '''def v65_map_page(title: str, geojson: Dict[str, Any], back_href: str) -> str:
    data = json.dumps(geojson, ensure_ascii=False)
    css = r\'''html,body,#map{height:100%;margin:0;background:#030712;color:#f8fbff;font-family:\'Inter\',system-ui,-apple-system,sans-serif;overflow:hidden}
#particle-canvas{position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:800}
.hud{position:absolute;z-index:1000;left:24px;top:24px;width:min(340px,calc(100% - 48px));padding:24px;border-radius:24px;background:rgba(7,14,30,0.75);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,0.08);box-shadow:0 24px 80px rgba(0,0,0,0.5),inset 0 1px 1px rgba(255,255,255,0.1);animation:slideIn 0.6s cubic-bezier(0.16,1,0.3,1) both}
.hud-brand{font-family:\'Outfit\',sans-serif;font-weight:800;font-size:10px;letter-spacing:0.1em;color:#32b7ff;text-transform:uppercase}
.hud-version{font-family:\'Outfit\',sans-serif;font-weight:600;font-size:10px;color:rgba(255,255,255,0.3)}
.hud h1{font-family:\'Outfit\',sans-serif;font-size:24px;font-weight:800;letter-spacing:-0.02em;line-height:1.2;margin:12px 0 4px;color:#fff}
.hud-meta{color:#6b8ab5;font-size:13px;font-weight:500}
.hud-divider{height:1px;background:linear-gradient(90deg,rgba(255,255,255,0.08) 0%,rgba(255,255,255,0) 100%);margin:16px 0}
.hud-stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.hud-stat-box{background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.05);padding:10px 14px;border-radius:14px;display:flex;flex-direction:column;gap:2px}
.hud-stat-val{font-family:\'Outfit\',sans-serif;font-size:18px;font-weight:800;color:#fff}
.hud-stat-lbl{font-size:10px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:0.05em}
.parameter-tabs{display:flex;gap:4px;margin-top:16px;background:rgba(3,7,18,0.6);padding:3px;border-radius:12px;border:1px solid rgba(255,255,255,0.05)}
.param-tab{flex:1;border:none;background:none;color:#6b8ab5;font-family:inherit;font-size:10px;font-weight:700;padding:8px 0;border-radius:9px;cursor:pointer;transition:all 0.2s ease;text-align:center}
.param-tab:hover{color:#fff;background:rgba(255,255,255,0.04)}
.param-tab.active{background:rgba(255,255,255,0.08);color:#fff;box-shadow:0 2px 8px rgba(0,0,0,0.2)}
.btn{display:inline-flex;align-items:center;justify-content:center;padding:12px 24px;border-radius:999px;background:rgba(50,183,255,0.12);border:1px solid rgba(50,183,255,0.25);color:#32b7ff;text-decoration:none;font-weight:700;font-size:13px;transition:all 0.3s cubic-bezier(0.4,0,0.2,1);cursor:pointer;font-family:inherit}
.btn:hover{background:rgba(50,183,255,0.2);border-color:rgba(50,183,255,0.4);transform:translateY(-1px);box-shadow:0 4px 12px rgba(50,183,255,0.15)}
.timeline-container{position:absolute;z-index:1000;left:50%;bottom:24px;transform:translateX(-50%);width:min(800px,calc(100% - 48px));padding:12px 20px;border-radius:24px;background:rgba(7,14,30,0.75);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border:1px solid rgba(255,255,255,0.08);box-shadow:0 24px 80px rgba(0,0,0,0.5);display:flex;align-items:center;gap:16px;animation:slideUp 0.6s cubic-bezier(0.16,1,0.3,1) both}
.play-btn{background:rgba(50,183,255,0.12);border:1px solid rgba(50,183,255,0.25);color:#32b7ff;width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;cursor:pointer;transition:all 0.3s ease;flex-shrink:0;font-size:14px}
.play-btn:hover{background:rgba(50,183,255,0.2);transform:scale(1.05)}
.time-scrubber{flex:1;display:flex;overflow-x:auto;gap:8px;padding:4px 0;scrollbar-width:none}
.time-scrubber::-webkit-scrollbar{display:none}
.time-pill{flex-shrink:0;padding:8px 16px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.05);border-radius:14px;color:#8fa0dd;font-family:inherit;font-size:13px;font-weight:700;cursor:pointer;transition:all 0.2s ease;display:flex;flex-direction:column;align-items:center;gap:2px}
.time-pill:hover{background:rgba(255,255,255,0.06)}
.time-pill span.day-label{font-size:9px;color:#64748b;text-transform:uppercase;font-weight:600}
.time-pill.active{background:linear-gradient(135deg,rgba(50,183,255,0.2) 0%,rgba(50,183,255,0.05) 100%);border-color:rgba(50,183,255,0.4);color:#32b7ff;box-shadow:0 4px 12px rgba(50,183,255,0.15)}
.legend{position:absolute;right:24px;bottom:24px;z-index:1000;background:rgba(7,14,30,0.75);border:1px solid rgba(255,255,255,0.08);border-radius:20px;padding:16px;font-size:11px;backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);box-shadow:0 12px 40px rgba(0,0,0,0.4);width:150px;animation:slideIn 0.6s cubic-bezier(0.16,1,0.3,1) both}
.legend-title{font-family:\'Outfit\',sans-serif;font-weight:800;color:#fff;margin-bottom:10px;text-transform:uppercase;letter-spacing:0.05em;font-size:9px}
.legend div{display:flex;gap:10px;align-items:center;margin:8px 0;color:#a8c4e0;font-weight:500}
.dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.leaflet-control-attribution{background:rgba(3,7,12,0.85)!important;color:#6b8ab5!important;font-size:10px!important}
.leaflet-popup-content-wrapper{background:rgba(7,14,30,0.92)!important;color:#f8fbff!important;border-radius:20px!important;border:1px solid rgba(255,255,255,0.08)!important;backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);box-shadow:0 20px 50px rgba(0,0,0,0.6)!important}
.leaflet-popup-tip{background:rgba(7,14,30,0.92)!important;border-left:1px solid rgba(255,255,255,0.08)!important;border-bottom:1px solid rgba(255,255,255,0.08)!important}
.leaflet-popup-content{margin:16px 20px!important;width:200px!important}
.popup-badge{padding:3px 8px;border-radius:999px;font-family:\'Outfit\',sans-serif;font-size:10px;font-weight:700;text-transform:uppercase}
.custom-leaflet-marker{background:none;border:none;display:flex;align-items:center;justify-content:center}
.pulse-marker{position:relative;display:flex;align-items:center;justify-content:center;width:var(--pulse-size);height:var(--pulse-size)}
.pulse-dot{background:var(--marker-color);border-radius:50%;box-shadow:0 0 12px var(--marker-color);border:1.5px solid #fff;z-index:2}
.pulse-ring{position:absolute;width:100%;height:100%;border:2px solid var(--marker-color);border-radius:50%;animation:pulse-ring-animation 2s cubic-bezier(0.215,0.61,0.355,1) infinite;opacity:0;z-index:1}
@keyframes pulse-ring-animation{0%{transform:scale(0.3);opacity:0}50%{opacity:0.5}100%{transform:scale(1);opacity:0}}
.temp-badge{color:#fff;font-family:\'Outfit\',sans-serif;font-size:11px;font-weight:800;padding:4px 8px;border-radius:12px;border:1px solid rgba(255,255,255,0.2);display:flex;align-items:center;justify-content:center;white-space:nowrap}
.radar-marker{position:relative;width:60px;height:60px;display:flex;align-items:center;justify-content:center}
.radar-core{width:8px;height:8px;background:var(--radar-color);border-radius:50%;box-shadow:0 0 10px var(--radar-color);z-index:2;border:1px solid #fff}
.radar-ping{position:absolute;width:100%;height:100%;border:1.5px solid var(--radar-color);border-radius:50%;animation:radar-ping-animation 2s linear infinite;opacity:0}
@keyframes radar-ping-animation{0%{transform:scale(0.1);opacity:0.8}100%{transform:scale(1);opacity:0}}
.humidity-halo{border-radius:50%;position:relative;display:flex;align-items:center;justify-content:center}
.cloud-glow{border-radius:50%;position:relative;display:flex;align-items:center;justify-content:center}
@keyframes slideIn{from{transform:translateX(-20px);opacity:0}to{transform:translateX(0);opacity:1}}
@keyframes slideUp{from{transform:translate(-50%,20px);opacity:0}to{transform:translate(-50%,0);opacity:1}}
@media(max-width:768px){
  .hud{left:12px;top:12px;width:calc(100% - 24px);padding:16px;border-radius:20px}
  .timeline-container{bottom:16px;width:calc(100% - 24px);padding:10px;border-radius:20px}
  .legend{right:12px;bottom:96px;width:120px;padding:12px;border-radius:16px}
}
\'''
    js = r\'''
const data = __DATA__;
const colors = {safe:\'#10b981\', watch:\'#f59e0b\', rain:\'#ff9d42\', danger:\'#ef4444\', limited:\'#94a3b8\'};
const features = (data && data.features) ? data.features : [];

// Find first coordinate center
const first = features[0] || {geometry:{coordinates:[106.8,-6.2]}, properties:{location_name:\'Lokasi\'}};
const center = first.geometry && first.geometry.coordinates ? [first.geometry.coordinates[1], first.geometry.coordinates[0]] : [-6.2,106.8];

// Particle Engine live states
const state = {
  windSpeed: 2.0,
  cloudPct: 40.0,
  tempC: 22.0,
  humidityPct: 70.0,
  rainProb: 0.0,
  activeLayer: \'risiko\'
};

function getTempColor(t) {
  if (t <= 18) return \'#3b82f6\';
  if (t <= 22) return \'#60a5fa\';
  if (t <= 25) return \'#f59e0b\';
  if (t <= 28) return \'#f97316\';
  return \'#ef4444\';
}

function getHumidityColor(h) {
  if (h <= 55) return \'#93c5fd\';
  if (h <= 70) return \'#38bdf8\';
  if (h <= 85) return \'#0ea5e9\';
  return \'#0284c7\';
}

function getCloudColor(c) {
  if (c <= 30) return \'#475569\';
  if (c <= 60) return \'#94a3b8\';
  return \'#f1f5f9\';
}

function getWindColor(w) {
  if (w <= 5) return \'#10b981\';
  if (w <= 12) return \'#3b82f6\';
  if (w <= 20) return \'#f59e0b\';
  return \'#ef4444\';
}

function updateHUDValues(p) {
  document.getElementById(\'hud-location-name\').textContent = p.location_name || \'Lokasi\';
  document.getElementById(\'hud-time-label\').textContent = `${p.relative || \'Prakiraan\'} · ${p.hour || \'\'}`;
  document.getElementById(\'val-temp\').textContent = p.temp_c != null ? `${Math.round(p.temp_c)}°C` : \'—\';
  document.getElementById(\'val-rain\').textContent = p.rain_probability != null ? `${Math.round(p.rain_probability)}%` : \'—\';
  document.getElementById(\'val-wind\').textContent = p.wind_kmh != null ? `${p.wind_kmh.toFixed(1)} km/jam` : \'—\';
  document.getElementById(\'val-humidity\').textContent = p.humidity_pct != null ? `${Math.round(p.humidity_pct)}%` : \'—\';
}

function makeSparkline(locationSlug, dateIso, activeHour) {
  const locFeatures = features.filter(f => f.properties.slug === locationSlug && f.properties.date_iso === dateIso);
  if (locFeatures.length < 2) return \'\';
  locFeatures.sort((a,b) => {
    const ha = (a.properties && a.properties.hour) || \'\';
    const hb = (b.properties && b.properties.hour) || \'\';
    return ha.localeCompare(hb);
  });
  const temps = locFeatures.map(f => f.properties.temp_c || 20);
  const minTemp = Math.min(...temps);
  const maxTemp = Math.max(...temps);
  const range = maxTemp - minTemp || 1;
  const points = locFeatures.map((f, i) => {
    const x = (i / (locFeatures.length - 1)) * 180;
    const y = 25 - ((f.properties.temp_c - minTemp) / range) * 20;
    return `${x},${y}`;
  }).join(\' \');
  const activeIdx = locFeatures.findIndex(f => f.properties.hour === activeHour);
  const activePt = activeIdx !== -1 ? `${(activeIdx / (locFeatures.length - 1)) * 180},${25 - ((locFeatures[activeIdx].properties.temp_c - minTemp) / range) * 20}` : \'\';
  let activeMarker = \'\';
  if (activePt) {
    const [ax, ay] = activePt.split(\',\');
    activeMarker = `<circle cx="${ax}" cy="${ay}" r="3" fill="#32b7ff" stroke="#fff" stroke-width="1.5" />`;
  }
  return `
    <div class="sparkline-wrap" style="margin-top:12px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.06)">
      <div style="display:flex;justify-content:space-between;font-size:10px;color:#64748b;margin-bottom:4px">
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
  if (!p) return \'\';
  const sparklineHtml = makeSparkline(p.slug, p.date_iso, p.hour);
  const riskColor = colors[p.risk_class || \'limited\'];
  return `
    <div class="popup-card">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px">
        <b style="font-family:\'Outfit\'; font-size:14px; color:#fff">${p.location_name}</b>
        <span class="popup-badge" style="background:${riskColor}20; color:${riskColor}; border:1px solid ${riskColor}30">${p.risk_label || \'—\'}</span>
      </div>
      <div style="font-size:11px; color:#6b8ab5; margin-bottom:10px">${p.date} · ${p.hour} WIB</div>
      <div style="display:grid; grid-template-columns:1fr 1fr; gap:6px; font-size:11px">
        <div style="background:rgba(255,255,255,0.02); padding:5px; border-radius:8px; border:1px solid rgba(255,255,255,0.04)">
          <div style="color:#64748b; font-size:9px">SUHU</div>
          <div style="font-weight:700; color:#f8fbff; margin-top:2px">${p.temp_c != null ? p.temp_c + \'°C\' : \'—\'}</div>
        </div>
        <div style="background:rgba(255,255,255,0.02); padding:5px; border-radius:8px; border:1px solid rgba(255,255,255,0.04)">
          <div style="color:#64748b; font-size:9px">HUJAN</div>
          <div style="font-weight:700; color:#f8fbff; margin-top:2px">${p.rain_probability != null ? p.rain_probability + \'%\' : \'—\'}</div>
        </div>
        <div style="background:rgba(255,255,255,0.02); padding:5px; border-radius:8px; border:1px solid rgba(255,255,255,0.04)">
          <div style="color:#64748b; font-size:9px">LEMBAP</div>
          <div style="font-weight:700; color:#f8fbff; margin-top:2px">${p.humidity_pct != null ? p.humidity_pct + \'%\' : \'—\'}</div>
        </div>
        <div style="background:rgba(255,255,255,0.02); padding:5px; border-radius:8px; border:1px solid rgba(255,255,255,0.04)">
          <div style="color:#64748b; font-size:9px">ANGIN</div>
          <div style="font-weight:700; color:#f8fbff; margin-top:2px">${p.wind_kmh != null ? p.wind_kmh + \' km/jam\' : \'—\'}</div>
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
  const legendEl = document.getElementById(\'map-legend\');
  let legendHtml = \'\';
  if (state.activeLayer === \'risiko\') {
    legendHtml = `
      <div class="legend-title">Indeks Risiko</div>
      <div><i class="dot" style="background:#10b981"></i>Aman</div>
      <div><i class="dot" style="background:#f59e0b"></i>Dipantau</div>
      <div><i class="dot" style="background:#ff9d42"></i>Waspada</div>
      <div><i class="dot" style="background:#ef4444"></i>Bahaya</div>
    `;
  } else if (state.activeLayer === \'temp\') {
    legendHtml = `
      <div class="legend-title">Temperatur</div>
      <div><i class="dot" style="background:#3b82f6"></i>&le; 18°C (Sejuk)</div>
      <div><i class="dot" style="background:#60a5fa"></i>19°C - 22°C</div>
      <div><i class="dot" style="background:#f59e0b"></i>23°C - 25°C</div>
      <div><i class="dot" style="background:#f97316"></i>26°C - 28°C</div>
      <div><i class="dot" style="background:#ef4444"></i>&gt; 28°C (Hangat)</div>
    `;
  } else if (state.activeLayer === \'rain\') {
    legendHtml = `
      <div class="legend-title">Peluang Hujan</div>
      <div><i class="dot" style="background:#3b82f6"></i>Rendah (&le; 20%)</div>
      <div><i class="dot" style="background:#f59e0b"></i>Sedang (21% - 50%)</div>
      <div><i class="dot" style="background:#ef4444"></i>Tinggi (&gt; 50%)</div>
    `;
  } else if (state.activeLayer === \'wind\') {
    legendHtml = `
      <div class="legend-title">Kecepatan Angin</div>
      <div><i class="dot" style="background:#10b981"></i>&le; 5 km/jam (Teduh)</div>
      <div><i class="dot" style="background:#3b82f6"></i>6 - 12 km/jam (Sepoi)</div>
      <div><i class="dot" style="background:#f59e0b"></i>13 - 20 km/jam (Kencang)</div>
      <div><i class="dot" style="background:#ef4444"></i>&gt; 20 km/jam (Bahaya)</div>
    `;
  } else if (state.activeLayer === \'humidity\') {
    legendHtml = `
      <div class="legend-title">Kelembapan Nisbi</div>
      <div><i class="dot" style="background:#93c5fd"></i>Kering (&le; 55%)</div>
      <div><i class="dot" style="background:#38bdf8"></i>Nyaman (56% - 70%)</div>
      <div><i class="dot" style="background:#0ea5e9"></i>Lembap (71% - 85%)</div>
      <div><i class="dot" style="background:#0284c7"></i>Sangat Lembap</div>
    `;
  } else if (state.activeLayer === \'cloud\') {
    legendHtml = `
      <div class="legend-title">Tutupan Awan</div>
      <div><i class="dot" style="background:#475569"></i>Cerah (&le; 30%)</div>
      <div><i class="dot" style="background:#94a3b8"></i>Berawan (31% - 60%)</div>
      <div><i class="dot" style="background:#f1f5f9"></i>Mendung (&gt; 60%)</div>
    `;
  }
  legendEl.innerHTML = legendHtml;
}

let activeTimeIndex = 0;

function drawForTime(dateIso, activeHour) {
  layer.clearLayers();
  const selected = features.filter(f => f.properties.date_iso === dateIso && f.properties.hour === activeHour);
  if (!selected.length) return;

  const activeLoc = selected[0].properties;
  state.windSpeed = activeLoc.wind_kmh || 2.0;
  state.cloudPct = activeLoc.cloud_pct || 40.0;
  state.tempC = activeLoc.temp_c || 24.0;
  state.humidityPct = activeLoc.humidity_pct || 70.0;
  state.rainProb = activeLoc.rain_probability || 0.0;

  updateHUDValues(activeLoc);

  selected.forEach(f => {
    const p = f.properties;
    const coords = f.geometry.coordinates;
    const latlng = [coords[1], coords[0]];

    if (state.activeLayer === \'risiko\') {
      const cls = p.risk_class || \'limited\';
      const color = colors[cls];
      const size = 12 + (p.risk_score || 0) * 0.18;
      const iconHtml = `
        <div class="pulse-marker" style="--marker-color:${color}; --pulse-size:${size * 3}px">
          <div class="pulse-dot" style="width:${size}px; height:${size}px"></div>
          <div class="pulse-ring"></div>
        </div>
      `;
      L.marker(latlng, {
        icon: L.divIcon({
          html: iconHtml,
          className: \'custom-leaflet-marker\',
          iconSize: [size * 3, size * 3],
          iconAnchor: [size * 1.5, size * 1.5]
        })
      }).bindPopup(ptxt(p)).addTo(layer);

    } else if (state.activeLayer === \'temp\') {
      const temp = p.temp_c || 24;
      const color = getTempColor(temp);
      const iconHtml = `
        <div class="temp-badge" style="background:${color}; box-shadow:0 0 15px ${color}80">
          ${Math.round(temp)}°C
        </div>
      `;
      L.marker(latlng, {
        icon: L.divIcon({
          html: iconHtml,
          className: \'custom-leaflet-marker\',
          iconSize: [45, 24],
          iconAnchor: [22, 12]
        })
      }).bindPopup(ptxt(p)).addTo(layer);

    } else if (state.activeLayer === \'rain\') {
      const rain = p.rain_probability || 0;
      const radarColor = rain > 50 ? \'#ef4444\' : (rain > 20 ? \'#f59e0b\' : \'#3b82f6\');
      const iconHtml = `
        <div class="radar-marker" style="--radar-color:${radarColor}">
          <div class="radar-ping"></div>
          <div class="radar-core"></div>
        </div>
      `;
      L.marker(latlng, {
        icon: L.divIcon({
          html: iconHtml,
          className: \'custom-leaflet-marker\',
          iconSize: [60, 60],
          iconAnchor: [30, 30]
        })
      }).bindPopup(ptxt(p)).addTo(layer);

    } else if (state.activeLayer === \'wind\') {
      const wind = p.wind_kmh || 0;
      const color = getWindColor(wind);
      const rotation = (coords[0] * 33 + coords[1] * 77) % 360;
      const iconHtml = `
        <div class="wind-badge" style="background:${color}; box-shadow:0 0 15px ${color}80; display:flex; align-items:center; gap:4px; padding:4px 8px; border-radius:12px; border:1px solid rgba(255,255,255,0.2); color:#fff; font-family:\'Outfit\',sans-serif; font-size:11px; font-weight:800; white-space:nowrap;">
          <span style="display:inline-block; transform:rotate(${rotation}deg);">➔</span>
          ${wind.toFixed(1)} km/jam
        </div>
      `;
      L.marker(latlng, {
        icon: L.divIcon({
          html: iconHtml,
          className: \'custom-leaflet-marker\',
          iconSize: [80, 24],
          iconAnchor: [40, 12]
        })
      }).bindPopup(ptxt(p)).addTo(layer);

    } else if (state.activeLayer === \'humidity\') {
      const hum = p.humidity_pct || 70;
      const color = getHumidityColor(hum);
      const size = 15 + hum * 0.15;
      const iconHtml = `
        <div class="humidity-halo" style="background:${color}; opacity:${0.1 + (hum/200)}; width:${size}px; height:${size}px; filter:blur(4px)">
          <div style="width:6px; height:6px; background:#fff; border-radius:50%; margin:auto; position:absolute; inset:0"></div>
        </div>
      `;
      L.marker(latlng, {
        icon: L.divIcon({
          html: iconHtml,
          className: \'custom-leaflet-marker\',
          iconSize: [size, size],
          iconAnchor: [size/2, size/2]
        })
      }).bindPopup(ptxt(p)).addTo(layer);

    } else if (state.activeLayer === \'cloud\') {
      const cld = p.cloud_pct || 40;
      const color = getCloudColor(cld);
      const opacity = 0.2 + (cld / 150);
      const iconHtml = `
        <div class="cloud-glow" style="background:${color}; opacity:${opacity}; width:32px; height:32px; border-radius:50%; filter:blur(6px); box-shadow:0 0 10px ${color}">
          <div style="width:4px; height:4px; background:#fff; border-radius:50%; margin:auto; position:absolute; inset:0"></div>
        </div>
      `;
      L.marker(latlng, {
        icon: L.divIcon({
          html: iconHtml,
          className: \'custom-leaflet-marker\',
          iconSize: [32, 32],
          iconAnchor: [16, 16]
        })
      }).bindPopup(ptxt(p)).addTo(layer);
    }
  });
}

function switchLayer(layerName) {
  state.activeLayer = layerName;
  document.querySelectorAll(\'.param-tab\').forEach(tab => {
    if (tab.getAttribute(\'data-layer\') === layerName) {
      tab.classList.add(\'active\');
    } else {
      tab.classList.remove(\'active\');
    }
  });
  
  updateLegend();
  
  const pills = document.querySelectorAll(\'.time-pill\');
  if (pills.length && pills[activeTimeIndex]) {
    const activePill = pills[activeTimeIndex];
    const dateIso = activePill.getAttribute(\'data-date\');
    const hourVal = activePill.getAttribute(\'data-hour\');
    drawForTime(dateIso, hourVal);
  }
}

let playInterval = null;
function togglePlay() {
  const playBtn = document.getElementById(\'play-btn\');
  if (playInterval) {
    clearInterval(playInterval);
    playInterval = null;
    playBtn.innerHTML = \'&#9654;\';
  } else {
    playBtn.innerHTML = \'&#10074;&#10074;\';
    const pills = document.querySelectorAll(\'.time-pill\');
    playInterval = setInterval(() => {
      activeTimeIndex = (activeTimeIndex + 1) % pills.length;
      pills[activeTimeIndex].click();
      pills[activeTimeIndex].scrollIntoView({ behavior: \'smooth\', block: \'nearest\', inline: \'center\' });
    }, 2500);
  }
}

let map;
try {
  map = L.map(\'map\',{
    scrollWheelZoom:true,
    worldCopyJump:false,
    maxBounds:[[-11.25,94],[6.45,141.25]],
    maxBoundsViscosity:.8,
    minZoom:5,
    zoomControl:false
  });
  
  const baseTile = L.tileLayer(\'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png\',{
    maxZoom:19,
    attribution:\'&copy; OpenStreetMap & CARTO\'
  });
  
  baseTile.on(\'tileerror\', function() {
    console.warn("Tile loading failed.");
    if (!document.getElementById(\'tile-warning\')) {
      document.body.insertAdjacentHTML(\'beforeend\', \'<div id="tile-warning" style="position:absolute; bottom:80px; left:24px; z-index:1001; background:rgba(239,68,68,0.85); color:#fff; padding:8px 16px; border-radius:12px; font-size:12px; font-family:sans-serif;">Peta dasar belum dapat dimuat. Data lokasi tetap tersedia.</div>\');
      setTimeout(() => {
        const warn = document.getElementById(\'tile-warning\');
        if (warn) warn.remove();
      }, 5000);
    }
  });
  baseTile.addTo(map);
  
  L.control.zoom({ position: \'topright\' }).addTo(map);
  
  var layer = L.layerGroup().addTo(map);
  
  // Sort and build timeline
  features.sort((a,b) => {
    const da = (a.properties && a.properties.date_iso) || \'\';
    const db = (b.properties && b.properties.date_iso) || \'\';
    if (da !== db) {
      return da.localeCompare(db);
    }
    const ha = (a.properties && a.properties.hour) || \'\';
    const hb = (b.properties && b.properties.hour) || \'\';
    return ha.localeCompare(hb);
  });
  
  // Filter to get a single unique location\'s time slots
  const firstLocSlug = (first.properties && first.properties.slug) || \'\';
  const locTimeline = features.filter(f => f.properties && f.properties.slug === firstLocSlug);
  const scrubber = document.getElementById(\'time-scrubber\');
  
  locTimeline.forEach((tFeature, idx) => {
    const p = tFeature.properties;
    const pill = document.createElement(\'button\');
    pill.className = \'time-pill\' + (idx === 0 ? \' active\' : \'\');
    pill.setAttribute(\'data-date\', p.date_iso);
    pill.setAttribute(\'data-hour\', p.hour);
    pill.innerHTML = `
      <span class="day-label">${p.relative}</span>
      <strong>${p.hour}</strong>
    `;
    pill.onclick = () => {
      document.querySelectorAll(\'.time-pill\').forEach(x => x.classList.remove(\'active\'));
      pill.classList.add(\'active\');
      activeTimeIndex = idx;
      drawForTime(p.date_iso, p.hour);
    };
    scrubber.appendChild(pill);
  });
  
  updateLegend();
  if (locTimeline.length) {
    drawForTime(locTimeline[0].properties.date_iso, locTimeline[0].properties.hour);
  } else {
    document.getElementById(\'hud-location-name\').textContent = \'Peta Prakiraan Cuaca\';
    document.getElementById(\'hud-time-label\').textContent = \'Data prakiraan tidak tersedia\';
    document.getElementById(\'val-temp\').textContent = \'—\';
    document.getElementById(\'val-rain\').textContent = \'—\';
    document.getElementById(\'val-wind\').textContent = \'—\';
    document.getElementById(\'val-humidity\').textContent = \'—\';
  }

  // Fit map bounds to show all markers automatically
  if (features.length) {
    const coordsList = features.map(f => [f.geometry.coordinates[1], f.geometry.coordinates[0]]);
    const bounds = L.latLngBounds(coordsList);
    map.fitBounds(bounds, { padding: [50, 50], maxZoom: 13 });
  } else {
    map.setView(center, 12);
  }

  // Keyboard controls
  document.addEventListener(\'keydown\', (e) => {
    const pills = document.querySelectorAll(\'.time-pill\');
    if (!pills.length) return;
    if (e.key === \'ArrowRight\') {
      activeTimeIndex = (activeTimeIndex + 1) % pills.length;
      pills[activeTimeIndex].click();
      pills[activeTimeIndex].scrollIntoView({ behavior: \'smooth\', block: \'nearest\', inline: \'center\' });
    } else if (e.key === \'ArrowLeft\') {
      activeTimeIndex = (activeTimeIndex - 1 + pills.length) % pills.length;
      pills[activeTimeIndex].click();
      pills[activeTimeIndex].scrollIntoView({ behavior: \'smooth\', block: \'nearest\', inline: \'center\' });
    }
  });

} catch(e) {
  console.error("Map initialization failed", e);
  document.body.insertAdjacentHTML(\'beforeend\',\'<div style="position:absolute;inset:0;display:grid;place-items:center;color:#6b8ab5;background:#030712;z-index:9999;font-family:sans-serif;padding:20px;text-align:center;">Peta dasar belum dapat dimuat. Data lokasi tetap tersedia.</div>\');
}

// Sync with postMessage from parent dashboard window
window.addEventListener(\'message\', (e) => {
  if (e.data && e.data.type === \'switchLayer\') {
    switchLayer(e.data.layer);
  }
});

// Hide the back button if embedded inside an iframe
if (window.self !== window.top) {
  const backBtn = document.getElementById(\'hud-back-btn\');
  if (backBtn) backBtn.style.display = \'none\';
}
\'''.replace("__DATA__", data)
    
    hud_html = f\'''  <section class="hud">
    <div style="display:flex; justify-content:space-between; align-items:center">
      <span class="hud-brand">{BRAND} Cuaca</span>
      <span class="hud-version">Peta Prakiraan</span>
    </div>
    <h1 id="hud-location-name">Memuat...</h1>
    <div class="hud-meta" id="hud-time-label">Memuat data prakiraan cuaca.</div>
    
    <div class="hud-divider"></div>
    
    <div class="hud-stats-grid">
      <div class="hud-stat-box">
        <span class="hud-stat-val" id="val-temp">—</span>
        <span class="hud-stat-lbl">Suhu</span>
      </div>
      <div class="hud-stat-box">
        <span class="hud-stat-val" id="val-rain">—</span>
        <span class="hud-stat-lbl">Peluang Hujan</span>
      </div>
      <div class="hud-stat-box">
        <span class="hud-stat-val" id="val-wind">—</span>
        <span class="hud-stat-lbl">Kecepatan Angin</span>
      </div>
      <div class="hud-stat-box">
        <span class="hud-stat-val" id="val-humidity">—</span>
        <span class="hud-stat-lbl">Kelembapan</span>
      </div>
    </div>

    <div class="hud-divider"></div>

    <div class="parameter-tabs">
      <button class="param-tab active" data-layer="risiko" onclick="switchLayer(\'risiko\')">Risiko</button>
      <button class="param-tab" data-layer="temp" onclick="switchLayer(\'temp\')">Suhu</button>
      <button class="param-tab" data-layer="rain" onclick="switchLayer(\'rain\')">Hujan</button>
      <button class="param-tab" data-layer="wind" onclick="switchLayer(\'wind\')">Angin</button>
      <button class="param-tab" data-layer="humidity" onclick="switchLayer(\'humidity\')">Lembap</button>
      <button class="param-tab" data-layer="cloud" onclick="switchLayer(\'cloud\')">Awan</button>
    </div>

    <div style="margin-top:20px; display:flex">
      <a class="btn" id="hud-back-btn" style="flex:1; margin-top:0" href="{esc(back_href)}">Kembali</a>
    </div>
  </section>\'''

    timeline_html = \'''  <div class="timeline-container">
    <button class="play-btn" id="play-btn" onclick="togglePlay()">&#9654;</button>
    <div class="time-scrubber" id="time-scrubber"></div>
  </div>\'''

    legend_html = \'<div class="legend" id="map-legend"></div>\'

    return f\'''<!doctype html>
<html lang="id">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{esc(title)}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>{css}</style>
</head>
<body>
  <div id="map"></div>
  <canvas id="particle-canvas"></canvas>
  {hud_html}
  {timeline_html}
  {legend_html}
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>{js}</script>
</body>
</html>\'''
'''

# Perform the replacement in memory
new_content = content[:start_idx] + replacement_code + content[end_idx:]

# Save modified content back to file
target_file.write_text(new_content, encoding='utf-8')
print("Successfully modified v65_map_page function in", target_file)
