/* Regional GFS: interpolate native values and meteorological wind components. */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.LangitWeatherField = api;
})(typeof window === "undefined" ? null : window, function () {
  "use strict";
  const finite = (v) => typeof v === "number" && Number.isFinite(v);
  const palettes = {
    wind: {
      title: "Angin 10 m",
      unit: "km/jam",
      stops: [
        [0, [219, 239, 244]],
        [10, [95, 174, 200]],
        [20, [45, 130, 172]],
        [40, [54, 88, 149]],
        [60, [131, 65, 140]],
      ],
    },
    temp: {
      title: "Suhu 2 m",
      unit: "°C",
      stops: [
        [10, [81, 111, 170]],
        [20, [81, 168, 180]],
        [25, [160, 199, 157]],
        [30, [235, 196, 112]],
        [35, [216, 124, 88]],
        [40, [173, 70, 93]],
      ],
    },
    humidity: {
      title: "Kelembapan 2 m",
      unit: "%",
      stops: [
        [30, [232, 215, 178]],
        [50, [180, 207, 192]],
        [70, [109, 177, 190]],
        [90, [65, 130, 169]],
        [100, [58, 100, 149]],
      ],
    },
    cloud: {
      title: "Tutupan awan",
      unit: "%",
      stops: [
        [0, [237, 243, 246]],
        [30, [193, 210, 220]],
        [60, [144, 170, 187]],
        [100, [88, 119, 149]],
      ],
    },
    rain: {
      title: "Hujan 1 jam",
      unit: "mm",
      stops: [
        [0, [222, 241, 244]],
        [0.5, [151, 206, 214]],
        [2, [79, 162, 189]],
        [5, [69, 120, 177]],
        [10, [113, 83, 158]],
        [25, [185, 92, 127]],
      ],
    },
  };
  function validate(d) {
    if (
      d?.schema_version !== 1 ||
      d.status !== "ready" ||
      d.model?.id !== "gfs013"
    )
      throw Error("Grid unavailable");
    const g = d.grid;
    for (const key of ["latitudes", "longitudes"]) {
      const a = g?.[key];
      if (
        !Array.isArray(a) ||
        a.length < 2 ||
        a.some((v, i) => !finite(v) || (i && a[i - 1] >= v))
      )
        throw Error("Invalid axis");
    }
    if (
      g.nx !== g.longitudes.length ||
      g.ny !== g.latitudes.length ||
      g.nx * g.ny > 1000
    )
      throw Error("Invalid grid");
    const t = d.times?.map(Date.parse);
    if (
      !t?.length ||
      t.some((v, i) => !finite(v) || (i && v - t[i - 1] !== 3600000))
    )
      throw Error("Invalid time axis");
    for (const key of [
      "u",
      "v",
      "temperature",
      "humidity",
      "cloud",
      "precipitation",
    ]) {
      const a = d.fields?.[key];
      if (
        !Array.isArray(a) ||
        a.length !== t.length ||
        a.some(
          (f) =>
            !Array.isArray(f) ||
            f.length !== g.nx * g.ny ||
            f.some((v) => v !== null && !finite(v)),
        )
      )
        throw Error("Invalid field");
    }
    return d;
  }
  function bracket(a, v) {
    if (!finite(v) || v < a[0] || v > a[a.length - 1]) return null;
    let lo = 0,
      hi = a.length - 1;
    while (hi - lo > 1) {
      const m = (hi + lo) >> 1;
      if (a[m] <= v) lo = m;
      else hi = m;
    }
    return [lo, (v - a[lo]) / (a[hi] - a[lo])];
  }
  function sample(d, t, lat, lon, keys = ["u", "v"]) {
    const x = bracket(d.grid.longitudes, lon),
      y = bracket(d.grid.latitudes, lat);
    if (!x || !y || t < 0 || t >= d.times.length) return null;
    const n = d.grid.nx,
      ix = x[0],
      iy = y[0],
      tx = x[1],
      ty = y[1],
      ids = [
        iy * n + ix,
        iy * n + ix + 1,
        (iy + 1) * n + ix,
        (iy + 1) * n + ix + 1,
      ],
      w = [(1 - tx) * (1 - ty), tx * (1 - ty), (1 - tx) * ty, tx * ty],
      out = {};
    for (const k of keys) {
      const f = d.fields[k]?.[t];
      if (!f) return null;
      let v = 0;
      for (let i = 0; i < 4; i++) {
        if (w[i] <= 1e-12) continue;
        if (!finite(f[ids[i]])) return null;
        v += w[i] * f[ids[i]];
      }
      out[k] = v;
    }
    return out;
  }
  function valueAt(d, t, lat, lon, layer) {
    const key = {
        temp: "temperature",
        humidity: "humidity",
        cloud: "cloud",
        rain: "precipitation",
      }[layer],
      p = sample(d, t, lat, lon, layer === "wind" ? ["u", "v"] : [key]);
    return !p ? null : layer === "wind" ? Math.hypot(p.u, p.v) * 3.6 : p[key];
  }
  function direction(u, v) {
    return ((Math.atan2(-u, -v) * 180) / Math.PI + 360) % 360;
  }
  function color(v, layer) {
    const s = palettes[layer].stops;
    if (v <= s[0][0]) return s[0][1];
    for (let i = 1; i < s.length; i++)
      if (v <= s[i][0]) {
        const a = s[i - 1],
          b = s[i],
          t = (v - a[0]) / (b[0] - a[0]);
        return a[1].map((x, k) => Math.round(x + (b[1][k] - x) * t));
      }
    return s[s.length - 1][1];
  }

  function create(map, L, options) {
    let data = null,
      index = -1,
      layer = options.layer || "wind",
      time = null,
      playing = false,
      raf = 0,
      last = 0,
      particles = [],
      frames = 0,
      visible = true,
      moving = false;
    const reduce = matchMedia("(prefers-reduced-motion: reduce)"),
      narrow = matchMedia("(max-width:768px)");
    let enabled = !reduce.matches;
    const status = options.status,
      toggle = options.toggle,
      inspector = options.inspector,
      pane = map.createPane("weatherFields");
    pane.style.zIndex = "430";
    pane.style.pointerEvents = "none";
    const raster = L.DomUtil.create("canvas", "weather-field-canvas", pane),
      trails = L.DomUtil.create("canvas", "weather-wind-canvas", pane);
    for (const c of [raster, trails]) c.setAttribute("aria-hidden", "true");
    const rc = raster.getContext("2d"),
      ctx = trails.getContext("2d"),
      scratch = document.createElement("canvas"),
      sc = scratch.getContext("2d");
    let width = 0,
      height = 0,
      dpr = 1,
      view = null,
      spawn = null,
      selectedPoint = null;
    const ready = () => Boolean(data && index >= 0);
    function announce(state, text) {
      status.dataset.state = state;
      status.textContent = text;
      toggle.disabled = !ready();
      toggle.setAttribute("aria-pressed", String(enabled && ready()));
      toggle.textContent =
        enabled && ready() ? "Angin bergerak" : "Animasi angin";
      options.onChange?.();
    }
    function configure() {
      const size = map.getSize();
      width = Math.max(1, size.x);
      height = Math.max(1, size.y);
      dpr = Math.min(devicePixelRatio || 1, 1.5);
      for (const c of [raster, trails]) {
        c.width = Math.round(width * dpr);
        c.height = Math.round(height * dpr);
        c.style.width = width + "px";
        c.style.height = height + "px";
        L.DomUtil.setPosition(c, map.containerPointToLayerPoint([0, 0]));
      }
      rc.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const b = map.getBounds(),
        merc = (lat) => Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
      view = {
        west: b.getWest(),
        east: b.getEast(),
        north: merc(b.getNorth()),
        south: merc(b.getSouth()),
      };
      if (data) {
        const g = data.grid,
          a = map.latLngToContainerPoint([
            g.latitudes[g.ny - 1],
            g.longitudes[0],
          ]),
          b = map.latLngToContainerPoint([
            g.latitudes[0],
            g.longitudes[g.nx - 1],
          ]);
        spawn = {
          left: Math.max(0, a.x),
          top: Math.max(0, a.y),
          right: Math.min(width, b.x),
          bottom: Math.min(height, b.y),
        };
      }
    }
    function coordinates(x, y) {
      return {
        lon: view.west + ((view.east - view.west) * x) / width,
        lat:
          ((2 *
            Math.atan(
              Math.exp(view.north + ((view.south - view.north) * y) / height),
            ) -
            Math.PI / 2) *
            180) /
          Math.PI,
      };
    }
    function draw() {
      configure();
      rc.clearRect(0, 0, width, height);
      ctx.clearRect(0, 0, width, height);
      raster.dataset.ready = String(ready());
      raster.dataset.layer = layer;
      if (!ready() || !palettes[layer]) return;
      const sw = Math.min(280, Math.ceil(width / 4)),
        sh = Math.min(210, Math.ceil(height / 4));
      scratch.width = sw;
      scratch.height = sh;
      const image = sc.createImageData(sw, sh);
      for (let y = 0; y < sh; y++)
        for (let x = 0; x < sw; x++) {
          const p = coordinates(
              ((x + 0.5) * width) / sw,
              ((y + 0.5) * height) / sh,
            ),
            v = valueAt(data, index, p.lat, p.lon, layer);
          if (v === null) continue;
          const o = (y * sw + x) * 4;
          image.data.set(color(v, layer), o);
          let a = 0.54;
          if (layer === "rain")
            a = v < 0.05 ? 0 : Math.min(0.68, 0.22 + v * 0.06);
          if (layer === "cloud") a = (0.6 * v) / 100;
          image.data[o + 3] = Math.round(a * 255);
        }
      sc.putImageData(image, 0, 0);
      rc.imageSmoothingEnabled = true;
      rc.save();
      rc.beginPath();
      rc.rect(
        spawn.left,
        spawn.top,
        Math.max(0, spawn.right - spawn.left),
        Math.max(0, spawn.bottom - spawn.top),
      );
      rc.clip();
      rc.drawImage(scratch, 0, 0, width, height);
      rc.restore();
    }
    function newParticle() {
      return !spawn || spawn.right <= spawn.left || spawn.bottom <= spawn.top
        ? null
        : {
            x: spawn.left + Math.random() * (spawn.right - spawn.left),
            y: spawn.top + Math.random() * (spawn.bottom - spawn.top),
            age: Math.random() * 4,
          };
    }
    function stop() {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      playing = false;
      last = 0;
    }
    function restart() {
      stop();
      particles = [];
      ctx.clearRect(0, 0, width, height);
      if (!enabled || !ready() || !visible || moving || document.hidden) return;
      const n = Math.min(
        narrow.matches ? 110 : 330,
        Math.max(45, Math.floor((width * height) / 3600)),
      );
      for (let i = 0; i < n; i++) {
        const p = newParticle();
        if (p) particles.push(p);
      }
      playing = true;
      raf = requestAnimationFrame(tick);
    }
    function tick(now) {
      if (!playing) return;
      const dt = Math.min(0.05, last ? (now - last) / 1000 : 0.016);
      last = now;
      ctx.globalCompositeOperation = "destination-in";
      ctx.fillStyle = "rgba(0,0,0,.94)";
      ctx.fillRect(0, 0, width, height);
      ctx.globalCompositeOperation = "source-over";
      ctx.beginPath();
      const ppm =
          (256 * Math.pow(2, map.getZoom())) /
          (40075016.686 * Math.cos((map.getCenter().lat * Math.PI) / 180)),
        factor = 22 / (Math.max(0.00001, ppm) * 3);
      for (let i = 0; i < particles.length; i++) {
        const p = particles[i];
        p.age += dt;
        const q = coordinates(p.x, p.y),
          v = sample(data, index, q.lat, q.lon);
        if (p.age > 5 || !v || Math.hypot(v.u, v.v) < 0.05) {
          particles[i] = newParticle() || p;
          continue;
        }
        const x = p.x + v.u * ppm * factor * dt,
          y = p.y - v.v * ppm * factor * dt;
        if (x < 0 || x > width || y < 0 || y > height) {
          particles[i] = newParticle() || p;
          continue;
        }
        ctx.moveTo(p.x, p.y);
        ctx.lineTo(x, y);
        p.x = x;
        p.y = y;
      }
      ctx.strokeStyle =
        layer === "wind" ? "rgba(255,255,255,.9)" : "rgba(25,65,87,.74)";
      ctx.lineWidth = layer === "wind" ? 1.35 : 1.1;
      ctx.save();
      const clip = new Path2D();
      clip.rect(
        spawn.left,
        spawn.top,
        Math.max(0, spawn.right - spawn.left),
        Math.max(0, spawn.bottom - spawn.top),
      );
      ctx.clip(clip);
      ctx.stroke();
      ctx.restore();
      trails.dataset.frame = String(++frames);
      raf = requestAnimationFrame(tick);
    }
    function inspect(lat, lon) {
      selectedPoint = [lat, lon];
      if (!ready()) return;
      const which = palettes[layer] ? layer : "wind",
        v = valueAt(data, index, lat, lon, which),
        wind = sample(data, index, lat, lon);
      inspector.textContent =
        v === null
          ? "Di luar cakupan grid GFS."
          : "GFS · " +
            lat.toFixed(3) +
            ", " +
            lon.toFixed(3) +
            " · " +
            palettes[which].title +
            " " +
            v.toFixed(1) +
            " " +
            palettes[which].unit +
            (which === "wind" && wind && Math.hypot(wind.u, wind.v) > 0.1
              ? " dari " + Math.round(direction(wind.u, wind.v)) + "°"
              : "");
    }
    function updateTime() {
      index = data
        ? data.times.findIndex((t) => Date.parse(t) === Date.parse(time))
        : -1;
      if (!ready())
        announce(
          data ? "no-time" : "loading",
          data ? "Grid tidak tersedia pada jam ini." : "Memuat grid GFS…",
        );
      else
        announce(
          "ready",
          "GFS 13 km · diambil " +
            new Intl.DateTimeFormat("id-ID", {
              timeZone: "Asia/Jakarta",
              day: "numeric",
              month: "short",
              hour: "2-digit",
              minute: "2-digit",
              hourCycle: "h23",
            }).format(new Date(data.fetched_at)) +
            " WIB",
        );
      draw();
      restart();
      if (selectedPoint) inspect(...selectedPoint);
    }
    function renderLegend(element) {
      if (!ready() || !palettes[layer]) return false;
      const p = palettes[layer];
      element.replaceChildren();
      const title = document.createElement("p");
      title.className = "legend-title";
      title.textContent = p.title + " (" + p.unit + ")";
      element.appendChild(title);
      const bar = document.createElement("div");
      bar.className = "field-color-bar";
      bar.style.background =
        "linear-gradient(90deg," +
        p.stops.map((s) => "rgb(" + s[1].join(",") + ")").join(",") +
        ")";
      element.appendChild(bar);
      const ticks = document.createElement("div");
      ticks.className = "field-color-ticks";
      p.stops.forEach((s, i) => {
        const e = document.createElement("span");
        e.textContent = (i === p.stops.length - 1 ? "≥" : "") + s[0];
        ticks.appendChild(e);
      });
      element.appendChild(ticks);
      const note = document.createElement("p");
      note.className = "field-legend-note";
      note.textContent =
        layer === "rain"
          ? "Akumulasi satu jam sebelum waktu terpilih."
          : "Interpolasi grid model; bukan pengamatan langsung.";
      element.appendChild(note);
      return true;
    }
    toggle.addEventListener("click", () => {
      enabled = !enabled;
      toggle.setAttribute("aria-pressed", String(enabled));
      toggle.textContent = enabled ? "Angin bergerak" : "Animasi angin";
      restart();
    });
    map.on("movestart zoomstart", () => {
      moving = true;
      stop();
    });
    map.on("moveend zoomend resize", () => {
      moving = false;
      draw();
      restart();
    });
    map.on("click", (e) => inspect(e.latlng.lat, e.latlng.lng));
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stop();
      else restart();
    });
    reduce.addEventListener?.("change", (e) => {
      if (e.matches) {
        enabled = false;
        toggle.setAttribute("aria-pressed", "false");
        toggle.textContent = "Animasi angin";
        restart();
      }
    });
    if (typeof IntersectionObserver !== "undefined")
      new IntersectionObserver((entries) => {
        visible = entries[0].isIntersecting;
        if (visible) restart();
        else stop();
      }).observe(map.getContainer());
    const controller = new AbortController(),
      timeout = setTimeout(() => controller.abort(), 12000);
    fetch(options.url, { signal: controller.signal })
      .then((r) => {
        if (!r.ok) throw Error("Grid request failed");
        return r.json();
      })
      .then(validate)
      .then((d) => {
        const age = Date.now() - Date.parse(d.fetched_at);
        if (!finite(age) || age < -3600000 || age > 18 * 3600000)
          throw Error("Grid expired");
        data = d;
        updateTime();
      })
      .catch(() => {
        data = null;
        index = -1;
        stop();
        rc.clearRect(0, 0, width, height);
        ctx.clearRect(0, 0, width, height);
        announce(
          "unavailable",
          "Grid GFS belum tersedia. Prakiraan lokasi tetap tersedia.",
        );
      })
      .finally(() => clearTimeout(timeout));
    announce("loading", "Memuat grid GFS…");
    return {
      setTime(t) {
        time = t;
        if (data) updateTime();
      },
      setLayer(l) {
        layer = l;
        draw();
        restart();
        if (selectedPoint) inspect(...selectedPoint);
        options.onChange?.();
      },
      inspect,
      isReady: ready,
      renderLegend,
      stop,
      getState() {
        return {
          ready: ready(),
          time: index >= 0 ? data.times[index] : null,
          layer,
          frames,
          playing,
        };
      },
    };
  }
  return {
    validate,
    bracket,
    sample,
    valueAt,
    direction,
    color,
    palettes,
    create,
  };
});
