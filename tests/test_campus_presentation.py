"""Presentation contracts that protect distinct campus assets and old products."""
import unittest
from pathlib import Path

import langit_v65_cinematic_rebuild as ui
from site_tools import campus_components
from site_tools.public_site import Links


class CampusPresentationContracts(unittest.TestCase):
    def test_responsive_picture_sources_are_in_the_public_link_contract(self):
        parser = Links()
        parser.feed('<picture><source srcset="assets/large.webp 1600w, assets/middle.webp 900w">'
                    '<img src="assets/small.webp" alt="Kampus"></picture>')
        self.assertEqual(set(parser.urls), {'assets/large.webp', 'assets/middle.webp', 'assets/small.webp'})

    def test_campus_photographs_are_distinct_and_keep_source_provenance(self):
        sources = []
        for slug in ('dago', 'jatinangor', 'arjawinangun'):
            meta = campus_components.THEME['locations'][slug]
            self.assertTrue(meta['source'].startswith('https://'))
            self.assertTrue(meta['author'])
            self.assertTrue(meta['license'])
            sources.append(meta['original_image_url'])
            for version in meta['files'].values():
                file = campus_components.ASSET_ROOT / version['file']
                self.assertEqual(file.stat().st_size, version['bytes'])
                self.assertGreater(version['width'], 0)
        self.assertEqual(len(set(sources)), 3)

    def test_campus_identity_does_not_relocate_the_forecast_point(self):
        day = {'date_label':'Sabtu, 31 Oktober 2026', 'risk_label':'Data terbatas'}
        html = ui.v65_hero({'location_slug':'dago','location_name':'Dago, Bandung'},
                           'Prakiraan Dago', 'Prakiraan', day)
        self.assertIn('Kampus Ganesha', html)
        self.assertIn('Titik prakiraan: Dago, Bandung', html)
        self.assertIn('bukan kondisi cuaca langsung', html)

    def test_partial_packages_do_not_link_to_unbuilt_locations(self):
        html = ui.v65_nav({'location_slug':'dago', 'location_name':'Dago, Bandung',
                          '_campus_available_slugs':['dago']}, 'today')
        self.assertIn('../dago/langit_app.html', html)
        self.assertNotIn('../jatinangor/langit_app.html', html)
        self.assertNotIn('../arjawinangun/langit_app.html', html)

    def test_presentation_does_not_include_simulated_background_weather(self):
        self.assertNotIn('requestAnimationFrame', ui.JS_V65)
        self.assertNotIn('atmo-canvas', campus_components.document(ui,
            {'location_slug':'dago','location_name':'Dago, Bandung'}, 'today','LANGIT',''))


if __name__ == '__main__':
    unittest.main()
