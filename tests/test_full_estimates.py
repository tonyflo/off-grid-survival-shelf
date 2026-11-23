import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


spec = importlib.util.spec_from_file_location('estimate_setup', Path(__file__).resolve().parents[1] / 'cache_in_case.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def entry(name, date, size, category=''):
    length = '' if size is None else ' length="{}"'.format(size)
    return ('<entry><id>urn:{name}-{date}</id><name>{name}</name>'
            '<title>{name}</title><updated>{date}</updated><flavour></flavour><category>{category}</category>'
            '<link type="application/x-zim" rel="http://opds-spec.org/acquisition/open-access" '
            'href="https://download.kiwix.org/zim/{name}_{date}.zim.meta4"{length}/></entry>'
            .format(name=name, date=date, length=length, category=category))


def feed(total, entries):
    return ('<feed xmlns="http://www.w3.org/2005/Atom"><totalResults>{}</totalResults>{}</feed>'
            .format(total, ''.join(entries))).encode()


class FullEstimateTests(unittest.TestCase):
    def test_kiwix_uses_latest_editions_deduplicates_maps_and_reports_unknown(self):
        entries = [entry('one', '2024', 1000000000), entry('one', '2026', 2000000000),
                   entry('map', '2026', 1000000000), entry('unknown', '2026', None)]
        requests = []
        def fetch(url):
            params = parse_qs(urlsplit(url).query)
            self.assertEqual(params['lang'], ['eng'])
            start = int(params['start'][0])
            requests.append(start)
            return feed(4, entries[start:start + 2])  # Publisher limits pages to 2.
        maps = [{'id': 'maps|map', 'filename': 'map_2026.zim', 'updated': '2026', 'size': 1000000000}]
        with patch.object(app.kiwix, '_fetch', side_effect=fetch), \
             patch.object(app.kiwix, '_maps', return_value=maps), \
             patch.object(app.download, 'download') as transfer, contextlib.redirect_stdout(io.StringIO()) as out:
            app.show_full_library_estimate('kiwix')
        self.assertEqual(requests, [0, 2])
        transfer.assert_not_called()
        self.assertIn('~3.0 GB + 1 file with unknown sizes', out.getvalue())
        self.assertIn('existing downloads are not deducted', out.getvalue().lower())

    def test_incomplete_kiwix_catalog_does_not_claim_total(self):
        replies = [b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>',
                   feed(2, [entry('one', '2026', 1)])]
        for reply in replies:
            with self.subTest(reply=reply), patch.object(app.kiwix, '_fetch', return_value=reply), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                app.show_full_library_estimate('kiwix')
            self.assertIn('size: unavailable', out.getvalue())

    def test_kiwix_category_estimate_filters_and_excludes_map_index(self):
        requests = []
        def fetch(url):
            params = parse_qs(urlsplit(url).query)
            requests.append(params)
            return feed(1, [entry('one', '2026', 2000000000, 'wikipedia')])
        with patch.object(app.kiwix, '_fetch', side_effect=fetch), \
             patch.object(app.kiwix, '_maps') as maps, contextlib.redirect_stdout(io.StringIO()) as out:
            app.show_full_library_estimate('kiwix', category='wikipedia')
        maps.assert_not_called()
        self.assertEqual(requests[0]['category'], ['wikipedia'])
        self.assertIn('Estimated Kiwix category: ~2.0 GB', out.getvalue())

    def test_empty_kiwix_category_is_not_saved_or_confirmed(self):
        with patch.object(app, 'ARGS', ['kiwix', 'all', 'wikipedia']), \
             patch.object(app, 'PREVIEW', True), \
             patch.object(app.sys.stdin, 'isatty', return_value=True), \
             patch.object(app.kiwix, 'categories', return_value=[{'id':'wikipedia','title':'Wikipedia'}]), \
             patch.object(app.kiwix, 'catalog_pages', return_value=iter(())), \
             patch.object(app, 'remember') as remember, patch.object(app, 'confirm') as confirm, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(app.run(), 0)
        self.assertIn('No English downloads found', out.getvalue())
        confirm.assert_not_called()
        remember.assert_not_called()

    def test_survivor_uses_saved_sizes_and_labels_unknown_books(self):
        saved = {'survivor': [{'size': 2000000000}, {'size': 1000000000}, {}]}
        with patch.object(app, 'current_catalog', return_value=saved), \
             patch.object(app.survivor, 'discover_categories') as network, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            app.show_full_library_estimate('survivor')
        network.assert_not_called()
        self.assertIn('~3.0 GB (2 of 3 categories have sizes)', out.getvalue())
        self.assertIn('Additional individual books', out.getvalue())
        self.assertIn('may be counted more than once', out.getvalue())

    def test_all_command_shows_estimate_before_confirmation_without_download(self):
        saved = {'survivor': [{'size': 2000000000}], 'kiwix': {}, 'files': {}}
        output = io.StringIO()
        def confirm(_prompt):
            self.assertIn('Estimated category ZIPs: ~2.0 GB', output.getvalue())
            return False
        with patch.object(app, 'ARGS', ['survivor', 'all']), patch.object(app, 'PREVIEW', True), \
             patch.object(app.catalog, 'load', return_value=saved), \
             patch.object(app.sys.stdin, 'isatty', return_value=True), \
             patch.object(app, 'confirm', side_effect=confirm), \
             patch.object(app.download, 'download') as transfer, contextlib.redirect_stdout(output):
            self.assertEqual(app.run(), 0)
        transfer.assert_not_called()
        self.assertIn('Cancelled.', output.getvalue())


if __name__ == '__main__':
    unittest.main()
