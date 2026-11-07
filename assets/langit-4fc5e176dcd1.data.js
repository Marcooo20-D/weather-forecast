/* Numeric contracts shared by charts and maps. No network or DOM dependency. */
(function (root) {
  'use strict';
  const variables = {
    rain: {field: 'rain_probability', unit: '%', lower: 0, upper: 100},
    temp: {field: 'temp_c', unit: '°C', lower: -80, upper: 60},
    humidity: {field: 'humidity_pct', unit: '%', lower: 0, upper: 100},
    wind: {field: 'wind_kmh', unit: ' km/jam', lower: 0, upper: 500}
  };
  function finite(value) {
    if (value == null || typeof value === 'boolean' ||
        (typeof value === 'string' && !value.trim())) return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }
  function minutes(hour) {
    const match = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(String(hour || ''));
    return match ? Number(match[1]) * 60 + Number(match[2]) : null;
  }
  function format(value, unit) {
    const number = finite(value);
    if (number == null) return '—';
    if (unit === '%' && number > 0 && number < 1) return '<1%';
    return (unit === '°C' || unit === ' km/jam' ? number.toFixed(1) : String(Math.round(number))) + unit;
  }
  function chart(hours, variable, width, height) {
    const meta = variables[variable] || variables.rain;
    const padX = width < 400 ? 30 : 50, padTop = 30, padBottom = 36;
    const rows = (hours || []).map(function (hour) {
      let value = finite(hour[meta.field]);
      if (value != null && (value < meta.lower || value > meta.upper)) value = null;
      return {hr: hour, minute: minutes(hour.hour), val: value};
    }).filter(function (point) { return point.minute != null; })
      .sort(function (a, b) { return a.minute - b.minute; });
    const values = rows.filter(function (p) { return p.val != null; }).map(function (p) { return p.val; });
    let low = 0, high = 100;
    if (variable === 'temp') {
      low = values.length ? Math.floor(Math.min.apply(null, values)) - 2 : 15;
      high = values.length ? Math.ceil(Math.max.apply(null, values)) + 2 : 30;
    } else if (variable === 'wind') {
      high = Math.max(5, Math.ceil(Math.max.apply(null, values.concat([0])) / 5) * 5);
    }
    const start = rows.length ? rows[0].minute : 0;
    const end = rows.length ? rows[rows.length - 1].minute : 0;
    const points = rows.map(function (p) {
      return Object.assign({}, p, {
        x: rows.length === 1 ? width / 2 : padX + (p.minute - start) / Math.max(1, end - start) * (width - padX * 2),
        y: p.val == null ? null : padTop + (high - p.val) / (high - low) * (height - padTop - padBottom)
      });
    });
    const segments = [];
    let segment = [];
    points.forEach(function (point, index) {
      const previous = points[index - 1];
      if (point.val == null || (previous && point.minute - previous.minute > 60)) {
        if (segment.length) segments.push(segment);
        segment = [];
      }
      if (point.val != null) segment.push(point);
    });
    if (segment.length) segments.push(segment);
    return {points: points, segments: segments, low: low, high: high,
      unit: meta.unit, padX: padX, padTop: padTop, bottom: height - padBottom};
  }
  function timeline(features) {
    const slots = new Map();
    (features || []).forEach(function (feature) {
      const p = feature.properties || {};
      if (/^\d{4}-\d{2}-\d{2}$/.test(p.date_iso || '') && minutes(p.hour) != null) {
        slots.set(p.date_iso + 'T' + p.hour, {date_iso: p.date_iso, hour: p.hour});
      }
    });
    return Array.from(slots.values()).sort(function (a, b) {
      return (a.date_iso + a.hour).localeCompare(b.date_iso + b.hour);
    });
  }
  function windRotation(direction, speed) {
    const bearing = finite(direction), velocity = finite(speed);
    return bearing == null || bearing < 0 || bearing > 360 || velocity == null || velocity <= 0
      ? null : (bearing + 90) % 360;
  }
  const api = {finite: finite, minutes: minutes, format: format, chart: chart,
    timeline: timeline, windRotation: windRotation};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.LangitPublicData = api;
})(typeof window === 'undefined' ? globalThis : window);
