"""Render a separate satellite-reference evaluation without relabelling stations."""
from __future__ import annotations
import html
import json
import math
from pathlib import Path


def number(value, decimals=3):
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return "—"
    return f"{value:.{decimals}f}"


def render(directory: Path) -> str:
    path = Path(directory) / "satellite_verification.json"
    if not path.exists():
        return ""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "LANGIT.satellite-verification.v1":
        raise ValueError("Unrecognized satellite verification contract")
    overall = payload.get("all_leads") or {}
    cases = int(overall.get("cases", 0))
    if not cases:
        details = '<p>Belum ada pasangan prakiraan dan referensi satelit yang cocok.</p>'
    else:
        rows = []
        for label, data in (payload.get("by_lead") or {}).items():
            rows.append('<tr><td>' + html.escape(label) + '</td><td>' + str(int(data.get("cases", 0))) + '</td><td>' + number(data.get("brier_score")) + '</td><td>' + number(data.get("mae_mm")) + '</td><td>' + number(data.get("crps_mm")) + '</td></tr>')
        start, end = overall.get("period_start", ""), overall.get("period_end", "")
        details = f'''<p>{cases} kasus · {int(overall.get("unique_valid_hours", 0))} jam berlaku unik · {html.escape(start)} hingga {html.escape(end)} (UTC)</p>
        <div class="table-wrap" style="overflow-x:auto"><table class="source-table"><thead><tr><th>Lead time</th><th>Kasus</th><th>Brier Score</th><th>MAE (mm)</th><th>CRPS (mm)</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>'''
    return f'''<section class="section-compact"><div class="container"><div class="glass glass-static reveal">
      <div class="section-header"><div class="section-overline">Evaluasi terpisah</div><h2>Hujan dibandingkan dengan referensi satelit</h2></div>
      <p>NOAA CPC CMORPH ICDR ADJ · grid 0,25° · akumulasi satu jam. Kejadian hujan: lebih dari 0,1 mm per jam.</p>
      {details}
      <p>Referensi ini merupakan estimasi hujan untuk satu sel grid, bukan penakar hujan di kampus. Kasus pada beberapa lead time dapat memakai jam pembanding yang sama. Hasil ini tidak menetapkan akurasi stasiun setempat atau mengubah kalibrasi prakiraan.</p>
      <p><a href="satellite_verification.json">Metode dan ringkasan</a> · <a href="satellite_verification_pairs.csv">Unduh pasangan data</a></p>
    </div></div></section>'''
