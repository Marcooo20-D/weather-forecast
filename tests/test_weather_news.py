import datetime as dt
import unittest
from site_tools import weather_news as news

NOW=dt.datetime(2026,11,4,8,tzinfo=dt.timezone.utc)

class WeatherNews(unittest.TestCase):
    def test_regional_weather_headline_is_classified_with_original_time(self):
        item=news.candidate('BMKG prakirakan hujan di Bandung','https://jabar.antaranews.com/berita/123/example','ANTARA Jabar',published_at=NOW,now=NOW)
        self.assertEqual(item['region'],'jabar');self.assertEqual(item['published_at'],NOW.isoformat())
    def test_old_and_foreign_headlines_are_not_latest_indonesia_weather(self):
        self.assertIsNone(news.candidate('Siklon menghantam Jamaika','https://www.antaranews.com/berita/123/x','ANTARA',published_at=NOW,now=NOW))
        self.assertIsNone(news.candidate('Hujan di Jakarta','https://www.antaranews.com/berita/123/x','ANTARA',published_at=NOW-dt.timedelta(days=9),now=NOW))
    def test_wind_energy_is_not_a_weather_headline(self):
        self.assertIsNone(news.candidate('Tiongkok memperluas jaringan energi angin','https://www.antaranews.com/berita/123/x','ANTARA',published_at=NOW,now=NOW))
    def test_only_publisher_https_urls_are_accepted(self):
        for url in ['javascript:alert(1)','https://antaranews.com.evil.invalid/x','http://www.bmkg.go.id/x']:
            self.assertIsNone(news.safe_url(url))
    def test_date_only_precision_is_retained(self):
        item=news.candidate('Prospek cuaca Indonesia pekan ini','https://www.bmkg.go.id/cuaca/x','BMKG',published_date=dt.date(2026,11,4),now=NOW)
        self.assertIsNone(item['published_at']);self.assertEqual(item['published_date'],'2026-11-04')
    def test_rss_omits_article_bodies(self):
        xml=b'<rss><channel><item><title>Hujan di Bandung menurut BMKG</title><link>https://jabar.antaranews.com/berita/123/x</link><pubDate>Wed, 04 Nov 2026 07:20:00 +0700</pubDate><description>Full article body</description></item></channel></rss>'
        item=news.parse_rss(xml,'ANTARA Jabar',NOW)[0]
        self.assertNotIn('description',item);self.assertNotIn('body',item);self.assertTrue(item['published_at'].endswith('+07:00'))
