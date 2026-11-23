import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cache_setup', ROOT / 'cache_in_case.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def entry(name, date='2026-01'):
    return ('<entry><id>urn:uuid:{n}</id><name>{n}</name><title>{n}</title>'
            '<updated>{d}-01T00:00:00Z</updated><flavour></flavour>'
            '<link type="application/x-zim" rel="http://opds-spec.org/acquisition/open-access" '
            'href="https://download.kiwix.org/zim/other/{n}_{d}.zim.meta4" length="8" /></entry>').format(n=name,d=date)


def feed(total, rows):
    return ('<feed xmlns="http://www.w3.org/2005/Atom"><totalResults>{}</totalResults>{}</feed>'
            .format(total, ''.join(rows))).encode()


class FullCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        self.item = {'source': 'kiwix-all', 'id': 'all', 'title': 'All Kiwix'}
        self.state = [self.item]
        self.patches = [patch.object(setup, 'DATA', self.data),
                        patch.object(setup, 'STATE', self.data / 'selections.json'),
                        patch.object(setup, 'LOG', self.data / 'log'),
                        patch.object(setup, 'PREVIEW', False)]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)
        self.maps = b'<a href="maps_en_canada_2026-06.zim">Canada</a>'

    def download(self, url, target, kind, progress=None, progress_update=None):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'ZIM fixture')

    def test_pagination_maps_and_deleted_file_recovery(self):
        def fetch(url):
            if url == setup.kiwix.MAP_INDEX: return self.maps
            params = parse_qs(urlsplit(url).query)
            self.assertEqual(params.get('lang'), ['eng'])
            start = int(params['start'][0])
            return feed(3, [entry('one'), entry('two')] if start == 0 else [entry('three')])
        with patch.object(setup.kiwix, '_fetch', side_effect=fetch), patch.object(setup.download, 'download', side_effect=self.download):
            setup.process(self.item, self.state)
            self.assertEqual(len(self.item['files']), 4)
            self.assertTrue((self.data/'kiwix/maps_en_canada_2026-06.zim').is_file())
            deleted = self.data/'kiwix/two_2026-01.zim'
            deleted.unlink()
            setup.process(self.item, self.state)
            self.assertTrue(deleted.is_file())

    def test_incomplete_catalog_does_not_report_success_and_saves_known_files(self):
        def fetch(url):
            if url == setup.kiwix.MAP_INDEX: return self.maps
            start = int(parse_qs(urlsplit(url).query)['start'][0])
            return feed(2, [entry('one')] if start == 0 else [])
        with patch.object(setup.kiwix, '_fetch', side_effect=fetch), patch.object(setup.download, 'download', side_effect=self.download):
            with self.assertRaisesRegex(RuntimeError, 'ended before'):
                setup.process(self.item, self.state)
        self.assertIn('kiwix/one_2026-01.zim', self.item['files'])
        self.assertTrue((self.data/'kiwix/one_2026-01.zim').exists())

    def test_preview_does_not_save_or_download(self):
        before = dict(self.item)
        def fetch(url):
            return self.maps if url == setup.kiwix.MAP_INDEX else feed(1, [entry('one')])
        with patch.object(setup, 'PREVIEW', True), patch.object(setup.kiwix, '_fetch', side_effect=fetch), patch.object(setup.download, 'download') as transfer:
            setup.process(self.item, self.state)
            transfer.assert_not_called()
        self.assertEqual(self.item, before)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_update_keeps_old_edition_and_downloads_new(self):
        current = ['2026-01']
        def fetch(url):
            return self.maps if url == setup.kiwix.MAP_INDEX else feed(1, [entry('one', current[0])])
        with patch.object(setup.kiwix, '_fetch', side_effect=fetch), patch.object(setup.download, 'download', side_effect=self.download):
            setup.process(self.item, self.state)
            current[0] = '2026-11'
            setup.process(self.item, self.state, update=True)
        self.assertTrue((self.data/'kiwix/one_2026-01.zim').exists())
        self.assertTrue((self.data/'kiwix/one_2026-11.zim').exists())

    def test_upgrade_only_downloads_new_editions_of_installed_entries(self):
        current = ['2026-01']
        def fetch(url):
            if url == setup.kiwix.MAP_INDEX: return self.maps
            rows = [entry('one', current[0]), entry('two', current[0])]
            if current[0] != '2026-01': rows.append(entry('new', current[0]))
            return feed(len(rows), rows)
        with patch.object(setup.kiwix, '_fetch', side_effect=fetch), \
             patch.object(setup.download, 'download', side_effect=self.download):
            setup.process(self.item, self.state)
            (self.data/'kiwix/two_2026-01.zim').unlink()
            current[0] = '2026-11'
            setup.process(self.item, self.state, update=True)
        self.assertTrue((self.data/'kiwix/one_2026-11.zim').is_file())
        self.assertFalse((self.data/'kiwix/two_2026-11.zim').exists())
        self.assertFalse((self.data/'kiwix/new_2026-11.zim').exists())
        self.assertIn('kiwix/two_2026-01.zim', self.item['files'])
        self.assertNotIn('kiwix/new_2026-11.zim', self.item['files'])

    def test_failed_upgrade_keeps_old_entry_and_can_retry(self):
        current = ['2026-01']
        def fetch(url):
            return self.maps if url == setup.kiwix.MAP_INDEX else feed(1, [entry('one', current[0])])
        with patch.object(setup.kiwix, '_fetch', side_effect=fetch), \
             patch.object(setup.download, 'download', side_effect=self.download):
            setup.process(self.item, self.state)
            current[0] = '2026-11'
            def fail_new(url, target, kind, progress=None, progress_update=None):
                if target.name == 'one_2026-11.zim': raise OSError('offline')
                self.download(url, target, kind, progress, progress_update)
            with patch.object(setup.download, 'download', side_effect=fail_new):
                with self.assertRaisesRegex(RuntimeError, 'offline'):
                    setup.process(self.item, self.state, update=True)
            self.assertIn('kiwix/one_2026-01.zim', self.item['files'])
            self.assertNotIn('kiwix/one_2026-11.zim', self.item['files'])
            setup.process(self.item, self.state, update=True)
        self.assertTrue((self.data/'kiwix/one_2026-01.zim').is_file())
        self.assertTrue((self.data/'kiwix/one_2026-11.zim').is_file())


if __name__ == '__main__': unittest.main()
