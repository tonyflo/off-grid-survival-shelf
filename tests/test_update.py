import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cache_update_test', ROOT / 'cache_in_case.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        for attr, value in [('DATA', self.data), ('STATE', self.data/'selections.json'), ('LOG', self.data/'log'), ('PREVIEW', False), ('ARGS', [])]:
            p = patch.object(app, attr, value); p.start(); self.addCleanup(p.stop)
        self.downloads = []
        self.real_download = app.download.download
        def transfer(url, path, kind, progress=None, progress_update=None, metadata_out=None):
            self.downloads.append(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if kind == 'zip':
                with zipfile.ZipFile(path, 'w') as z: z.writestr('guide/index.html', 'updated guide')
            else:
                path.write_bytes(b'ZIM\x04updated')
        p = patch.object(app.download, 'download', side_effect=transfer); p.start(); self.addCleanup(p.stop)

    def write_state(self, state):
        app.save(state)

    def test_updates_kiwix_without_removing_older_edition(self):
        old = self.data/'kiwix/example_2026-01.zim'; old.parent.mkdir(); old.write_bytes(b'old')
        item = {'source':'kiwix', 'id':'example|', 'title':'Example', 'files':['kiwix/example_2026-01.zim']}
        state = [item]; self.write_state(state)
        resolved = {'filename':'example_2026-11.zim', 'download_url':'https://example.com/new.zim', 'size': 12}
        with patch.object(app.kiwix, 'resolve', return_value=resolved):
            self.assertEqual(app.sync(state, update=True), 0)
        self.assertEqual(old.read_bytes(), b'old')
        self.assertTrue((self.data/'kiwix/example_2026-11.zim').is_file())
        self.assertEqual(item['files'], ['kiwix/example_2026-11.zim'])

    def test_does_not_downgrade_kiwix_or_download_uninstalled(self):
        old = self.data/'kiwix/example_2026-11.zim'; old.parent.mkdir(); old.write_bytes(b'newer')
        item = {'source':'kiwix', 'id':'example|', 'title':'Example', 'files':['kiwix/example_2026-11.zim']}
        missing = {'source':'kiwix', 'id':'missing|', 'title':'Missing'}
        state = [item, missing]; self.write_state(state)
        resolved = {'filename':'example_2026-01.zim', 'download_url':'https://example.com/old.zim', 'size': 1}
        with patch.object(app.kiwix, 'resolve', return_value=resolved) as lookup:
            self.assertEqual(app.sync(state, update=True), 0)
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(self.downloads, [])
        self.assertEqual(item['files'], ['kiwix/example_2026-11.zim'])

    def test_skipped_upgrade_preserves_previous_download_error(self):
        item = {'source':'survivor-file', 'id':'Food.ZIP', 'title':'Food', 'error':'connection lost'}
        state = [item]; self.write_state(state)
        self.assertEqual(app.sync(state, update=True), 0)
        self.assertEqual(item['error'], 'connection lost')
        self.assertEqual(json.loads(app.STATE.read_text())[0]['error'], 'connection lost')

    def test_failed_kiwix_update_can_retry_without_losing_original(self):
        old = self.data/'kiwix/example_2026-01.zim'; old.parent.mkdir(); old.write_bytes(b'old')
        item = {'source':'kiwix', 'id':'example|', 'title':'Example', 'files':['kiwix/example_2026-01.zim']}
        state = [item]; self.write_state(state)
        resolved = {'filename':'example_2026-11.zim', 'download_url':'https://example.com/new.zim', 'size': 12}
        with patch.object(app.kiwix, 'resolve', return_value=resolved), patch.object(app.download, 'download', side_effect=OSError('offline')):
            self.assertEqual(app.sync(state, update=True), 1)
        self.assertEqual(item['files'], ['kiwix/example_2026-01.zim'])
        self.assertEqual(old.read_bytes(), b'old')
        with patch.object(app.kiwix, 'resolve', return_value=resolved):
            self.assertEqual(app.sync(state, update=True), 0)
        self.assertTrue((self.data/'kiwix/example_2026-11.zim').exists())

    def test_last_light_stages_without_changing_current_guide(self):
        site = self.data/'last-light/site'; site.mkdir(parents=True); (site/'index.html').write_text('current guide')
        state = [{'source':'last-light', 'id':'standard', 'title':'Last Light',
                  'last_light_source': {'etag':'"old"'}}]; self.write_state(state)
        with patch.object(app.download, 'remote_metadata', return_value={'etag':'"new"', 'size':123}):
            self.assertEqual(app.sync(state, update=True), 0)
        self.assertEqual((site/'index.html').read_text(), 'current guide')
        self.assertEqual((self.data/'last-light/site-update/index.html').read_text(), 'updated guide')
        app.status(state)
        self.assertEqual(app.sync(state, update=True), 0)
        self.assertEqual(len(self.downloads), 1)

    def test_last_light_update_preview_does_not_create_files(self):
        site = self.data/'last-light/site'; site.mkdir(parents=True); (site/'index.html').write_text('guide')
        state = [{'source':'last-light', 'id':'standard', 'title':'Last Light',
                  'last_light_source': {'etag':'"old"'}}]; self.write_state(state)
        before = sorted((p.relative_to(self.data), p.read_bytes() if p.is_file() else None) for p in self.data.rglob('*'))
        with patch.object(app, 'PREVIEW', True), patch.object(app.download, 'remote_metadata', return_value={'etag':'"new"', 'size':123}):
            self.assertEqual(app.sync(state, update=True), 0)
        after = sorted((p.relative_to(self.data), p.read_bytes() if p.is_file() else None) for p in self.data.rglob('*'))
        self.assertEqual(before, after)
        self.assertEqual(self.downloads, [])

    def test_last_light_unchanged_and_unknown_are_not_downloaded(self):
        site = self.data/'last-light/site'; site.mkdir(parents=True); (site/'index.html').write_text('guide')
        item = {'source':'last-light', 'id':'standard', 'title':'Last Light',
                'last_light_source': {'etag':'"same"'}}
        with patch.object(app.download, 'remote_metadata', return_value={'etag':'"same"', 'size':123}), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(app.sync([item], update=True), 0)
        self.assertIn('already up to date', out.getvalue())
        self.assertEqual(self.downloads, [])
        item.pop('last_light_source')
        with patch.object(app.download, 'remote_metadata', return_value={'etag':'"same"', 'size':123}), \
             patch.object(app.sys.stdin, 'isatty', return_value=False), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(app.sync([item], update=True), 0)
        self.assertIn('Cannot tell whether Last Light has changed', out.getvalue())
        self.assertIn('No Last Light download started', out.getvalue())
        self.assertEqual(self.downloads, [])

    def test_legacy_last_light_archive_with_different_size_is_staged(self):
        site = self.data/'last-light/site'; site.mkdir(parents=True); (site/'index.html').write_text('guide')
        archive = self.data/'last-light/last-light-survival-guide-standard.zip'
        archive.write_bytes(b'older')
        item = {'source':'last-light', 'id':'standard', 'title':'Last Light'}
        with patch.object(app.download, 'remote_metadata', return_value={'etag':'"new"', 'size':123}):
            self.assertEqual(app.sync([item], update=True), 0)
        self.assertEqual((site/'index.html').read_text(), 'guide')
        self.assertEqual((self.data/'last-light/site-update/index.html').read_text(), 'updated guide')

    def test_last_light_check_failure_does_not_download(self):
        site = self.data/'last-light/site'; site.mkdir(parents=True); (site/'index.html').write_text('guide')
        item = {'source':'last-light', 'id':'standard', 'title':'Last Light'}
        with patch.object(app.download, 'remote_metadata', return_value=None), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(app.sync([item], update=True), 1)
        self.assertIn('Could not check', out.getvalue())
        self.assertEqual(self.downloads, [])

    def test_real_last_light_update_uses_publisher_etag(self):
        def package(content):
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w') as archive:
                archive.writestr('guide/index.html', content)
            return output.getvalue()
        class Publisher(BaseHTTPRequestHandler):
            body = package('original')
            etag = '"first"'
            gets = 0
            def log_message(self, *args): pass
            def send_headers(self):
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Length', str(len(type(self).body)))
                self.send_header('ETag', type(self).etag)
                self.end_headers()
            def do_HEAD(self): self.send_headers()
            def do_GET(self):
                type(self).gets += 1
                self.send_headers()
                self.wfile.write(type(self).body)
        with patch('socket.getfqdn', return_value='localhost'):
            server = ThreadingHTTPServer(('127.0.0.1', 0), Publisher)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:{}/guide.zip'.format(server.server_port)
        item = {'source':'last-light', 'id':'standard', 'title':'Last Light'}
        with patch.object(app, 'LAST_URL', url), patch.object(app.download, 'download', wraps=self.real_download), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.sync([item]), 0)
            self.assertEqual(item['last_light_source']['etag'], '"first"')
            self.assertEqual(app.sync([item], update=True), 0)
            self.assertEqual(Publisher.gets, 1)
            changed = package('revised!')
            self.assertEqual(len(changed), len(Publisher.body))
            Publisher.body, Publisher.etag = changed, '"second"'
            self.assertEqual(app.sync([item], update=True), 0)
        self.assertEqual(Publisher.gets, 2)
        self.assertEqual((self.data/'last-light/site/index.html').read_text(), 'original')
        self.assertEqual((self.data/'last-light/site-update/index.html').read_text(), 'revised!')

    def test_preview_is_read_only_and_survivor_same_name_is_preserved(self):
        dest = self.data/'survivor-library/zips/Food.ZIP'; dest.parent.mkdir(parents=True); dest.write_bytes(b'old ZIP')
        state = [{'source':'survivor-file', 'id':'Food.ZIP', 'title':'Food', 'files':['survivor-library/zips/Food.ZIP']}]
        self.write_state(state); before = app.STATE.read_bytes()
        with patch.object(app, 'PREVIEW', True): self.assertEqual(app.sync(state, update=True), 0)
        self.assertEqual(app.STATE.read_bytes(), before)
        self.assertEqual(dest.read_bytes(), b'old ZIP')
        self.assertEqual(app.sync(state, update=True), 0)
        self.assertEqual(dest.read_bytes(), b'old ZIP')
        self.assertEqual(self.downloads, [])


if __name__ == '__main__': unittest.main()
