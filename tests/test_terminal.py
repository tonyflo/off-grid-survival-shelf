"""Exercise the real interactive UI in a pseudo-terminal, as over SSH."""
import errno
import fcntl
import json
import os
import pty
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELECT_SCRIPT = '''
import importlib.util, json, pathlib
spec = importlib.util.spec_from_file_location("cache_setup", pathlib.Path("cache_in_case.py"))
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
items = [
    {"id":"first", "title":"First title", "source_url":"https://example.org/first"},
    {"id":"second", "title":"Second title", "source_url":"https://example.org/second"},
]
items = app.select(items)
print("SELECT_RESULT=" + json.dumps([item["id"] for item in items]), flush=True)
'''

BROWSE_SCRIPT = '''
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location("cache_setup", pathlib.Path("cache_in_case.py"))
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
source, action = sys.argv[1:3]
app.ARGS = ["browse", source, "food"] if action == "browse" else [action]
app.kiwix.search_page = lambda query, count=-1: ([{"id":"food|", "title":"Food", "source_url":"https://example.org", "size":5}], False)
app.kiwix.resolve = lambda item_id: {"filename":"food.zim", "download_url":"https://example.org/food.zim", "size":5}
app.catalog.load = lambda _: {"survivor":[{"id":"food", "title":"Food", "source_url":"https://www.survivorlibrary.com/index.php/library-food/", "size":1234567}], "files":{}, "kiwix":{}}
app.survivor.discover_categories = lambda: (_ for _ in ()).throw(AssertionError("unexpected catalog request"))
app.survivor.category_contents = lambda url: ([{"filename":"Food.ZIP", "download_url":"https://www.survivorlibrary.com/library/Food.ZIP", "size":5}], [])
app.download.remote_size = lambda url: (_ for _ in ()).throw(AssertionError("unexpected size request"))
def transfer(url, target, kind, progress=None, progress_update=None, metadata_out=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fixture")
app.download.download = transfer
sys.exit(app.main())
'''

PROGRESS_SCRIPT = '''
import importlib.util, itertools, pathlib, threading, types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("cache_setup", pathlib.Path("cache_in_case.py"))
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)
app.DATA.mkdir(parents=True, exist_ok=True)
body = b"ZIM\\x04" + b"0" * (3 * 1024 * 1024)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.path.endswith(".sha256"):
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

# HTTPServer's default reverse lookup can stall on macOS runners. This server
# is local to the test, so its published name can be fixed without a DNS query.
with patch("socket.getfqdn", return_value="localhost"):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
clock = itertools.count(0, 1)
app.download.time = types.SimpleNamespace(monotonic=clock.__next__, sleep=lambda _: None)
try:
    url = "http://127.0.0.1:%d/example.zim" % server.server_port
    app.download.download(url, app.DATA / "example.zim", "zim", attempts=1,
                          progress=app.say, progress_update=app.show_progress)
    app.say("Complete")
finally:
    server.shutdown()
    server.server_close()
    thread.join()
'''


def run_pty(command, prompt, answer, env=None, cwd=ROOT):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
    process_env = dict(os.environ, TERM='xterm-256color', PYTHONUNBUFFERED='1')
    if env:
        for key, value in env.items():
            if value is None: process_env.pop(key, None)
            else: process_env[key] = value
    proc = subprocess.Popen(command, cwd=cwd, env=process_env, stdin=slave,
                            stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    data = bytearray()
    sent = False
    deadline = time.monotonic() + 12
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 16384)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b''
                if chunk:
                    data.extend(chunk)
                elif proc.poll() is not None:
                    break
            if not sent and prompt.encode() in data:
                os.write(master, answer)
                sent = True
            if proc.poll() is not None and not ready:
                break
        if proc.poll() is None:
            raise AssertionError('Terminal timed out: ' + data.decode(errors='replace'))
        return proc.wait(), data.decode(errors='replace'), sent
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        os.close(master)


def run_pty_steps(command, steps, env=None):
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
    process_env = dict(os.environ, TERM='xterm-256color', PYTHONUNBUFFERED='1')
    if env: process_env.update(env)
    proc = subprocess.Popen(command, cwd=ROOT, env=process_env, stdin=slave,
                            stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    data = bytearray(); step = 0; cursor = 0; deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try: chunk = os.read(master, 16384)
                except OSError as exc:
                    if exc.errno != errno.EIO: raise
                    chunk = b''
                if chunk: data.extend(chunk)
            if step < len(steps):
                prompt, answer = steps[step]
                position = data.find(prompt.encode(), cursor)
                if position >= 0:
                    cursor = position + len(prompt)
                    os.write(master, answer); step += 1
            if proc.poll() is not None: break
        if proc.poll() is None: raise AssertionError('Terminal timed out: ' + data.decode(errors='replace'))
        return proc.wait(), data.decode(errors='replace'), step
    finally:
        if proc.poll() is None: proc.kill(); proc.wait()
        os.close(master)


class TerminalTests(unittest.TestCase):
    def copy_app(self, root):
        repo = root / 'repo'
        repo.mkdir()
        shutil.copy(ROOT/'cache_in_case.py', repo/'cache_in_case.py')
        shutil.copytree(ROOT/'sources', repo/'sources', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        return repo

    def test_arrow_space_enter_selects_second_item(self):
        # xterm's application-keypad sequence for the down-arrow key.
        code, out, sent = run_pty([sys.executable, '-c', SELECT_SCRIPT],
                                  'Up/down:', b'\x1bOB \n')
        self.assertTrue(sent, out)
        self.assertEqual(code, 0, out)
        self.assertIn('SELECT_RESULT=["second"]', out)

    def test_q_cancels_selection(self):
        code, out, sent = run_pty([sys.executable, '-c', SELECT_SCRIPT],
                                  'Up/down:', b'q')
        self.assertTrue(sent, out)
        self.assertEqual(code, 0, out)
        self.assertIn('SELECT_RESULT=[]', out)

    def test_saved_survivor_size_appears_without_a_network_lookup(self):
        script = SELECT_SCRIPT.replace('items = app.select(items)',
                                       "items[0].update({'size':1234567, 'size_approx':True}); items = app.select(items, 'Survivor Library')")
        code, out, sent = run_pty([sys.executable, '-c', script], 'Up/down:', b'q')
        self.assertTrue(sent, out)
        self.assertEqual(code, 0, out)
        self.assertIn('(~1.2 MB)', out)
        self.assertIn('SELECT_RESULT=[]', out)

    def test_category_without_zip_can_be_selected_as_individual_books(self):
        script = SELECT_SCRIPT.replace('items = app.select(items)',
                                       "items[0]['no_zip'] = True; items = app.select(items, 'Survivor Library')")
        code, out, sent = run_pty([sys.executable, '-c', script], 'Up/down:', b' \n')
        self.assertTrue(sent, out)
        self.assertEqual(code, 0, out)
        self.assertIn('individual books', out)
        self.assertIn('SELECT_RESULT=["first"]', out)

    def test_real_transfer_updates_one_terminal_line_and_keeps_a_readable_log(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / 'library'
            code, out, sent = run_pty([sys.executable, '-c', PROGRESS_SCRIPT],
                                      'Downloading:', b'', {'CACHE_DATA': str(data)})
            self.assertTrue(sent, out)
            self.assertEqual(code, 0, out)
            self.assertIn('\r  [--------------------] 0.0 / 3.1 MB (0%)', out)
            self.assertIn('\r  [####################] 3.1 / 3.1 MB (100%)', out)
            self.assertGreaterEqual(out.count('\r  ['), 4)
            self.assertIn('(100%)\r\nChecksum unavailable', out)
            self.assertIn('\r\nComplete\r\n', out)
            log = (data / 'cache-in-case.log').read_bytes()
            self.assertNotIn(b'\r', log)
            self.assertIn(b'[--------------------] 0.0 / 3.1 MB (0%)\n', log)
            self.assertIn(b'[####################] 3.1 / 3.1 MB (100%)\n', log)

    def test_noninteractive_transfer_has_readable_progress_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / 'library'
            process = subprocess.run([sys.executable, '-c', PROGRESS_SCRIPT], cwd=ROOT,
                                     env=dict(os.environ, CACHE_DATA=str(data)), capture_output=True, text=True, timeout=20)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertNotIn('\r', process.stdout)
            self.assertIn('[--------------------] 0.0 / 3.1 MB (0%)\n', process.stdout)
            self.assertIn('[####################] 3.1 / 3.1 MB (100%)\n', process.stdout)

    def test_q_discards_unconfirmed_checklist_choices(self):
        code, out, sent = run_pty([sys.executable, '-c', SELECT_SCRIPT],
                                  'Up/down:', b' q')
        self.assertTrue(sent, out)
        self.assertEqual(code, 0, out)
        self.assertIn('SELECT_RESULT=[]', out)

    def test_space_toggles_items_on_and_off(self):
        code, out, sent = run_pty([sys.executable, '-c', SELECT_SCRIPT],
                                  'Up/down:', b' \x1bOB \x1bOA \n')
        self.assertTrue(sent, out)
        self.assertEqual(code, 0, out)
        self.assertIn('SELECT_RESULT=["second"]', out)

    def test_status_from_guided_menu(self):
        with tempfile.TemporaryDirectory() as temp:
            code, out, sent = run_pty([sys.executable, 'cache_in_case.py'],
                                      'Choose an action:', b'status\n',
                                      {'CACHE_DATA': str(Path(temp) / 'library')})
        self.assertTrue(sent, out)
        self.assertEqual(code, 0, out)
        self.assertIn('No selections saved.', out)

    def test_quit_from_guided_menu(self):
        with tempfile.TemporaryDirectory() as temp:
            code, out, sent = run_pty([sys.executable, 'cache_in_case.py'],
                                      'Choose an action:', b'quit\n',
                                      {'CACHE_DATA': str(Path(temp) / 'library')})
        self.assertTrue(sent, out)
        self.assertEqual(code, 0, out)
        self.assertIn('OFF-GRID SURVIVAL SHELF', out)
        self.assertIn('Build your own offline library of survival knowledge.', out)
        self.assertIn('\r\nOFF-GRID SURVIVAL SHELF\r\nBuild your own offline library of survival knowledge.', out)
        self.assertIn('New here? Start with a recommended collection.', out)
        self.assertIn('Refresh catalog listings and size estimates', out)
        self.assertIn('Check downloaded content for newer versions', out)
        self.assertNotIn('[|||]', out)
        self.assertIn('Cancelled.', out)

    def test_help_from_guided_menu(self):
        with tempfile.TemporaryDirectory() as temp:
            code, out, sent = run_pty([sys.executable, 'cache_in_case.py'],
                                      'Choose an action:', b'help\n',
                                      {'CACHE_DATA': str(Path(temp) / 'library')})
        self.assertTrue(sent, out)
        self.assertEqual(code, 0, out)
        self.assertIn('bash setup.sh browse kiwix', out)
        self.assertIn('Last Light is one guide', out)

    def test_kiwix_starter_topic_and_default_from_guided_menu(self):
        for answer, expected_ids in [
            (b'medical\n', {'wikipedia_en_medicine_maxi', 'medlineplus.gov_en_all',
                             'nhs.uk_en_medicines'}),
            (b'\n', {'wikipedia_en_medicine_maxi', 'medlineplus.gov_en_all',
                       'nhs.uk_en_medicines', 'ready.gov_en_all',
                       'cd3wdproject.org_en_all', 'solar.lowtechmagazine.com_mul_all',
                       'based.cooking_en_all'}),
        ]:
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as temp:
                data = Path(temp) / 'library'
                steps = [('Choose an action:', b'starter\n'),
                         ('Choose all, kiwix, survivor, or last-light [all]:', b'kiwix\n'),
                         ('Choose a topic (press Enter for the full starter):', answer),
                         ('Download these selections now?', b'n\n')]
                code, out, completed = run_pty_steps(
                    [sys.executable, 'cache_in_case.py'], steps, {'CACHE_DATA': str(data)})
                self.assertEqual(completed, len(steps), out)
                self.assertEqual(code, 0, out)
                self.assertEqual({item['id'] for item in json.loads((data/'selections.json').read_text())},
                                 expected_ids)
                self.assertFalse(list(data.rglob('*.zim')))

    def test_starter_defaults_to_all_sources_from_guided_menu(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / 'library'
            steps = [('Choose an action:', b'starter\n'),
                     ('Choose all, kiwix, survivor, or last-light [all]:', b'\n'),
                     ('Download these selections now?', b'n\n')]
            code, out, completed = run_pty_steps(
                [sys.executable, 'cache_in_case.py'], steps, {'CACHE_DATA': str(data)})
            self.assertEqual(completed, len(steps), out)
            self.assertEqual(code, 0, out)
            self.assertIn('Recommended content from all three sources', out)
            state = json.loads((data/'selections.json').read_text())
            self.assertEqual({item['source'] for item in state},
                             {'kiwix', 'survivor-file', 'last-light'})

    def test_browse_without_source_prompts_for_one(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)/'library'
            steps = [('Choose kiwix, survivor, or last-light:', b'last-light\n'),
                     ('Download these selections now?', b'n\n')]
            code, out, completed = run_pty_steps([sys.executable, 'cache_in_case.py', 'browse'],
                                                 steps, {'CACHE_DATA':str(data)})
            self.assertEqual(completed, len(steps), out)
            self.assertEqual(code, 0, out)
            self.assertIn('Selections saved.', out)
            self.assertIn('One offline survival guide', out)
            self.assertIn('Last Light is one offline survival guide', out)
            self.assertEqual(json.loads((data/'selections.json').read_text())[0]['source'], 'last-light')

    def test_survivor_all_explains_additional_books_before_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)/'library'
            code, out, sent = run_pty([sys.executable, 'cache_in_case.py', 'survivor', 'all'],
                                      'Select the Survivor library?', b'n\n',
                                      {'CACHE_DATA':str(data)})
            self.assertTrue(sent, out)
            self.assertEqual(code, 0, out)
            self.assertIn('category ZIPs and other books', out)
            self.assertFalse((data/'selections.json').exists())

    def test_default_config_and_custom_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.copy_app(root)
            env = os.environ.copy(); env.pop('CACHE_DATA', None)
            code, out, sent = run_pty([sys.executable, 'cache_in_case.py'], 'Choose an action:', b'quit\n',
                                      {'CACHE_DATA': None}, cwd=repo)
            self.assertTrue(sent, out)
            self.assertEqual(code, 0, out)
            config = repo/'config.json'
            self.assertEqual(json.loads(config.read_text())['library_path'], 'offline-library')
            library = root/'offline library'
            config.write_text(json.dumps({'library_path': str(library)}))
            status = subprocess.run([sys.executable, 'cache_in_case.py', 'status'], cwd=repo,
                                    env=env, capture_output=True, text=True)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn('Library: ' + str(library), status.stdout)

    def test_bad_config_does_not_block_help_or_environment_override(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = self.copy_app(root); library = root/'alternate library'
            (repo/'config.json').write_text('{broken')
            env = os.environ.copy(); env.pop('CACHE_DATA', None)
            help_result = subprocess.run([sys.executable, 'cache_in_case.py', '--help'], cwd=repo,
                                         env=env, capture_output=True, text=True)
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            status = subprocess.run([sys.executable, 'cache_in_case.py', 'status'], cwd=repo,
                                    env=dict(env, CACHE_DATA=str(library)), capture_output=True, text=True)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn('Library: ' + str(library), status.stdout)
            broken = subprocess.run([sys.executable, 'cache_in_case.py', 'status'], cwd=repo,
                                    env=env, capture_output=True, text=True)
            self.assertNotEqual(broken.returncode, 0)
            self.assertIn('Cannot read', broken.stderr)

    def test_bad_selections_do_not_block_help(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = self.copy_app(root); library = root/'library'
            library.mkdir(); (library/'selections.json').write_text('{broken')
            result = subprocess.run([sys.executable, 'cache_in_case.py', '--help'], cwd=repo,
                                    env=dict(os.environ, CACHE_DATA=str(library)), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('bash setup.sh', result.stdout)

    def test_legacy_path_is_migrated_without_moving_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = self.copy_app(root); library = root/'old library'
            library.mkdir(); (library/'keep.txt').write_text('preserved')
            (repo/'.cache-in-case-location').write_text(str(library)+'\n')
            code, out, sent = run_pty([sys.executable, 'cache_in_case.py'], 'Choose an action:', b'quit\n',
                                      {'CACHE_DATA': None}, cwd=repo)
            self.assertTrue(sent, out)
            self.assertEqual(code, 0, out)
            self.assertEqual(json.loads((repo/'config.json').read_text())['library_path'], str(library))
            self.assertEqual((library/'keep.txt').read_text(), 'preserved')

    def test_existing_default_data_folder_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = self.copy_app(root); library = repo/'data'
            library.mkdir(); (library/'keep.txt').write_text('preserved')
            code, out, sent = run_pty([sys.executable, 'cache_in_case.py'], 'Choose an action:', b'quit\n',
                                      {'CACHE_DATA': None}, cwd=repo)
            self.assertTrue(sent, out)
            self.assertEqual(code, 0, out)
            self.assertEqual(json.loads((repo/'config.json').read_text())['library_path'], 'data')
            self.assertEqual((library/'keep.txt').read_text(), 'preserved')

    def test_preview_creates_no_config_or_library(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = self.copy_app(root)
            code, out, sent = run_pty([sys.executable, 'cache_in_case.py', '--preview'], 'Choose an action:', b'quit\n',
                                      {'CACHE_DATA': None}, cwd=repo)
            self.assertTrue(sent, out)
            self.assertEqual(code, 0, out)
            self.assertFalse((repo/'config.json').exists())
            self.assertFalse((repo/'offline-library').exists())

    def test_select_items_past_first_100_without_loading_more(self):
        script = SELECT_SCRIPT.replace('items = app.select(items)',
            "items = [{'id':str(i), 'title':'Entry %03d' % i} for i in range(130)]; items = app.select(items, 'Kiwix')")
        code, out, sent = run_pty([sys.executable, '-c', script], 'Up/down:', b' ' + b'\x1b[6~' * 9 + b' \n')
        self.assertTrue(sent, out)
        self.assertEqual(code, 0, out)
        self.assertIn('130 results', out)
        self.assertIn('SELECT_RESULT=["0", "129"]', out)

    def test_browse_save_without_download_then_sync(self):
        for source, filename in [('kiwix', 'kiwix/food.zim'), ('survivor', 'survivor-library/zips/Food.ZIP')]:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temp:
                data = Path(temp) / 'library'
                env = {'CACHE_DATA': str(data)}
                steps = [('Up/down:', b' \n'), ('Download these selections now?', b'n\n')]
                code, out, completed = run_pty_steps([sys.executable, '-c', BROWSE_SCRIPT, source, 'browse'], steps, env)
                self.assertEqual(completed, len(steps), out)
                self.assertEqual(code, 0, out)
                self.assertIn('Selections saved.', out)
                self.assertEqual(len(json.loads((data/'selections.json').read_text())), 1)
                self.assertFalse((data/filename).exists())
                process_env = dict(os.environ, **env)
                sync = subprocess.run([sys.executable, '-c', BROWSE_SCRIPT, source, 'download'], cwd=ROOT,
                                      env=process_env, capture_output=True, text=True)
                self.assertEqual(sync.returncode, 0, sync.stdout + sync.stderr)
                self.assertTrue((data/filename).is_file())

    def test_bundled_survivor_catalog_browses_and_saves_without_network(self):
        script = '''
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('app', pathlib.Path('cache_in_case.py'))
app = importlib.util.module_from_spec(spec); spec.loader.exec_module(app)
app.ARGS = ['browse', 'survivor', 'food']
app.survivor.discover_categories = lambda: (_ for _ in ()).throw(AssertionError('index fetched'))
app.download.remote_size = lambda *a, **k: (_ for _ in ()).throw(AssertionError('size fetched'))
sys.exit(app.main())
'''
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)/'library'
            steps = [('Up/down:', b' \n'), ('Download these selections now?', b'n\n')]
            code, out, completed = run_pty_steps([sys.executable, '-c', script], steps, {'CACHE_DATA':str(data)})
            self.assertEqual(completed, len(steps), out)
            self.assertEqual(code, 0, out)
            self.assertIn('Food: ~', out)
            self.assertEqual(json.loads((data/'selections.json').read_text())[0]['id'], 'food')
            self.assertFalse(list(data.rglob('*.ZIP')))

    def test_preview_browse_does_not_save(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / 'library'
            steps = [('Up/down:', b' \n'), ('Preview these choices?', b'y\n')]
            code, out, completed = run_pty_steps([sys.executable, '-c', BROWSE_SCRIPT, 'kiwix', 'browse', '--preview'],
                                                 steps, {'CACHE_DATA': str(data)})
            self.assertEqual(completed, len(steps), out)
            self.assertEqual(code, 0, out)
            self.assertIn('Would download:', out)
            self.assertFalse(data.exists())


if __name__ == '__main__':
    unittest.main()
