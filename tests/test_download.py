import io
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from sources.download import DownloadError, download, install_last_light, remote_size


ZIM = b'ZIM\x04' + b'0123456789' * 1000


class Handler(BaseHTTPRequestHandler):
    calls = []
    head_calls = 0
    mode = 'normal'
    checksum = None
    def log_message(self, *args):
        pass
    def do_HEAD(self):
        type(self).head_calls += 1
        if self.mode == 'fail':
            self.send_response(405); self.end_headers(); return
        if self.mode == 'redirect' and self.path != '/final':
            self.send_response(302); self.send_header('Location', '/final'); self.end_headers(); return
        self.send_response(200)
        if self.mode != 'no-length':
            self.send_header('Content-Length', str(len(ZIM)))
        if self.mode == 'html':
            self.send_header('Content-Type', 'text/html')
        self.end_headers()
    def do_GET(self):
        if self.path.endswith('.sha256'):
            if self.checksum is None:
                self.send_response(404); self.end_headers(); return
            body = (self.checksum + '  example.zim\n').encode()
            self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        self.calls.append((self.headers.get('Range'), self.headers.get('If-Range')))
        start = 0
        if self.mode == 'fail':
            self.send_response(503); self.end_headers(); return
        if self.headers.get('Range') and self.mode != 'ignore-range':
            start = int(self.headers['Range'].split('=')[1].split('-')[0])
            self.send_response(206)
            range_start = 1 if self.mode == 'bad-range' else start
            self.send_header('Content-Range', f'bytes {range_start}-{len(ZIM)-1}/{len(ZIM)}')
        else:
            self.send_response(200)
        body = ZIM[start:]
        if self.mode != 'no-length':
            self.send_header('Content-Length', str(len(body)))
        self.send_header('ETag', '"version-2"' if self.mode == 'changed' else '"version-1"')
        self.end_headers()
        self.wfile.write(body)


class TransferTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch('socket.getfqdn', return_value='localhost'):
            cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:%d/example.zim' % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        Handler.mode = 'normal'; Handler.calls = []; Handler.head_calls = 0; Handler.checksum = None
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dest = self.root / 'example.zim'

    def tearDown(self):
        self.temp.cleanup()

    def partial(self, with_metadata=True):
        (self.root / 'example.zim.part').write_bytes(ZIM[:100])
        if with_metadata:
            (self.root / 'example.zim.part.json').write_text(json.dumps({'url': self.url, 'validator': '"version-1"'}))

    def test_download_and_skip(self):
        self.assertEqual(download(self.url, self.dest, 'zim', attempts=1), 'downloaded')
        self.assertEqual(self.dest.read_bytes(), ZIM)
        self.assertEqual(download(self.url, self.dest, 'zim', attempts=1), 'existing')
        self.assertEqual(len(Handler.calls), 1)

    def test_zim_checksum_when_published(self):
        Handler.checksum = hashlib.sha256(ZIM).hexdigest()
        messages = []
        self.assertEqual(download(self.url, self.dest, 'zim', attempts=1, progress=messages.append), 'downloaded')
        self.assertTrue(any('SHA-256 verified' in message for message in messages))

    def test_head_uses_the_server_size_without_downloading(self):
        self.assertEqual(remote_size(self.url), len(ZIM))
        self.assertEqual(Handler.head_calls, 1)
        self.assertEqual(Handler.calls, [])
        for mode in ('no-length', 'fail', 'html'):
            Handler.mode = mode
            self.assertIsNone(remote_size(self.url))

    def test_head_follows_redirects_without_starting_a_get(self):
        Handler.mode = 'redirect'
        self.assertEqual(remote_size(self.url), len(ZIM))
        self.assertEqual(Handler.head_calls, 2)
        self.assertEqual(Handler.calls, [])

    def test_unreadable_head_response_leaves_size_unknown(self):
        with patch('sources.download.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = http.client.BadStatusLine('broken')
            self.assertIsNone(remote_size(self.url))

    def test_progress_reports_completion_even_for_fast_downloads(self):
        messages, updates = [], []
        self.assertEqual(download(self.url, self.dest, 'zim', attempts=1,
                                  progress=messages.append, progress_update=updates.append), 'downloaded')
        self.assertTrue(updates[0].endswith('(0%)'))
        self.assertTrue(updates[-1].endswith('(100%)'))
        self.assertTrue(any('Checksum unavailable' in message for message in messages))

    def test_progress_without_content_length_shows_bytes_received(self):
        Handler.mode = 'no-length'
        updates = []
        self.assertEqual(download(self.url, self.dest, 'zim', attempts=1,
                                  progress=lambda _: None, progress_update=updates.append), 'downloaded')
        self.assertIn('MB received (total size unavailable)', updates[-1])

    def test_bad_zim_checksum_does_not_publish(self):
        Handler.checksum = '0' * 64
        with self.assertRaisesRegex(DownloadError, 'SHA-256 verification failed'):
            download(self.url, self.dest, 'zim', attempts=1)
        self.assertFalse(self.dest.exists())

    def test_pdf_validation_accepts_books_and_rejects_error_pages(self):
        from sources.download import _valid
        pdf = self.root/'book.pdf'
        pdf.write_bytes(b'%PDF-1.4\nbook data')
        self.assertTrue(_valid(pdf, 'pdf'))
        pdf.write_bytes(b'<html>Publisher is unavailable</html>')
        self.assertFalse(_valid(pdf, 'pdf'))

    def test_low_disk_space_fails_without_publishing(self):
        from types import SimpleNamespace
        with patch('sources.download.shutil.disk_usage', return_value=SimpleNamespace(free=1)):
            with self.assertRaisesRegex(DownloadError, 'Not enough free space'):
                download(self.url, self.dest, 'zim', attempts=1)
        self.assertFalse(self.dest.exists())

    def test_resumes_using_if_range(self):
        self.partial()
        self.assertEqual(download(self.url, self.dest, 'zim', attempts=1), 'downloaded')
        self.assertEqual(self.dest.read_bytes(), ZIM)
        self.assertEqual(Handler.calls[0], ('bytes=100-', '"version-1"'))

    def test_ignored_range_restarts(self):
        self.partial(); Handler.mode = 'ignore-range'
        self.assertEqual(download(self.url, self.dest, 'zim', attempts=1), 'downloaded')
        self.assertEqual(self.dest.read_bytes(), ZIM)

    def test_unvalidated_partial_restarts(self):
        self.partial(with_metadata=False)
        self.assertEqual(download(self.url, self.dest, 'zim', attempts=1), 'downloaded')
        self.assertEqual(Handler.calls[0][0], None)
        self.assertEqual(self.dest.read_bytes(), ZIM)

    def test_malformed_partial_metadata_restarts(self):
        self.partial()
        (self.root / 'example.zim.part.json').write_text('[]')
        self.assertEqual(download(self.url, self.dest, 'zim', attempts=1), 'downloaded')
        self.assertIsNone(Handler.calls[0][0])
        self.assertEqual(self.dest.read_bytes(), ZIM)

    def test_bad_range_fails_without_publishing(self):
        self.partial(); Handler.mode = 'bad-range'
        with self.assertRaises(DownloadError):
            download(self.url, self.dest, 'zim', attempts=1)
        self.assertFalse(self.dest.exists())
        self.assertEqual((self.root / 'example.zim.part').read_bytes(), ZIM[:100])

    def test_changed_validator_is_not_appended(self):
        self.partial(); Handler.mode = 'changed'
        with self.assertRaises(DownloadError):
            download(self.url, self.dest, 'zim', attempts=1)
        self.assertFalse(self.dest.exists())
        self.assertEqual((self.root / 'example.zim.part').read_bytes(), ZIM[:100])
        self.assertFalse((self.root / 'example.zim.part.json').exists())
        Handler.mode = 'normal'
        self.assertEqual(download(self.url, self.dest, 'zim', attempts=1), 'downloaded')
        self.assertIsNone(Handler.calls[-1][0])

    def test_failed_and_deleted_partial_can_retry(self):
        Handler.mode = 'fail'
        with self.assertRaises(DownloadError):
            download(self.url, self.dest, 'zim', attempts=1)
        self.assertFalse(self.dest.exists())
        Handler.mode = 'normal'
        self.assertEqual(download(self.url, self.dest, 'zim', attempts=1), 'downloaded')

    def test_http_protocol_interruption_is_retried(self):
        import urllib.request
        open_url = urllib.request.urlopen
        requests = []
        def interrupted_once(*args, **kwargs):
            requests.append(args[0])
            if len(requests) == 1:
                raise http.client.IncompleteRead(b'partial')
            return open_url(*args, **kwargs)
        with patch('sources.download.urllib.request.urlopen', side_effect=interrupted_once), \
             patch('sources.download.time.sleep'):
            self.assertEqual(download(self.url, self.dest, 'zim', attempts=2), 'downloaded')
        self.assertEqual(self.dest.read_bytes(), ZIM)

    def test_extract_and_preserve_existing(self):
        archive = self.root / 'guide.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('guide/index.html', 'offline guide')
            z.writestr('guide/book.pdf', 'book')
        site = self.root / 'site'
        self.assertEqual(install_last_light(archive, site), 'installed')
        self.assertEqual((site / 'index.html').read_text(), 'offline guide')
        self.assertEqual(install_last_light(archive, site), 'existing')

    def test_reject_zip_traversal(self):
        archive = self.root / 'evil.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('../escape', 'bad')
            z.writestr('index.html', 'hello')
        with self.assertRaises(DownloadError):
            install_last_light(archive, self.root / 'site')
        self.assertFalse((self.root / 'escape').exists())
        self.assertFalse((self.root / 'site').exists())

    def test_interrupted_extraction_can_retry(self):
        archive = self.root / 'guide.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('guide/index.html', 'original')
            z.writestr('guide/book.pdf', 'book')
        site = self.root / 'site'; site.mkdir()
        (site / '.cache-in-case-installing').touch()
        (site / 'index.html').write_text('already present')
        self.assertEqual(install_last_light(archive, site), 'installed')
        self.assertEqual((site / 'index.html').read_text(), 'already present')
        self.assertEqual((site / 'book.pdf').read_text(), 'book')
        self.assertFalse((site / '.cache-in-case-installing').exists())


if __name__ == '__main__':
    unittest.main()
