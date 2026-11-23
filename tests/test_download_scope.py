"""Exercise download command routing with real selection and retry logic."""
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('download_scope', ROOT / 'cache_in_case.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class DownloadScopeTests(unittest.TestCase):
    def test_new_category_download_leaves_other_selections_for_bare_download(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / 'library'
            data.mkdir()
            node = ET.fromstring('''<entry><id>sample</id><name>sample</name>
              <title>Sample</title><updated>2026-01</updated><category>wikipedia</category>
              <flavour></flavour><link type="application/x-zim"
              rel="http://opds-spec.org/acquisition/open-access"
              href="https://example.org/sample_2026-01.zim.meta4" length="8" /></entry>''')
            calls = []

            def pages(category=None):
                self.assertEqual(category, 'wikipedia')
                yield [node], 1, 1

            def transfer(url, path, kind, **kwargs):
                calls.append(path.name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'fixture data')

            with patch.object(app, 'DATA', data), patch.object(app, 'STATE', data/'selections.json'), \
                 patch.object(app, 'LOG', data/'log'), patch.object(app, 'PREVIEW', False), \
                 patch.object(app, 'ARGS', ['download', 'kiwix', 'wikipedia']), \
                 patch.object(app, '_catalog_cache', None), patch.object(app, '_catalog_dir', None), \
                 patch.object(app.kiwix, 'categories', return_value=[{'id':'wikipedia', 'title':'Wikipedia'}]), \
                 patch.object(app.kiwix, 'catalog_pages', side_effect=pages), \
                 patch.object(app.download, 'download', side_effect=transfer), \
                 patch.object(app.sys.stdin, 'isatty', return_value=True), \
                 patch.object(app, 'confirm', side_effect=[True, True]), \
                 contextlib.redirect_stdout(io.StringIO()):
                app.save([{'source':'survivor-file', 'id':'Food.ZIP', 'title':'Food'}])
                self.assertEqual(app.run(), 0)
                self.assertEqual(calls, ['sample_2026-01.zim'])
                self.assertFalse((data/'survivor-library/zips/Food.ZIP').exists())
                self.assertEqual(len(app.read_state()), 2)
                app.ARGS[:] = ['download']
                with patch.object(app.sys.stdin, 'isatty', return_value=False):
                    self.assertEqual(app.run(), 0)
                self.assertEqual(calls, ['sample_2026-01.zim', 'Food.ZIP'])
                self.assertTrue((data/'survivor-library/zips/Food.ZIP').is_file())


if __name__ == '__main__':
    unittest.main()
