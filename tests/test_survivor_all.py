import contextlib
import importlib.util
import io
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location('cache_setup_test', Path(__file__).resolve().parents[1] / 'cache_in_case.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SurvivorAllTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.data = root / 'library'
        self.data.mkdir()
        self.publisher = root / 'publisher'
        self.publisher.mkdir()
        for name, members in [('Food.ZIP', ['folder/food.pdf']), ('Extra.ZIP', ['global.pdf'])]:
            with zipfile.ZipFile(self.publisher / name, 'w') as archive:
                for member in members:
                    archive.writestr(member, b'%PDF-1.4 archive book')
        for name in ('food.pdf', 'global.pdf', 'extra.pdf', 'standalone.pdf', 'orphan.pdf'):
            (self.publisher / name).write_bytes(b'%PDF-1.4 individual book ' + name.encode())

        self.requests = []
        requests = self.requests
        directory = str(self.publisher)
        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=directory, **kwargs)
            def do_GET(self):
                requests.append(self.path)
                super().do_GET()
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = 'http://127.0.0.1:{}/'.format(self.server.server_port)
        self.original = setup.DATA, setup.STATE, setup.LOG, setup.PREVIEW
        self.addCleanup(self.restore)
        setup.DATA, setup.STATE, setup.LOG, setup.PREVIEW = self.data, self.data/'selections.json', self.data/'log.txt', False
        self.all = {'source': 'survivor-all', 'id': 'all', 'title': 'Full Survivor Library'}
        self.state = [self.all]
        self.categories = [self.cat('food'), self.cat('shelter')]

    def restore(self):
        setup.DATA, setup.STATE, setup.LOG, setup.PREVIEW = self.original

    def cat(self, name):
        return {'id': name, 'title': name.title(), 'source_url': 'https://www.survivorlibrary.com/index.php/library-' + name + '/'}

    def entry(self, filename):
        return {'filename': filename, 'download_url': self.base + filename,
                'size': None, 'kind': filename.rsplit('.', 1)[-1].lower()}

    def category_contents(self, url):
        if 'food' in url:
            return [self.entry('Food.ZIP')], [self.entry('food.pdf'), self.entry('extra.pdf')]
        return [], [self.entry('standalone.pdf')]

    def recent(self):
        return 180, [self.entry(name) for name in ('Extra.ZIP', 'food.pdf', 'global.pdf', 'orphan.pdf')]

    def fixtures(self):
        return contextlib.ExitStack()

    def set_sources(self, stack):
        stack.enter_context(patch.object(setup.survivor, 'discover_categories', return_value=self.categories))
        stack.enter_context(patch.object(setup.survivor, 'category_contents', side_effect=self.category_contents))
        stack.enter_context(patch.object(setup.survivor, 'recent_downloads', side_effect=self.recent))

    def test_downloads_real_zips_and_uncovered_books_then_repairs_deletions(self):
        with self.fixtures() as stack:
            self.set_sources(stack)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(setup.sync(self.state), 0)
            self.assertEqual(set(self.requests), {'/Food.ZIP', '/Extra.ZIP', '/extra.pdf', '/standalone.pdf', '/orphan.pdf'})
            for name in ('extra.pdf', 'standalone.pdf', 'orphan.pdf'):
                self.assertTrue((self.data/'survivor-library/books'/name).read_bytes().startswith(b'%PDF-'))
            self.assertFalse((self.data/'survivor-library/books/food.pdf').exists())
            self.assertFalse((self.data/'survivor-library/books/global.pdf').exists())
            self.assertEqual(len(self.all['files']), 5)
            (self.data/'survivor-library/books/orphan.pdf').unlink()
            with contextlib.redirect_stdout(io.StringIO()) as out:
                setup.status(self.state)
            self.assertIn('missing (4 of 5 files present)', out.getvalue())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(setup.sync(self.state), 0)
            self.assertEqual(self.requests.count('/orphan.pdf'), 2)
            self.assertEqual(self.requests.count('/Food.ZIP'), 1)
            self.assertNotIn('error', json.loads(setup.STATE.read_text())[0])

    def test_preview_neither_saves_nor_downloads(self):
        setup.PREVIEW = True
        self.data.rmdir()
        before = json.dumps(self.state, sort_keys=True)
        with self.fixtures() as stack:
            self.set_sources(stack)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                setup.process(self.all, self.state)
        self.assertEqual(json.dumps(self.state, sort_keys=True), before)
        self.assertFalse(self.data.exists())
        self.assertEqual(self.requests, [])
        self.assertIn('individual books may also be downloaded', out.getvalue())

    def test_download_failure_is_reported_and_missing_file_is_retried(self):
        (self.publisher/'orphan.pdf').unlink()
        original = setup.download.download
        def attempt_once(url, dest, kind, **kwargs):
            return original(url, dest, kind, attempts=1, **kwargs)
        with self.fixtures() as stack:
            self.set_sources(stack)
            stack.enter_context(patch.object(setup.download, 'download', side_effect=attempt_once))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(setup.sync(self.state), 1)
            self.assertIn('orphan.pdf', out.getvalue())
            self.assertNotIn('All selected downloads completed', out.getvalue())
            self.assertIn('survivor-library/books/orphan.pdf', self.all['files'])
            (self.publisher/'orphan.pdf').write_bytes(b'%PDF-1.4 restored')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(setup.sync(self.state), 0)
            self.assertTrue((self.data/'survivor-library/books/orphan.pdf').exists())

    def test_index_error_does_not_report_success(self):
        with self.fixtures() as stack:
            self.set_sources(stack)
            stack.enter_context(patch.object(setup.survivor, 'recent_downloads', side_effect=RuntimeError('offline')))
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(setup.sync(self.state), 1)
        self.assertIn('Recent-books index: offline', out.getvalue())
        self.assertNotIn('All selected downloads completed', out.getvalue())

    def test_corrupt_existing_zip_is_not_replaced_and_books_are_not_downloaded(self):
        target = self.data/'survivor-library/zips/Food.ZIP'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'broken')
        with self.fixtures() as stack:
            self.set_sources(stack)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(setup.sync(self.state), 1)
        self.assertIn('Cannot read ZIP', out.getvalue())
        self.assertEqual(target.read_bytes(), b'broken')
        self.assertNotIn('/orphan.pdf', self.requests)

    def test_missing_previously_selected_zip_is_not_silently_discarded(self):
        self.all['categories'] = [{'source':'survivor-category', 'id':'food', 'title':'Food',
                                  'url':self.cat('food')['source_url'], 'files':['survivor-library/zips/old.ZIP']}]
        with self.fixtures() as stack:
            self.set_sources(stack)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(setup.sync(self.state), 1)
        self.assertIn('old.ZIP', out.getvalue())
        self.assertIn('survivor-library/zips/old.ZIP', self.all['files'])

    def test_upgrade_does_not_download_survivor(self):
        target = self.data/'survivor-library/zips/Food.ZIP'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'installed')
        self.all['files'] = ['survivor-library/zips/Food.ZIP']
        before = json.dumps(self.all, sort_keys=True)
        with patch.object(setup.survivor, 'discover_categories', side_effect=AssertionError('catalog fetched')):
            setup.process(self.all, self.state, update=True)
        self.assertEqual(json.dumps(self.all, sort_keys=True), before)


if __name__ == '__main__':
    unittest.main()
