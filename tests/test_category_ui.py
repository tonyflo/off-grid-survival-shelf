"""Exercise the category choices in the actual interactive terminal."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from test_terminal import run_pty_steps


SCRIPT = '''
import importlib.util, pathlib, sys
from xml.etree import ElementTree as ET
spec = importlib.util.spec_from_file_location('app', pathlib.Path('cache_in_case.py'))
app = importlib.util.module_from_spec(spec); spec.loader.exec_module(app)
app.ARGS = sys.argv[1:]
app.kiwix.categories = lambda: [{'id':'wikipedia', 'title':'Wikipedia'}]
def pages(category=None, query=None):
    assert category == 'wikipedia', category
    node = ET.fromstring('<entry><id>example</id><name>example</name><title>Example</title>'
        '<category>wikipedia</category><updated>2026-01</updated><flavour></flavour>'
        '<link type="application/x-zim" rel="http://opds-spec.org/acquisition/open-access" '
        'href="https://download.kiwix.org/zim/example_2026-01.zim.meta4" length="123" /></entry>')
    yield [node], 1, 1
app.kiwix.catalog_pages = pages
app.kiwix._maps = lambda _: (_ for _ in ()).throw(AssertionError('maps are not categorized'))
sys.exit(app.main())
'''


class CategoryTerminalTests(unittest.TestCase):
    def test_bulk_select_category_and_save_without_download(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / 'library'
            steps = [('Choose a category by name', b'Wikipedia\n'),
                     ('Select this Kiwix category?', b'y\n'),
                     ('Download these selections now?', b'n\n')]
            code, out, completed = run_pty_steps([sys.executable, '-c', SCRIPT, 'kiwix', 'all'],
                                                  steps, {'CACHE_DATA': str(data)})
            self.assertEqual(completed, len(steps), out)
            self.assertEqual(code, 0, out)
            item = json.loads((data / 'selections.json').read_text())[0]
            self.assertEqual((item['source'], item['category']), ('kiwix-all', 'wikipedia'))
            self.assertIn('Estimated Kiwix category:', out)
            self.assertFalse(list(data.rglob('*.zim')))

    def test_browse_category_and_save_individual_item(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / 'library'
            steps = [('Choose a category by name', b'Wikipedia\n'),
                     ('Search by topic', b'\n'), ('Up/down:', b' \n'),
                     ('Download these selections now?', b'n\n')]
            code, out, completed = run_pty_steps([sys.executable, '-c', SCRIPT, 'browse', 'kiwix'],
                                                  steps, {'CACHE_DATA': str(data)})
            self.assertEqual(completed, len(steps), out)
            self.assertEqual(code, 0, out)
            item = json.loads((data / 'selections.json').read_text())[0]
            self.assertEqual(item['source'], 'kiwix')
            self.assertEqual(item['id'], 'example|')
            self.assertIn('Showing Kiwix category: wikipedia', out)
            self.assertFalse(list(data.rglob('*.zim')))


if __name__ == '__main__':
    unittest.main()
