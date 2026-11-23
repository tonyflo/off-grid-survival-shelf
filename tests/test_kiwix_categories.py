import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sources import kiwix


class KiwixCategoryTests(unittest.TestCase):
    def test_official_navigation_feed_supplies_category_ids(self):
        # Modelled on Kiwix's catalog/v2/categories navigation feed.
        xml = b'''<feed xmlns="http://www.w3.org/2005/Atom">
          <entry><title>Wikipedia</title><link rel="subsection"
          href="/catalog/v2/entries?category=wikipedia"/></entry>
          <entry><title>Health and medicine</title><link rel="subsection"
          href="/catalog/v2/entries?category=health%20and%20medicine"/></entry>
          </feed>'''
        urls = []
        def fetch(url):
            urls.append(url)
            return xml
        self.assertEqual(kiwix.categories(fetch), [
            {'id': 'wikipedia', 'title': 'Wikipedia'},
            {'id': 'health and medicine', 'title': 'Health and medicine'},
        ])
        self.assertEqual(urls, [kiwix.CATEGORIES])

    def test_category_feed_rejects_unreadable_or_incomplete_entries(self):
        for xml in (b'<feed>', b'<error/>',
                    b'<feed><entry><title>Bad</title></entry></feed>',
                    b'<feed><entry><title>Bad</title><link rel="subsection" href="?category=a,b"/></entry></feed>'):
            with self.subTest(xml=xml), self.assertRaises(ValueError):
                kiwix.categories(lambda _: xml)
        self.assertEqual(kiwix.categories(lambda _: b'<feed/>'), [])

    def test_category_filter_uses_upstream_filter_and_does_not_add_maps(self):
        urls = []
        xml = b'''<feed xmlns="http://www.w3.org/2005/Atom"><totalResults>1</totalResults>
          <entry><id>a</id><title>Example</title><name>example</name><category>wikipedia</category>
          <link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim"
          href="https://download.kiwix.org/zim/example.zim" length="123"/></entry></feed>'''
        def fetch(url):
            urls.append(url)
            if url == kiwix.MAP_INDEX:
                raise AssertionError('unclassified maps should not be added to an official category')
            return xml
        entries, more = kiwix.search_page('', count=-1, fetch=fetch, category='wikipedia')
        self.assertFalse(more)
        self.assertEqual(entries[0]['category'], 'wikipedia')
        self.assertEqual(parse_qs(urlsplit(urls[0]).query)['category'], ['wikipedia'])
        pages = list(kiwix.catalog_pages(fetch=fetch, category='wikipedia'))
        self.assertEqual(len(pages), 1)
        self.assertEqual(parse_qs(urlsplit(urls[-1]).query)['category'], ['wikipedia'])
        pages = list(kiwix.catalog_pages(fetch=fetch, category='wikipedia', query='  medicine  '))
        params = parse_qs(urlsplit(urls[-1]).query)
        self.assertEqual(params['category'], ['wikipedia'])
        self.assertEqual(params['q'], ['medicine'])

    def test_unclassified_entry_and_no_english_category_results(self):
        xml = b'''<feed xmlns="http://www.w3.org/2005/Atom"><totalResults>1</totalResults>
          <entry><id>a</id><title>Example</title><name>example</name><category></category>
          <link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim"
          href="https://download.kiwix.org/zim/example.zim" length="123"/></entry></feed>'''
        entries, _ = kiwix.search_page('example', fetch=lambda _: xml)
        self.assertEqual(entries[0]['category'], '')
        self.assertEqual(list(kiwix.catalog_pages(fetch=lambda _: b'<feed><totalResults>0</totalResults></feed>',
                                                  category='wikipedia')), [])
        self.assertEqual(list(kiwix.catalog_pages(fetch=lambda _: b'<feed><totalResults>0</totalResults></feed>',
                                                  query='no match')), [])
        with self.assertRaisesRegex(RuntimeError, 'no entries'):
            list(kiwix.catalog_pages(fetch=lambda _: b'<feed><totalResults>0</totalResults></feed>'))

    def test_filtered_catalog_still_checks_pagination_completeness(self):
        with self.assertRaisesRegex(RuntimeError, 'ended before'):
            list(kiwix.catalog_pages(fetch=lambda _: b'<feed><totalResults>1</totalResults></feed>',
                                     category='wikipedia'))


if __name__ == '__main__':
    unittest.main()
