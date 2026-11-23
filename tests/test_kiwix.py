import sys
import http.client
import io
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sources import kiwix

ENTRY = '''<entry><id>urn:uuid:{date}</id><title>Wikipedia Medicine</title>
<updated>{date}-01T00:00:00Z</updated><summary>Medical reference</summary>
<name>wikipedia_en_medicine</name><flavour>{flavour}</flavour>
<link type="text/html" href="/content/{filename}" />
<link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim"
 href="https://download.kiwix.org/zim/wikipedia/{filename}.zim.meta4" length="1234" />
</entry>'''


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        entries = [ENTRY.format(date=date, flavour=flavour, filename=f'wikipedia_en_medicine_{flavour}_{date}')
                   for date, flavour in [('2025-01', 'maxi'), ('2026-02', 'maxi'), ('2026-03', 'mini')]]
        self.xml = ('<feed xmlns="http://www.w3.org/2005/Atom">' + ''.join(entries) + '</feed>').encode()
        self.map_index = b'''<html><a href="maps_en_canada_2025-06.zim">old</a> 2025-06-22 08:00 9.0G
        <a href="maps_en_canada_2026-08a.zim">new</a> 2026-08-21 08:00 10G
        <a href="maps_en_all_2026-06.zim">world</a> 2026-06-22 08:00 72G
        <a href="../">parent</a><a href="maps_en_all_2026-06.zim.meta4">metadata</a></html>'''

    def fetch(self, url):
        self.requests.append(url)
        return self.map_index if url == kiwix.MAP_INDEX else self.xml

    def test_live_search_metadata_and_safe_download(self):
        results = kiwix.search('medicine', fetch=self.fetch)
        item = results[0]
        self.assertEqual(item['id'], 'wikipedia_en_medicine|maxi')
        self.assertEqual(item['size'], 1234)
        self.assertEqual(item['description'], 'Medical reference')
        self.assertEqual(item['source_url'], 'https://browse.library.kiwix.org/content/wikipedia_en_medicine_maxi_2025-01')
        self.assertEqual(item['filename'], 'wikipedia_en_medicine_maxi_2025-01.zim')
        self.assertTrue(item['download_url'].endswith('.zim'))
        self.assertEqual(parse_qs(urlsplit(self.requests[0]).query)['q'], ['medicine'])
        self.assertNotIn(kiwix.MAP_INDEX, self.requests)

    def test_resolution_preserves_flavour_and_selects_latest(self):
        item = kiwix.resolve_prefix('wikipedia_en_medicine_maxi', fetch=self.fetch)
        self.assertEqual(item['filename'], 'wikipedia_en_medicine_maxi_2026-02.zim')
        self.assertEqual(parse_qs(urlsplit(self.requests[0]).query)['name'], ['wikipedia_en_medicine'])

    def test_map_index_fallback(self):
        results = kiwix.search('canada map', fetch=self.fetch)
        maps = [x for x in results if x['id'].startswith('maps|')]
        self.assertEqual([x['filename'] for x in maps], ['maps_en_canada_2026-08a.zim'])
        self.assertEqual(maps[0]['size'], 10 * 1024 ** 3)
        self.assertTrue(maps[0]['size_approx'])
        resolved = kiwix.resolve(maps[0]['id'], fetch=self.fetch)
        self.assertEqual(resolved['filename'], maps[0]['filename'])
        self.assertEqual(resolved['size'], 10 * 1024 ** 3)

    def test_map_without_a_listed_size_still_works(self):
        page = b'<html><a href="maps_en_niue_2026-06.zim">Niue</a></html>'
        result = kiwix._maps(lambda _: page)
        self.assertEqual(result[0]['filename'], 'maps_en_niue_2026-06.zim')
        self.assertIsNone(result[0]['size'])

    def test_browse_all_includes_maps(self):
        results, _ = kiwix.search_page('', fetch=self.fetch)
        self.assertTrue(any(x['id'] == 'maps|maps_en_canada' for x in results))

    def test_more_results_use_catalog_pagination(self):
        entry = ENTRY.format(date='2026-02', flavour='maxi', filename='wikipedia_en_medicine_maxi_2026-02')
        def fetch(url):
            self.requests.append(url)
            return ('<feed xmlns="http://www.w3.org/2005/Atom"><totalResults>101</totalResults>' + entry + '</feed>').encode()
        first, more = kiwix.search_page('medicine', count=1, fetch=fetch)
        self.assertTrue(more)
        self.assertEqual(len(first), 1)
        _, more = kiwix.search_page('medicine', start=100, count=1, fetch=fetch)
        self.assertFalse(more)
        self.assertEqual(parse_qs(urlsplit(self.requests[-1]).query)['start'], ['100'])

    def test_unlimited_browse_can_return_more_than_100_entries(self):
        entries = []
        for i in range(130):
            entry = ENTRY.format(date='2026-02', flavour='maxi', filename='example_{}_2026-02'.format(i))
            entries.append(entry.replace('<name>wikipedia_en_medicine</name>', '<name>example_{}</name>'.format(i)))
        def fetch(url):
            self.requests.append(url)
            return ('<feed xmlns="http://www.w3.org/2005/Atom"><totalResults>130</totalResults>' + ''.join(entries) + '</feed>').encode()
        results, more = kiwix.search_page('medicine', count=-1, fetch=fetch)
        self.assertEqual(len(results), 130)
        self.assertFalse(more)
        self.assertEqual(parse_qs(urlsplit(self.requests[0]).query)['count'], ['-1'])

    def test_map_in_catalog_and_index_has_resolvable_map_id(self):
        xml = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <name>maps_en_canada</name><title>Canada map</title><flavour></flavour>
        <link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim"
          href="https://download.kiwix.org/zim/maps/maps_en_canada_2026-08a.zim.meta4" length="5" />
        </entry></feed>'''
        def fetch(url): return self.map_index if url == kiwix.MAP_INDEX else xml
        items = kiwix.search('canada map', fetch=fetch)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['id'], 'maps|maps_en_canada')
        self.assertEqual(kiwix.resolve(items[0]['id'], fetch=fetch)['filename'], items[0]['filename'])

    def test_bad_catalog_or_failed_map_index_is_an_error(self):
        with self.assertRaisesRegex(ValueError, 'unreadable'):
            kiwix.search('medicine', fetch=lambda _: b'<broken')
        def bad_index(url):
            return b'<html>temporary failure</html>' if url == kiwix.MAP_INDEX else self.xml
        with self.assertRaisesRegex(ValueError, 'maps'):
            kiwix.search('maps', fetch=bad_index)

    def test_catalog_fetch_retries_protocol_interruption(self):
        with patch.object(kiwix.urllib.request, 'urlopen', side_effect=[http.client.IncompleteRead(b''), io.BytesIO(b'feed')]), \
             patch.object(kiwix.time, 'sleep'):
            self.assertEqual(kiwix._fetch(kiwix.CATALOG, attempts=2), b'feed')

    def test_catalog_fetch_rejects_unexpectedly_large_response(self):
        with patch.object(kiwix, 'MAX_CATALOG_BYTES', 10), \
             patch.object(kiwix.urllib.request, 'urlopen', return_value=io.BytesIO(b'x' * 11)):
            with self.assertRaisesRegex(ValueError, 'unexpectedly large'):
                kiwix._fetch(kiwix.CATALOG)


if __name__ == '__main__':
    unittest.main()
