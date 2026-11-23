import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class LockTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.env = dict(os.environ, CACHE_DATA=str(self.data))

    def tearDown(self):
        self.temp.cleanup()

    def run_setup(self, *args):
        return subprocess.run(['bash', str(ROOT / 'setup.sh'), *args], env=self.env, capture_output=True, text=True)

    def test_active_lock_blocks_writers_but_not_status_or_preview(self):
        with (self.data / '.cache-in-case.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            active = self.run_setup('sync')
            self.assertNotEqual(active.returncode, 0)
            self.assertIn('already running', active.stderr)
            status = self.run_setup('status')
            self.assertEqual(status.returncode, 0)
            self.assertIn('No selections saved', status.stdout)
            preview = self.run_setup('sync', '--preview')
            self.assertNotIn('already running', preview.stderr)
            self.assertFalse((self.data / 'cache-in-case.log').exists())

    def test_catalog_refresh_can_run_during_download_but_not_twice(self):
        script = '''
import importlib.util, pathlib, sys
spec = importlib.util.spec_from_file_location('app', pathlib.Path('cache_in_case.py'))
app = importlib.util.module_from_spec(spec); spec.loader.exec_module(app)
app.ARGS = [sys.argv[1], 'kiwix']
app.catalog.refresh = lambda *args, **kwargs: 0
sys.exit(app.main())
'''
        def refresh(command):
            return subprocess.run(['python3', '-c', script, command], cwd=ROOT, env=self.env,
                                  capture_output=True, text=True, timeout=20)
        with (self.data/'.cache-in-case.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for command in ('update', 'refresh-catalog'):
                result = refresh(command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with (self.data/'.catalog-refresh.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for command in ('update', 'refresh-catalog'):
                result = refresh(command)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('catalog refresh is already running', result.stderr)

    def test_last_light_marker_is_incomplete_and_sync_finishes(self):
        last = self.data / 'last-light'; site = last / 'site'; site.mkdir(parents=True)
        (site / '.cache-in-case-installing').touch()
        (site / 'index.html').write_text('already present')
        archive = last / 'last-light-survival-guide-standard.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('guide/index.html', 'original')
            z.writestr('guide/book.pdf', 'book')
        (self.data / 'selections.json').write_text(json.dumps([
            {'source': 'last-light', 'id': 'standard', 'title': 'Last Light'}
        ]))
        status = self.run_setup('status')
        self.assertEqual(status.returncode, 0)
        self.assertIn('extraction incomplete', status.stdout)
        sync = self.run_setup('sync')
        self.assertEqual(sync.returncode, 0, sync.stdout + sync.stderr)
        self.assertEqual((site / 'index.html').read_text(), 'already present')
        self.assertEqual((site / 'book.pdf').read_text(), 'book')
        self.assertFalse((site / '.cache-in-case-installing').exists())


if __name__ == '__main__':
    unittest.main()
