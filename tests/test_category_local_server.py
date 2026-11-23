"""Exercise a category against a local HTTPS publisher, including actual transfers."""
import contextlib
import hashlib
import importlib.util
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('category_local_server', ROOT / 'cache_in_case.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


@unittest.skipUnless(shutil.which('openssl'), 'local HTTPS fixture needs openssl')
class LocalCategoryServerTests(unittest.TestCase):
    def test_real_paged_catalog_preview_save_and_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key, cert = root / 'key.pem', root / 'cert.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-keyout', str(key), '-out', str(cert), '-days', '1',
                            '-subj', '/CN=localhost'], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            requests = []
            body = b'ZIM\x04fixture'
            digest = hashlib.sha256(body).hexdigest().encode('ascii')

            class Publisher(BaseHTTPRequestHandler):
                def log_message(self, *args): pass

                def do_GET(self):
                    parts = urlsplit(self.path)
                    params = parse_qs(parts.query)
                    requests.append((parts.path, params))
                    base = 'https://127.0.0.1:{}'.format(self.server.server_port)
                    if parts.path == '/categories':
                        payload = ('''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
                            <title>Medicine</title><link rel="subsection" href="/entries?category=medicine"/>
                            </entry></feed>''').encode()
                    elif parts.path == '/entries':
                        if params.get('lang') != ['eng'] or params.get('category') != ['medicine']:
                            self.send_error(400)
                            return
                        start = int(params['start'][0])
                        name = ['one', 'two'][start] if start < 2 else None
                        rows = ('''<entry><id>{name}</id><name>{name}</name><title>{name}</title>
                            <updated>2026-01</updated><category>medicine</category><flavour></flavour>
                            <link type="application/x-zim" rel="http://opds-spec.org/acquisition/open-access"
                            href="{base}/zim/{name}_2026-01.zim.meta4" length="11"/></entry>'''
                            .format(name=name, base=base)) if name else ''
                        payload = ('<feed xmlns="http://www.w3.org/2005/Atom">'
                                   '<totalResults>2</totalResults>{}</feed>'.format(rows)).encode()
                    elif parts.path.endswith('.zim.sha256'):
                        payload = digest + b'\n'
                    elif parts.path.endswith('.zim'):
                        payload = body
                    else:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            with patch.object(socket, 'getfqdn', return_value='localhost'):
                server = ThreadingHTTPServer(('127.0.0.1', 0), Publisher)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            base = 'https://127.0.0.1:{}'.format(server.server_port)
            data = root / 'library'
            data.mkdir()
            with patch.object(ssl, '_create_default_https_context', ssl._create_unverified_context), \
                 patch.object(app.kiwix, 'CATALOG', base + '/entries'), \
                 patch.object(app.kiwix, 'CATEGORIES', base + '/categories'), \
                 patch.object(app, 'DATA', data), patch.object(app, 'STATE', data / 'selections.json'), \
                 patch.object(app, 'LOG', data / 'log'), patch.object(app, 'ARGS', ['kiwix', 'all', 'medicine']), \
                 patch.object(app, '_catalog_cache', None), patch.object(app, '_catalog_dir', None), \
                 patch.object(app.sys.stdin, 'isatty', return_value=True), \
                 patch.object(app, 'confirm', side_effect=[True, True, True, True]), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                with patch.object(app, 'PREVIEW', True):
                    self.assertEqual(app.run(), 0)
                self.assertFalse((data / 'selections.json').exists())
                self.assertFalse((data / 'kiwix').exists())
                self.assertFalse(any(path.startswith('/zim/') for path, _ in requests))
                with patch.object(app, 'PREVIEW', False):
                    self.assertEqual(app.run(), 0)
                self.assertEqual(app.read_state()[0]['category'], 'medicine')
                self.assertEqual((data / 'kiwix' / 'one_2026-01.zim').read_bytes(), body)
                self.assertEqual((data / 'kiwix' / 'two_2026-01.zim').read_bytes(), body)
                self.assertIn('SHA-256 verified', out.getvalue())
            entries = [p for path, p in requests if path == '/entries']
            self.assertEqual({p['start'][0] for p in entries}, {'0', '1'})
            self.assertTrue(all(p['category'] == ['medicine'] and p['lang'] == ['eng'] for p in entries))
            self.assertTrue(any(path == '/categories' for path, _ in requests))
            self.assertFalse(any('/maps/' in path for path, _ in requests))


if __name__ == '__main__':
    unittest.main()
