"""The download command supports saved selections and new Kiwix batches."""
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('download_commands', ROOT / 'cache_in_case.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class DownloadCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'library'
        self.data.mkdir()
        for key, value in [('DATA', self.data), ('STATE', self.data / 'selections.json'),
                           ('LOG', self.data / 'cache-in-case.log'),
                           ('PREVIEW', False), ('ARGS', []), ('_catalog_cache', None),
                           ('_catalog_dir', None)]:
            active = patch.object(app, key, value)
            active.start()
            self.addCleanup(active.stop)

    def invoke(self, *args):
        app.ARGS[:] = args
        with contextlib.redirect_stdout(io.StringIO()):
            return app.run()

    def test_bare_download_runs_saved_selections_as_before(self):
        saved = [{'source': 'survivor-file', 'id': 'Food.ZIP', 'title': 'Food'}]
        app.save(saved)
        with patch.object(app, 'sync', return_value=0) as sync:
            self.assertEqual(self.invoke('download'), 0)
        self.assertEqual(sync.call_args.args[0], saved)
        self.assertEqual(sync.call_args.kwargs, {'ask': True})

    def test_all_selects_full_catalog_without_category_chooser(self):
        with patch.object(app.sys.stdin, 'isatty', return_value=True), \
             patch.object(app.kiwix, 'categories') as categories, \
             patch.object(app, 'show_full_library_estimate') as estimate, \
             patch.object(app, 'confirm', side_effect=[True, False]), \
             patch.object(app, 'sync') as sync:
            self.assertEqual(self.invoke('download', 'kiwix', 'all'), 0)
        categories.assert_not_called()
        estimate.assert_called_once_with('kiwix', category=None)
        sync.assert_not_called()
        self.assertEqual(app.read_state(), [{'source': 'kiwix-all', 'id': 'all',
                                            'title': 'Full Kiwix catalog'}])

    def test_named_category_download_only_runs_new_selection(self):
        existing = {'source': 'survivor-file', 'id': 'Food.ZIP', 'title': 'Food'}
        app.save([existing])
        with patch.object(app.sys.stdin, 'isatty', return_value=True), \
             patch.object(app.kiwix, 'categories', return_value=[{'id': 'wikipedia', 'title': 'Wikipedia'}]), \
             patch.object(app, 'show_full_library_estimate') as estimate, \
             patch.object(app, 'confirm', side_effect=[True, True]), \
             patch.object(app, 'sync', return_value=0) as sync:
            self.assertEqual(self.invoke('download', 'kiwix', 'Wikipedia'), 0)
        estimate.assert_called_once_with('kiwix', category='wikipedia')
        state, chosen = sync.call_args.args
        self.assertEqual(len(state), 2)
        self.assertEqual(chosen, [state[1]])
        self.assertEqual(chosen[0]['category'], 'wikipedia')
        self.assertEqual(state[0], existing)

    def test_preview_does_not_save_the_new_selection(self):
        with patch.object(app, 'PREVIEW', True), \
             patch.object(app.sys.stdin, 'isatty', return_value=True), \
             patch.object(app.kiwix, 'categories', return_value=[{'id': 'wikipedia', 'title': 'Wikipedia'}]), \
             patch.object(app, 'show_full_library_estimate'), \
             patch.object(app, 'confirm', side_effect=[True, True]), \
             patch.object(app, 'sync', return_value=0):
            self.assertEqual(self.invoke('download', 'kiwix', 'wikipedia'), 0)
        self.assertFalse(app.STATE.exists())

    def test_invalid_arguments_do_not_download_saved_selections(self):
        with patch.object(app, 'sync') as sync, \
             patch.object(app.sys.stdin, 'isatty', return_value=True), \
             patch.object(app.kiwix, 'categories', return_value=[{'id': 'wikipedia', 'title': 'Wikipedia'}]):
            for args in [('download', 'survivor'), ('download', 'kiwix', 'all', 'extra'),
                         ('download', 'kiwix', 'not-a-category'), ('sync', 'extra')]:
                with self.subTest(args=args), self.assertRaises(ValueError):
                    self.invoke(*args)
        sync.assert_not_called()


if __name__ == '__main__':
    unittest.main()
