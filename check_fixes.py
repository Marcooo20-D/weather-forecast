#!/usr/bin/env python3
"""Verify all map fixes are present in the generated HTML files."""
import json, os

def check_file(path, checks):
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    print(f"\n=== {path} ({len(content)} bytes) ===")
    for name, query in checks.items():
        found = query.lower() in content.lower()
        status = "OK" if found else "MISSING"
        print(f"  {name}: {status}")
    return content

# Check map HTML
check_file('outputs/dago/anemos_map.html', {
    'fitBounds': 'fitBounds',
    'postMessage_listener': 'switchLayer',
    'Angin_layer': 'angin',
    'tileerror': 'tileerror',
    'sparkline_sort': '.sort(',
    'leaflet_css': 'leaflet.css',
    'leaflet_js': 'leaflet',
})

# Check app HTML
check_file('outputs/dago/anemos_app.html', {
    'map_frame': 'map-frame',
    'command_center': 'command-center',
    'postMessage_send': 'postMessage',
    'layer_btn': 'layer-btn',
    'peta_prakiraan': 'Peta Prakiraan',
    'lapisan_cuaca': 'Lapisan Cuaca',
    'iframe_map': 'anemos_map.html',
})

# Check portal map
check_file('outputs/langit_portal_map.html', {
    'leaflet': 'leaflet',
    'geojson': 'geojson',
    'fitBounds': 'fitBounds',
})

# Check geojson files
for loc in ['dago', 'jatinangor', 'arjawinangun']:
    gj_path = f'outputs/{loc}/langit_location.geojson'
    if os.path.exists(gj_path):
        with open(gj_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and 'features' in data:
            print(f"\n  {gj_path}: OK ({len(data['features'])} features)")
        else:
            print(f"\n  {gj_path}: ERROR - invalid structure")
    else:
        print(f"\n  {gj_path}: FILE NOT FOUND")

# Check main portal index
check_file('outputs/index.html', {
    'portal_map_link': 'langit_portal_map',
    'location_links': 'dago',
})

print("\n=== ALL CHECKS COMPLETE ===")
