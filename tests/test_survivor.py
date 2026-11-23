import unittest

from sources import survivor


class SurvivorDiscoveryTests(unittest.TestCase):
    def test_discovery_accepts_real_html_link_forms_and_deduplicates(self):
        html = '''<a href="/index.php/library-canning/">Canning</a>
          <a href='http://survivorlibrary.com/index.php/library-canning'>Duplicate</a>
          <a href=/index.php/library-food/>Food &amp; Nutrition</a>
          <a href="https://evil.example/index.php/library-fake/">Fake</a>
          <a href="/index.php/other/">Other</a>'''
        result = survivor.discover_categories(fetch=lambda url: html)
        self.assertEqual([r['id'] for r in result], ['canning', 'food'])
        self.assertEqual(result[1]['title'], 'Food & Nutrition')
        self.assertEqual(result[0]['source_url'], 'https://www.survivorlibrary.com/index.php/library-canning/')

    def test_discovery_raises_on_missing_categories(self):
        with self.assertRaisesRegex(RuntimeError, 'No Survivor Library categories'):
            survivor.discover_categories(fetch=lambda url: '<html>maintenance</html>')

    def test_category_discovers_multiple_zips_sizes_and_ignores_other_hosts(self):
        page = 'https://www.survivorlibrary.com/index.php/library-canning/'
        html = '''<table><tr><td>2026-09-28</td><td>Canning.ZIP</td><td>283 mb</td>
          <td><a href=/library/Canning.ZIP download>ZIP 283 mb</a></td></tr>
          <tr><td>Big ZIP</td><td>1.5 gb</td><td><a href="/library/Big%20Book.zip">ZIP</a></td></tr></table>
          <a href="https://elsewhere.example/library/Malware.zip">ZIP</a>
          <a href="/library/Book.pdf">Book</a>'''
        result = survivor.category_downloads(page, fetch=lambda url: html)
        self.assertEqual([r['filename'] for r in result], ['Big Book.zip', 'Canning.ZIP'])
        self.assertEqual(result[1]['size'], 283000000)
        self.assertTrue(result[1]['size_approx'])
        self.assertEqual(result[0]['size'], 1500000000)
        self.assertEqual(result[0]['download_url'], 'https://www.survivorlibrary.com/library/Big%20Book.zip')
        self.assertTrue(all(r['source_url'] == page for r in result))

    def test_category_with_no_zip_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, 'No ZIP links found'):
            survivor.category_downloads('https://www.survivorlibrary.com/index.php/library-food/', fetch=lambda url: '<a href="/library/book.pdf">Book</a>')

    def test_category_distinguishes_no_zip_from_a_broken_page(self):
        url = 'https://www.survivorlibrary.com/index.php/library-food/'
        with self.assertRaises(survivor.NoCategoryZipError):
            survivor.category_downloads(url, fetch=lambda _: '<a href="/library/book.pdf">Book</a>')
        with self.assertRaisesRegex(RuntimeError, 'may have changed') as failure:
            survivor.category_downloads(url, fetch=lambda _: '<html>Temporarily unavailable</html>')
        self.assertNotIsInstance(failure.exception, survivor.NoCategoryZipError)

    def test_unclosed_link_is_discovered(self):
        # Matches the publisher's live unquoted link with its missing </a>.
        html = '<tr><td>2026-09-28</td><td>Food.ZIP</td><td>53 mb</td><td><a href=https://www.survivorlibrary.com/library/Food.ZIP target=blank download>ZIP 53 mb</td> </tr>'
        found = survivor.category_downloads('https://www.survivorlibrary.com/index.php/library-food/', fetch=lambda url: html)
        self.assertEqual(found[0]['filename'], 'Food.ZIP')
        self.assertEqual(found[0]['size'], 53000000)

    def test_invalid_category_url_is_rejected_without_fetch(self):
        for url in ('https://bad.example/index.php/library-food/', 'https://www.survivorlibrary.com/library/Food.ZIP'):
            with self.assertRaises(ValueError):
                survivor.category_downloads(url, fetch=lambda url: self.fail('must not fetch'))

    def test_category_contents_includes_individual_books_and_zip(self):
        page = 'https://www.survivorlibrary.com/index.php/library-food/'
        html = '''<a href="/library/Food.ZIP">ZIP 3 mb</a>
                  <a href="/library/Book%20one.pdf">PDF 1 mb</a>
                  <a href="/library/book-two.epub">EPUB</a>
                  <a href="https://evil.example/library/wrong.pdf">Bad</a>'''
        zips, books = survivor.category_contents(page, fetch=lambda _: html)
        self.assertEqual([f['filename'] for f in zips], ['Food.ZIP'])
        self.assertEqual([f['filename'] for f in books], ['Book one.pdf', 'book-two.epub'])
        self.assertEqual([f['kind'] for f in books], ['pdf', 'epub'])

    def test_recent_index_finds_unassigned_books_and_zips(self):
        pages = {
            survivor.INDEX: '<a href="/index.php/last-90-days/">90</a><a href="/index.php/last-180-days/">180</a>',
            'https://www.survivorlibrary.com/index.php/last-180-days/':
                '<a href="/library/orphan.pdf">PDF 2 mb</a><a href="/library/unlisted.ZIP">ZIP</a>'
                '<a href="https://evil.example/library/evil.pdf">PDF</a>',
        }
        requested = []
        def fetch(url):
            requested.append(url)
            return pages[url]
        days, files = survivor.recent_downloads(fetch=fetch)
        self.assertEqual(days, 180)
        self.assertEqual({f['filename'] for f in files}, {'orphan.pdf', 'unlisted.ZIP'})
        self.assertNotIn('https://www.survivorlibrary.com/index.php/last-90-days/', requested)

    def test_missing_or_broken_recent_index_is_reported(self):
        with self.assertRaisesRegex(RuntimeError, 'recent-books index'):
            survivor.recent_downloads(fetch=lambda _: '<html>No index</html>')
        def fetch(url):
            return '<a href="/index.php/last-180-days/">180</a>' if url == survivor.INDEX else '<html>Offline</html>'
        with self.assertRaisesRegex(RuntimeError, 'no book links'):
            survivor.recent_downloads(fetch=fetch)

    def test_fetch_failure_propagates(self):
        def unavailable(url):
            raise OSError('network unavailable')
        with self.assertRaisesRegex(OSError, 'network unavailable'):
            survivor.discover_categories(fetch=unavailable)


if __name__ == '__main__':
    unittest.main()
