"""Category selections should behave like saved download selections."""
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('category_integration', ROOT / 'cache_in_case.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def entry(name, edition='2026-01', category='medicine'):
    return ET.fromstring(('''<entry><id>{name}</id><name>{name}</name>
        <title>{name}</title><updated>{edition}</updated><flavour></flavour><category>{category}</category>
        <link type="application/x-zim" rel="http://opds-spec.org/acquisition/open-access"
        href="https://download.kiwix.org/zim/{name}_{edition}.zim.meta4" length="8" />
        </entry>''').format(name=name, edition=edition, category=category))


class CategoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'library'
        self.data.mkdir()
        for name, value in [('DATA', self.data), ('STATE', self.data / 'selections.json'),
                            ('LOG', self.data / 'log'), ('PREVIEW', False),
                            ('ARGS', []), ('_catalog_cache', None), ('_catalog_dir', None)]:
            active = patch.object(app, name, value)
            active.start()
            self.addCleanup(active.stop)
        self.edition = '2026-01'
        self.transfers = []

    def pages(self, category=None):
        self.assertEqual(category, 'medicine')
        yield [entry('one', self.edition), entry('two', self.edition)], 2, 2

    def transfer(self, url, target, kind, progress=None, progress_update=None):
        self.transfers.append(target.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'ZIM test')

    def choose_and_save(self):
        with patch.object(app, 'ARGS', ['kiwix', 'all', 'medicine']), \
             patch.object(app.kiwix, 'categories', return_value=[{'id': 'medicine', 'title': 'Medicine'}]), \
             patch.object(app.kiwix, 'catalog_pages', side_effect=self.pages), \
             patch.object(app.kiwix, '_maps', side_effect=AssertionError('map index should not load')), \
             patch.object(app.sys.stdin, 'isatty', return_value=True), \
             patch.object(app, 'confirm', side_effect=[True, False]), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(app.run(), 0)
        self.assertIn('Selections saved.', out.getvalue())
        self.assertIn('Uncategorized content and maps', out.getvalue())
        state = app.read_state()
        self.assertEqual(len(state), 1)
        self.assertEqual(state[0]['category'], 'medicine')
        return state

    def test_save_status_download_retry_and_upgrade_stay_in_category(self):
        state = self.choose_and_save()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            app.status(state)
        self.assertIn('ready to download', out.getvalue())
        with patch.object(app.kiwix, 'catalog_pages', side_effect=self.pages), \
             patch.object(app.kiwix, '_maps', side_effect=AssertionError('map index should not load')), \
             patch.object(app.download, 'download', side_effect=self.transfer), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.sync(state), 0)
            self.assertEqual(sorted(self.transfers), ['one_2026-01.zim', 'two_2026-01.zim'])
            (self.data / 'kiwix' / 'two_2026-01.zim').unlink()
            self.assertEqual(app.sync(app.read_state()), 0)
            self.assertEqual(self.transfers.count('one_2026-01.zim'), 1)
            self.assertEqual(self.transfers.count('two_2026-01.zim'), 2)
            (self.data / 'kiwix' / 'two_2026-01.zim').unlink()
            self.edition = '2026-11'
            self.assertEqual(app.sync(app.read_state(), update=True), 0)
        self.assertTrue((self.data / 'kiwix' / 'one_2026-01.zim').is_file())
        self.assertTrue((self.data / 'kiwix' / 'one_2026-11.zim').is_file())
        self.assertFalse((self.data / 'kiwix' / 'two_2026-11.zim').exists())
        self.assertEqual(app.read_state()[0]['category'], 'medicine')

    def test_failed_file_is_reported_and_can_be_retried_from_saved_state(self):
        state = self.choose_and_save()
        def fail_two(url, target, kind, progress=None, progress_update=None):
            if target.name.startswith('two_'):
                raise OSError('network failure')
            self.transfer(url, target, kind, progress, progress_update)
        with patch.object(app.kiwix, 'catalog_pages', side_effect=self.pages), \
             patch.object(app.kiwix, '_maps', side_effect=AssertionError('map index should not load')), \
             patch.object(app.download, 'download', side_effect=fail_two), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(app.sync(state), 1)
        self.assertIn('network failure', out.getvalue())
        self.assertTrue((self.data / 'kiwix' / 'one_2026-01.zim').exists())
        with patch.object(app.kiwix, 'catalog_pages', side_effect=self.pages), \
             patch.object(app.kiwix, '_maps', side_effect=AssertionError('map index should not load')), \
             patch.object(app.download, 'download', side_effect=self.transfer), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.sync(app.read_state()), 0)
        self.assertTrue((self.data / 'kiwix' / 'two_2026-01.zim').exists())
        self.assertEqual(self.transfers.count('one_2026-01.zim'), 1)

    def test_preview_category_download_does_not_change_saved_selection_or_download(self):
        state = self.choose_and_save()
        before = app.STATE.read_bytes()
        with patch.object(app, 'PREVIEW', True), \
             patch.object(app.kiwix, 'catalog_pages', side_effect=self.pages), \
             patch.object(app.kiwix, '_maps', side_effect=AssertionError('map index should not load')), \
             patch.object(app.download, 'download') as transfer, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(app.sync(state), 0)
        transfer.assert_not_called()
        self.assertEqual(app.STATE.read_bytes(), before)
        self.assertFalse((self.data / 'kiwix').exists())
        self.assertIn('Would download:', out.getvalue())

    def test_mismatched_category_is_reported_and_not_downloaded(self):
        state = self.choose_and_save()
        def wrong_category(category=None):
            self.assertEqual(category, 'medicine')
            yield [entry('unrelated', category='games')], 1, 1
        with patch.object(app.kiwix, 'catalog_pages', side_effect=wrong_category), \
             patch.object(app.kiwix, '_maps', side_effect=AssertionError('map index should not load')), \
             patch.object(app.download, 'download') as transfer, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(app.sync(state), 1)
        transfer.assert_not_called()
        self.assertIn('outside the selected category', out.getvalue())
        self.assertEqual(app.read_state()[0]['category'], 'medicine')
        self.assertFalse((self.data / 'kiwix').exists())


if __name__ == '__main__':
    unittest.main()
