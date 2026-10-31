"""Campus presentation only. Forecast values come from the existing renderer."""
from __future__ import annotations

import json
from pathlib import Path

ASSET_ROOT = Path(__file__).parent / "campus_assets"
THEME = json.loads((ASSET_ROOT / "credits.json").read_text(encoding="utf-8"))


def icon(name: str) -> str:
    shapes = {
        "arrow": '<path d="M5 12h14M13 6l6 6-6 6"/>',
        "chevron": '<path d="m6 9 6 6 6-6"/>',
        "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7v.1"/>',
        "copy": '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h3"/>',
        "roof": '<path d="M3 27 10 18l5 4 5-15 5 15 5-4 7 9M6 27v7h28v-7M11 27h18M17 27v7m6-7v7"/>',
    }
    view = "0 0 40 40" if name == "roof" else "0 0 24 24"
    return f'<svg viewBox="{view}" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{shapes[name]}</svg>'


def campus(api: dict, root: bool = False) -> dict:
    return THEME["locations"].get(api.get("location_slug"), THEME["locations"]["dago"])


def picture(u, meta: dict, prefix: str, *, hero: bool = False) -> str:
    small, large = meta["files"]["small"], meta["files"]["large"]
    loading = 'loading="eager" fetchpriority="high"' if hero else 'loading="lazy"'
    cls = ' class="hero-image"' if hero else ""
    return (
        f'<picture><source media="(min-width: 768px)" srcset="{prefix}{u.esc(large["file"])}">'
        f'<img{cls} src="{prefix}{u.esc(small["file"])}" width="{small["width"]}" height="{small["height"]}" '
        f'alt="{u.esc(meta["caption"])}" {loading} decoding="async" style="object-position:{u.esc(meta["position"])}"></picture>'
    )


def nav(u, api: dict, active: str, root: bool = False) -> str:
    if root:
        items = [("Lokasi", "index.html", "locations"), ("Peta", "langit_portal_map.html", "map")]
        home, prefix = "index.html", ""
    else:
        items = [("Hari ini", "langit_app.html", "today"), ("3 hari ke depan", "langit_3day.html", "3day"),
                 ("Panduan Aktivitas", "langit_activity.html", "activity"), ("Peta", "langit_map_room.html", "map")]
        home, prefix = "../index.html", "../"
    links = []
    for label, url, key in items:
        current = ' aria-current="page"' if key == active else ""
        links.append(f'<a class="nav-link {"active" if key == active else ""}"{current} href="{url}">{label}</a>')
    links = "".join(links)
    available = api.get("_campus_available_slugs", [api.get("location_slug")])
    options = "".join(f'<a href="{prefix}{slug}/langit_app.html"><span>{m["campus"]}</span>'
                      f'<small>{m["region"]}</small></a>' for slug, m in THEME["locations"].items()
                      if slug in available)
    return f'''<header class="nav-bar">
      <a class="nav-brand" href="{home}" aria-label="LANGIT, portal cuaca kampus ITB">
        <span class="nav-logo">{icon('roof')}</span>
        <span class="nav-wordmark"><span class="nav-title">LANGIT</span><span class="nav-sub">Cuaca kampus ITB</span></span>
      </a>
      <nav class="nav-links" id="site-navigation" aria-label="Navigasi utama">{links}</nav>
      <details class="campus-switch"><summary>Kampus {icon('chevron')}</summary><div class="campus-switch-menu">{options}</div></details>
      <button class="nav-toggle" type="button" aria-label="Menu" aria-controls="site-navigation" aria-expanded="false"><span></span><span></span><span></span></button>
    </header>'''


def photo_credits(u, prefix: str) -> str:
    items = []
    for m in THEME["locations"].values():
        if m["license_url"]:
            rights = f'A2613 · <a href="{u.esc(m["license_url"])}">CC BY-SA 4.0</a>. Ukuran dan format dioptimalkan; tampilan dipotong responsif.'
        else:
            rights = 'Dokumentasi ITB; hak foto tetap pada pemiliknya. Lisensi terbuka tidak dinyatakan pada sumber.'
        items.append(f'<li><a href="{u.esc(m["source"])}">{u.esc(m["caption"])}</a> — {rights}</li>')
    return f'<details class="photo-credits"><summary>Kredit foto kampus</summary><ul>{"".join(items)}</ul><a href="{prefix}campus-credits.json">Rincian sumber foto</a></details>'


def document(u, api: dict, active: str, title: str, body: str, root: bool = False) -> str:
    prefix = "assets/" if root else "../assets/"
    issue = api.get("today", {}).get("issued_at") or api.get("generated_at_iso")
    meta = campus(api, root)
    config = {"location_name": api.get("location_name", "Portal"), "location_slug": api.get("location_slug", "portal"),
              "latitude": api.get("latitude"), "longitude": api.get("longitude"), "is_portal": root,
              "forecast_issued_at": issue, "stale_after_hours": 18}
    footer_links = ('<a class="footer-link" href="index.html#kampus">Pilih kampus</a>'
                    '<a class="footer-link" href="langit_portal_map.html">Peta lokasi</a>') if root else (
                    '<a class="footer-link" href="keandalan_data.html">Keandalan data</a>'
                    '<a class="footer-link" href="akurasi_data.html">Akurasi</a>'
                    '<a class="footer-link" href="langit_planner.html">Planner</a>'
                    '<a class="footer-link" href="langit_api_v1.json">JSON</a>'
                    '<a class="footer-link" href="langit_location.geojson">GeoJSON</a>')
    return f'''<!doctype html><html lang="id"><head>
      <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
      <title>{u.esc(title)}</title><meta name="theme-color" content="#ffffff">
      <meta name="description" content="{u.esc(title)}. Prakiraan, peluang hujan, angin, dan rencana aktivitas untuk komunitas kampus ITB.">
      <link rel="stylesheet" href="{prefix}{u.asset_name('css')}">
    </head><body data-campus="{u.esc(meta['slug'])}">
      <a class="skip-link" href="#main-content">Langsung ke prakiraan</a>
      {nav(u, api, active, root)}
      <main class="page" id="main-content" tabindex="-1">
        <div class="freshness-bar"><div class="container"><p class="freshness-note" role="status" data-issued-at="{u.esc(issue)}">Waktu terbit prakiraan: {u.esc(u.fmt_update(issue))}.</p><span class="time-zone">Waktu Indonesia Barat · UTC+7</span></div></div>
        {body}
      </main>
      <footer class="footer"><div class="container">
        <div class="footer-main"><div><div class="footer-brand">LANGIT</div><p class="footer-tagline">Untuk hari-hari di kampus.</p><p class="footer-version">{u.VERSION} · {u.esc(api.get('generated_at', u.fmt_update()))}<br>Proyek independen mahasiswa Meteorologi ITB. Bukan layanan resmi ITB atau BMKG.</p></div><div class="footer-links">{footer_links}</div></div>
        {photo_credits(u, prefix)}
      </div></footer>
      <script>window.LANGIT_CONFIG={u.inline_json(config)};</script>
      <script defer src="{prefix}{u.asset_name('data.js')}"></script><script defer src="{prefix}{u.asset_name('js')}"></script>
    </body></html>'''


def hero(u, api: dict, heading: str, subtitle: str, day: dict, show_metrics: bool = True) -> str:
    is_root = bool(api.get("campus_apis"))
    meta, prefix = campus(api, is_root), ("assets/" if is_root else "../assets/")
    date_prefix = str(day.get("date_label", "")) + ". "
    if not is_root and subtitle.startswith(date_prefix):
        subtitle = subtitle[len(date_prefix):]
    eyebrow = "ITB · Ganesha / Jatinangor / Cirebon" if is_root else f'ITB · Kampus {meta["campus"]}'
    metrics = ""
    if show_metrics:
        metrics = f'''<div class="hero-metrics"><div class="hero-metric"><div class="hero-metric-value">{u.deg(day.get('avg_temp_c'))}</div><div class="hero-metric-label">Suhu rata-rata data tersedia</div></div>
        <div class="hero-metric"><div class="hero-metric-value">{u.pct(day.get('peak_rain_probability'))}</div><div class="hero-metric-label">Peluang hujan tertinggi</div></div>
        <div class="hero-metric"><div class="hero-metric-value">{u.esc(day.get('risk_label'))}</div><div class="hero-metric-label">Potensi gangguan aktivitas</div></div></div>'''
    glance = ""
    if is_root:
        rows = []
        for a in api["campus_apis"]:
            m = campus(a)
            rows.append(f'<a class="glance-row" href="{u.esc(a["location_slug"])}/langit_app.html"><span><span class="glance-name">{u.esc(m["campus"])}</span><span class="glance-rain">Hujan {u.pct(a["today"].get("peak_rain_probability"))} · {u.esc(a["location_name"])}</span></span><span class="glance-temp">{u.deg(a["today"].get("avg_temp_c"))}</span></a>')
        glance = f'<aside class="hero-campus-glance" aria-label="Ringkasan cuaca tiga lokasi"><div class="glance-label">Sekilas hari ini</div><p class="glance-note">Suhu rata-rata · peluang hujan tertinggi</p>{"".join(rows)}</aside>'
    actions = f'<div class="hero-actions"><a class="btn btn-primary" href="#kampus">Pilih kampus {icon("arrow")}</a><a class="btn btn-secondary" href="langit_portal_map.html">Jelajah peta</a></div>' if is_root else ""
    point = (f'<p class="hero-meta">{u.esc(day.get("date_label"))} · Tiga lokasi prakiraan</p>' if is_root
             else f'<p class="hero-meta">Titik prakiraan: {u.esc(api.get("location_name"))} · {u.esc(day.get("date_label"))}</p>')
    kind = " hero-portal" if is_root else (" hero-compact" if not show_metrics else "")
    return f'''<section class="hero{kind}">
      {picture(u, meta, prefix, hero=True)}
      <div class="container hero-layout"><div><div class="hero-label">{u.esc(eyebrow)}</div><h1 class="hero-title">{u.esc(heading)}</h1><p class="hero-subtitle">{u.esc(subtitle)}</p>{point}{metrics}{actions}</div>{glance}</div>
      <p class="hero-photo-caption"><a href="{u.esc(meta['source'])}">{u.esc(meta['caption'])}</a> · Foto dokumentasi, bukan kondisi cuaca langsung</p>
    </section>'''


def portal(u, apis: list, root: Path) -> str:
    today = apis[0]["today"] if apis else u.summarize_day("Hari ini", u.local_now().date(), [])
    order = {"dago": 0, "jatinangor": 1, "arjawinangun": 2}
    apis = sorted(apis, key=lambda a: order.get(a["location_slug"], 99))
    dummy = {"location_name": "Portal", "location_slug": "portal", "generated_at": u.fmt_update(u.latest_issue(apis)), "today": today, "campus_apis": apis,
             "_campus_available_slugs": [a["location_slug"] for a in apis]}
    body = hero(u, dummy, "Langit kampus.\nRencana hari ini.", "Prakiraan Dago, Jatinangor, dan Arjawinangun untuk kuliah, perjalanan, dan aktivitas di luar ruang.", today, False)
    cards = []
    for i, a in enumerate(apis):
        m, d, slug = campus(a), a["today"], a["location_slug"]
        cls = d.get("risk_class", "limited")
        cards.append(f'''<article class="campus-card"><a class="location-card" href="{u.esc(slug)}/langit_app.html">
          <div class="campus-card-image">{picture(u, m, 'assets/')}<span class="campus-card-number">0{i+1} / {u.esc(m['region'])}</span></div>
          <div class="campus-card-body"><div class="campus-card-heading"><h3 class="location-name">{u.esc(m['campus'])}</h3><span class="campus-card-arrow">{icon('arrow')}</span></div>
          <p class="campus-point">Titik prakiraan: {u.esc(a['location_name'])}</p><span class="status-badge status-{u.esc(cls)}">{u.esc(d.get('risk_label'))}</span>
          <p class="location-desc">{u.esc(u.decision_sentence(a['location_name'], d, short=True))}</p>
          <div class="location-stats"><div><div class="location-stat-value">{u.deg(d.get('avg_temp_c'))}</div><div class="location-stat-label">Suhu rata-rata</div></div><div><div class="location-stat-value">{u.pct(d.get('peak_rain_probability'))}</div><div class="location-stat-label">Peluang hujan tertinggi</div></div><div><div class="location-stat-value">{u.esc(d.get('peak_rain_hour'))}</div><div class="location-stat-label">Puncak · WIB</div></div></div></div>
        </a><div class="campus-card-links"><a href="{u.esc(slug)}/langit_3day.html">3 hari ke depan {icon('arrow')}</a><a href="{u.esc(slug)}/langit_activity.html">Aktivitas {icon('arrow')}</a></div></article>''')
    body += f'''<section class="section" id="kampus"><div class="container"><div class="section-heading-row"><div class="section-header"><div class="section-overline">Pilih kampus</div><h2 class="section-title">Tiga kampus. Satu pandangan.</h2><p class="section-desc">Lihat prakiraan di lokasi pilihanmu, lalu susun rencana hari ini.</p></div><span class="section-count">{u.esc(today['date_label'])}</span></div><div class="location-grid">{"".join(cards)}</div></div></section>'''
    body += u.v65_notice()
    body += f'''<section class="section"><div class="container"><div class="section-heading-row"><div class="section-header"><div class="section-overline">Jelajah lokasi</div><h2 class="section-title">Baca cuaca di peta.</h2><p class="section-desc">Titik prakiraan tiga lokasi. Pilih jam, lokasi, dan parameter yang ingin dilihat.</p></div></div><div class="map-wrapper"><iframe class="map-frame" title="Peta titik prakiraan cuaca LANGIT" src="langit_portal_map.html" loading="lazy"></iframe><div class="map-actions"><a class="btn btn-primary" href="langit_portal_map.html">Buka peta penuh {icon('arrow')}</a><a class="btn" href="langit_all_locations.geojson">GeoJSON</a></div></div></div></section>'''
    body += '''<section class="section-compact"><div class="container"><div class="glass"><div class="section-header"><div class="section-overline">Transparansi</div><h2 class="section-title">Data yang bisa ditelusuri.</h2><p class="section-desc">Unduh prakiraan, periksa sumbernya, dan gunakan data sesuai waktu berlakunya.</p></div><div class="data-downloads"><a class="btn" href="forecast_all_locations.csv">Forecast CSV</a><a class="btn" href="source_status_all_locations.csv">Sumber CSV</a><a class="btn" href="langit_portal_manifest.json">Manifest</a></div></div></div></section>'''
    return document(u, dummy, "locations", "LANGIT — Cuaca Kampus ITB", body, True)


def share(u, api: dict, day: dict) -> str:
    message = f"LANGIT — {api['location_name']}\n{day['date_label']}\n{u.decision_sentence(api['location_name'], day, short=True)}\nPeluang hujan tertinggi: {u.pct(day.get('peak_rain_probability'))} sekitar pukul {day.get('peak_rain_hour','—')} WIB."
    return f'''<section class="section-compact"><div class="container"><div class="share-grid"><div class="glass"><div class="section-overline">Bagikan</div><h3 style="font-size:21px;margin:8px 0 16px">Ringkas, lalu kirim.</h3><textarea class="share-text" aria-label="Ringkasan prakiraan untuk dibagikan" readonly>{u.esc(message)}</textarea><div class="share-actions"><button type="button" class="btn" data-copy-forecast>{icon('copy')} Salin ringkasan</button><span class="copy-status" role="status" aria-live="polite"></span></div></div><div class="glass"><div class="section-overline">Sebelum berangkat</div><h3 style="font-size:21px;margin:8px 0 16px">Tetap lihat kondisi sekitar.</h3><p class="section-desc">Prakiraan dapat berubah. Perhatikan waktu terbit dan waktu berlakunya. Untuk cuaca ekstrem, pantau peringatan dini BMKG dan kondisi setempat.</p><p class="section-desc">Peluang hujan tidak menyatakan intensitas. Indeks aktivitas adalah panduan heuristik, bukan peringatan cuaca berbahaya.</p></div></div></div></section>'''
