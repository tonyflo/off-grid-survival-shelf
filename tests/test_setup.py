import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('cache_setup', Path(__file__).resolve().parents[1] / 'cache_in_case.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class SetupIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'library with spaces'
        for name, value in [('DATA', self.data), ('STATE', self.data/'selections.json'),
                            ('LOG', self.data/'cache-in-case.log'), ('PREVIEW', False),
                            ('CONFIG', self.data.parent/'config.json'),
                            ('ARGS', []), ('STARTER_SURVIVOR', ['Canning.ZIP'])]:
            p = patch.object(app, name, value)
            p.start(); self.addCleanup(p.stop)
        self.downloaded = []
        def download(url, target, kind, progress=None, progress_update=None, metadata_out=None):
            self.downloaded.append(target.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'fixture')
            if progress: progress('Transfer progress: ' + target.name)
        p = patch.object(app.download, 'download', side_effect=download)
        p.start(); self.addCleanup(p.stop)
        def install_last_light(archive, target):
            target.mkdir(parents=True, exist_ok=True)
            (target/'index.html').write_text('guide')
        p = patch.object(app.download, 'install_last_light', side_effect=install_last_light)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(app.sys.stdin, 'isatty', return_value=False)
        self.stdin_tty = p.start(); self.addCleanup(p.stop)
        saved_catalog = {
            'survivor': [{'id':'food', 'title':'Food', 'source_url':'https://www.survivorlibrary.com/index.php/library-food/', 'size':1234567}],
            'files': {'https://www.survivorlibrary.com/library/Canning.ZIP': {'size':1234567}},
            'kiwix': {}}
        p = patch.object(app.catalog, 'load', return_value=saved_catalog)
        p.start(); self.addCleanup(p.stop)

    def run_action(self, *args, preview=False):
        app.ARGS[:] = args
        app.PREVIEW = preview
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = app.main()
        return code, output.getvalue()

    def state(self):
        return json.loads((self.data/'selections.json').read_text())

    def test_adding_another_source_preserves_previous_selection(self):
        code, _ = self.run_action('starter', 'survivor')
        self.assertEqual(code, 0)
        code, out = self.run_action('last-light')
        self.assertEqual(code, 0, out)
        self.assertEqual({x['source'] for x in self.state()}, {'survivor-file', 'last-light'})
        self.assertEqual(self.downloaded.count('Canning.ZIP'), 1)
        self.assertIn('Transfer progress: Canning.ZIP', app.LOG.read_text())
        self.assertTrue((self.data/'last-light/site/index.html').exists())

    def test_sync_retries_a_deleted_selected_download(self):
        self.run_action('starter', 'survivor')
        (self.data/'survivor-library/zips/Canning.ZIP').unlink()
        code, out = self.run_action('sync')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.downloaded.count('Canning.ZIP'), 2)
        self.assertTrue((self.data/'survivor-library/zips/Canning.ZIP').exists())

    def test_failure_saves_selection_and_rerun_clears_error(self):
        with patch.object(app.download, 'download', side_effect=OSError('connection reset')):
            code, out = self.run_action('starter', 'survivor')
        self.assertEqual(code, 1, out)
        self.assertIn('Canning.ZIP', out)
        self.assertIn('connection reset', out)
        self.assertNotIn('All selected downloads completed', out)
        self.assertEqual(self.state()[0]['id'], 'Canning.ZIP')
        self.assertIn('connection reset', self.state()[0]['error'])
        code, out = self.run_action('sync')
        self.assertEqual(code, 0, out)
        self.assertNotIn('error', self.state()[0])
        self.assertTrue((self.data/'survivor-library/zips/Canning.ZIP').exists())

    def test_preview_neither_saves_nor_downloads(self):
        code, out = self.run_action('starter', 'survivor', preview=True)
        self.assertEqual(code, 0, out)
        self.assertIn('Would download: Canning.ZIP', out)
        self.assertFalse(self.data.exists())
        self.assertEqual(self.downloaded, [])

    def test_kiwix_starter_topics_save_exact_choices_without_downloading(self):
        expected = {
            'medical': {'wikipedia_en_medicine_maxi', 'medlineplus.gov_en_all', 'nhs.uk_en_medicines'},
            'preparedness': {'ready.gov_en_all', 'cd3wdproject.org_en_all', 'solar.lowtechmagazine.com_mul_all'},
            'food': {'based.cooking_en_all'},
        }
        self.stdin_tty.return_value = True
        previous = set()
        with patch('builtins.input', return_value='n'):
            for topic, prefixes in expected.items():
                code, out = self.run_action('starter', 'kiwix', topic)
                self.assertEqual(code, 0, out)
                state = self.state()
                self.assertEqual({x['id'] for x in state} - previous, prefixes)
                self.assertTrue(all(x['source'] == 'kiwix' and x['prefix'] for x in state))
                if topic == 'food':
                    self.assertIn('Based Cooking: Recipes', out)
                    self.assertEqual(next(x for x in state if x['id'] == 'based.cooking_en_all')['title'],
                                     'Based Cooking: Recipes')
                previous |= prefixes
        self.assertEqual(self.downloaded, [])

    def test_kiwix_starter_without_topic_still_selects_full_starter(self):
        self.stdin_tty.return_value = True
        with patch('builtins.input', return_value='n'):
            code, out = self.run_action('starter', 'kiwix')
        self.assertEqual(code, 0, out)
        self.assertEqual([x['id'] for x in self.state()], app.STARTER_KIWIX)
        self.assertEqual(self.downloaded, [])

    def test_kiwix_starter_topic_preview_does_not_save_or_download(self):
        def resolve(prefix):
            return {'filename': prefix + '_2026-01.zim', 'download_url': 'https://example.org/' + prefix + '.zim', 'size': 123}
        with patch.object(app.kiwix, 'resolve_prefix', side_effect=resolve):
            code, out = self.run_action('starter', 'kiwix', 'food', preview=True)
        self.assertEqual(code, 0, out)
        self.assertIn('Would download: based.cooking_en_all_2026-01.zim', out)
        self.assertFalse(self.data.exists())
        self.assertEqual(self.downloaded, [])

    def test_kiwix_starter_topic_retries_and_upgrades_through_saved_prefix(self):
        def resolve(prefix):
            return {'filename': prefix + '_2026-01.zim', 'download_url': 'https://example.org/old.zim', 'size': 123}
        with patch.object(app.kiwix, 'resolve_prefix', side_effect=resolve):
            code, out = self.run_action('starter', 'kiwix', 'food')
            self.assertEqual(code, 0, out)
            old = self.data/'kiwix/based.cooking_en_all_2026-01.zim'
            old.unlink()
            code, out = self.run_action('download')
            self.assertEqual(code, 0, out)
            self.assertTrue(old.exists())
        newer = {'filename': 'based.cooking_en_all_2026-02.zim', 'download_url': 'https://example.org/new.zim', 'size': 123}
        with patch.object(app.kiwix, 'resolve_prefix', return_value=newer) as lookup:
            code, out = self.run_action('upgrade')
        self.assertEqual(code, 0, out)
        lookup.assert_called_once_with('based.cooking_en_all')
        self.assertTrue(old.exists())
        self.assertTrue((self.data/'kiwix/based.cooking_en_all_2026-02.zim').exists())

    def test_kiwix_starter_rejects_invalid_topics_and_extra_arguments(self):
        for args in [('starter', 'kiwix', 'unknown'), ('starter', 'survivor', 'food'),
                     ('starter', 'kiwix', 'food', 'extra')]:
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, 'Kiwix starter topic'):
                self.run_action(*args, preview=True)
        self.assertFalse(self.data.exists())

    def test_last_light_can_be_browsed_in_preview(self):
        code, out = self.run_action('browse', 'last-light', preview=True)
        self.assertEqual(code, 0, out)
        self.assertIn('https://bunkerbot.co.uk/download.html', out)
        self.assertIn('Would download:', out)
        self.assertFalse(self.data.exists())

    def test_help_lists_log_and_preview(self):
        code, out = self.run_action('--help')
        self.assertEqual(code, 0)
        self.assertIn('bash setup.sh log', out)
        self.assertIn('--preview', out)
        self.assertIn('browse last-light', out)
        self.assertIn('starter last-light', out)
        self.assertIn('bash setup.sh download', out)
        self.assertIn('bash setup.sh upgrade', out)
        self.assertNotIn('Catalog sources:', out)
        self.assertTrue(all(len(line) <= 80 for line in out.splitlines()))

    def test_update_refreshes_metadata_without_downloading_or_changing_selections(self):
        state = [{'source':'kiwix', 'id':'example|', 'title':'Example'}]
        app.save(state)
        before = (self.data/'selections.json').read_bytes()
        with patch.object(app.catalog, 'refresh', return_value=0) as refresh, \
             patch.object(app.download, 'download', side_effect=AssertionError('downloaded an archive')):
            code, out = self.run_action('update', 'survivor', 'food')
        self.assertEqual(code, 0, out)
        self.assertEqual(refresh.call_args.args[2], 'survivor')
        self.assertEqual(refresh.call_args.kwargs['topic'], 'food')
        self.assertEqual((self.data/'selections.json').read_bytes(), before)
        self.assertEqual(self.downloaded, [])

    def test_refresh_catalog_alias_still_refreshes_metadata(self):
        with patch.object(app.catalog, 'refresh', return_value=0) as refresh:
            code, out = self.run_action('refresh-catalog', 'last-light')
        self.assertEqual(code, 0, out)
        self.assertEqual(refresh.call_args.args[2], 'last-light')

    def test_update_preview_is_read_only(self):
        with patch.object(app.catalog, 'refresh', side_effect=AssertionError('network used')):
            code, out = self.run_action('update', preview=True)
        self.assertEqual(code, 0, out)
        self.assertIn('Would refresh', out)
        self.assertFalse(self.data.exists())

    def test_download_and_sync_both_retry_missing_saved_files(self):
        self.run_action('starter', 'survivor')
        target = self.data/'survivor-library/zips/Canning.ZIP'
        for command in ('download', 'sync'):
            target.unlink()
            code, out = self.run_action(command)
            self.assertEqual(code, 0, out)
            self.assertTrue(target.is_file())
        self.assertEqual(self.downloaded.count('Canning.ZIP'), 3)

    def test_sync_summarizes_only_missing_downloads_and_can_cancel(self):
        installed = self.data/'survivor-library/zips/Canning.ZIP'
        installed.parent.mkdir(parents=True)
        installed.write_bytes(b'old file')
        state = [{'source':'survivor-file', 'id':'Canning.ZIP', 'title':'Canning',
                  'files':['survivor-library/zips/Canning.ZIP']},
                 {'source':'kiwix', 'id':'example|', 'title':'Example'},
                 {'source':'survivor-file', 'id':'Unknown.ZIP', 'title':'Unknown'}]
        app.save(state)
        resolved = {'filename':'example.zim', 'download_url':'https://example.org/example.zim', 'size':2000000}
        self.stdin_tty.return_value = True
        with patch.object(app.kiwix, 'resolve', return_value=resolved) as lookup, \
             patch('builtins.input', return_value='n'):
            code, out = self.run_action('sync')
        self.assertEqual(code, 0, out)
        summary = out.split('Download summary:')[1]
        self.assertIn('Example: ~2.0 MB', summary)
        self.assertIn('Unknown: size unknown', summary)
        self.assertIn('Estimated total: ~2.0 MB + unknown sizes', summary)
        self.assertNotIn('Canning:', summary)
        self.assertIn('Your selections are still saved', out)
        self.assertEqual(self.downloaded, [])
        self.assertEqual(lookup.call_count, 1)

    def test_sync_resolves_once_then_downloads_after_summary(self):
        app.save([{'source':'kiwix', 'id':'example|', 'title':'Example'}])
        resolved = {'filename':'example.zim', 'download_url':'https://example.org/example.zim', 'size':2000000}
        with patch.object(app.kiwix, 'resolve', return_value=resolved) as lookup:
            code, out = self.run_action('sync')
        self.assertEqual(code, 0, out)
        self.assertLess(out.index('Download summary:'), out.index('Transfer progress:'))
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(self.downloaded, ['example.zim'])

    def test_sync_counts_the_same_missing_archive_once(self):
        archive = {'filename':'Food.ZIP', 'download_url':'https://www.survivorlibrary.com/library/Food.ZIP', 'size':1234567}
        state = [{'source':'survivor-category', 'id':'food', 'title':'Food category',
                  'url':'https://www.survivorlibrary.com/index.php/library-food/'},
                 {'source':'survivor-file', 'id':'Food.ZIP', 'title':'Food ZIP'}]
        self.data.mkdir(parents=True)
        with patch.object(app.survivor, 'category_contents', return_value=([archive], [])), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            app.download_summary(state)
        self.assertIn('Food category: ~1.2 MB', out.getvalue())
        self.assertNotIn('Food ZIP:', out.getvalue())
        self.assertIn('Estimated total: ~1.2 MB', out.getvalue())

    def test_status_reports_actual_files_and_estimated_remaining_sizes(self):
        target = self.data/'survivor-library/zips/Canning.ZIP'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'x' * 2000000)
        state = [{'source':'survivor-file', 'id':'Canning.ZIP', 'title':'Canning',
                  'files':['survivor-library/zips/Canning.ZIP']},
                 {'source':'kiwix', 'id':'map|', 'title':'A map', 'size':3000000}]
        with patch.object(app.kiwix, 'resolve', side_effect=AssertionError('status used network')), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            app.status(state)
        text = out.getvalue()
        self.assertIn('Canning: downloaded (2.0 MB)', text)
        self.assertIn('A map: ready to download (estimated ~3.0 MB)', text)
        self.assertIn('Library folder size: 2.0 MB', text)
        self.assertIn('Estimated downloads remaining: ~3.0 MB', text)

    def test_status_folder_size_includes_extracted_and_staged_last_light(self):
        base = self.data/'last-light'
        (base/'site').mkdir(parents=True)
        (base/'site'/'index.html').write_bytes(b'x' * 1000000)
        (base/'site-update').mkdir()
        (base/'site-update'/'index.html').write_bytes(b'y' * 1000000)
        (base/'last-light-survival-guide-standard.zip').write_bytes(b'z' * 1000000)
        state = [{'source':'last-light', 'id':'standard', 'title':'Last Light',
                  'files':['last-light/last-light-survival-guide-standard.zip']}]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            app.status(state)
        self.assertIn('Last Light: downloaded (ZIP 1.0 MB) (update staged)', out.getvalue())
        self.assertIn('Library folder size: 3.0 MB', out.getvalue())

    def test_upgrade_downloads_newer_kiwix_without_removing_old_file(self):
        old = self.data/'kiwix/example_2026-01.zim'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'old')
        app.save([{'source':'kiwix', 'id':'example|', 'title':'Example',
                   'files':['kiwix/example_2026-01.zim']}])
        resolved = {'filename':'example_2026-11.zim', 'download_url':'https://example.org/example.zim', 'size':12}
        with patch.object(app.kiwix, 'resolve', return_value=resolved):
            code, out = self.run_action('upgrade')
        self.assertEqual(code, 0, out)
        self.assertEqual(old.read_bytes(), b'old')
        self.assertTrue((self.data/'kiwix/example_2026-11.zim').is_file())

    def test_upgrade_skips_selections_that_were_never_downloaded(self):
        state = [{'source':'kiwix', 'id':'missing|', 'title':'Missing Kiwix'},
                 {'source':'kiwix-all', 'id':'all', 'title':'All Kiwix'},
                 {'source':'survivor-file', 'id':'Food.ZIP', 'title':'Food'},
                 {'source':'survivor-category', 'id':'food', 'title':'Food category',
                  'url':'https://www.survivorlibrary.com/index.php/library-food/'},
                 {'source':'survivor-all', 'id':'all', 'title':'All Survivor'}]
        app.save(state)
        with patch.object(app.kiwix, 'resolve', side_effect=AssertionError('catalog fetched')), \
             patch.object(app.survivor, 'discover_categories', side_effect=AssertionError('catalog fetched')), \
             patch.object(app.survivor, 'category_downloads', side_effect=AssertionError('category fetched')):
            code, out = self.run_action('upgrade')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.downloaded, [])
        self.assertEqual(self.state(), state)
        self.assertIn('Skip upgrade (not downloaded)', out)

    def test_starter_shows_download_size_before_the_choice_to_download(self):
        self.stdin_tty.return_value = True
        with patch('builtins.input', return_value='n'):
            code, out = self.run_action('starter', 'survivor')
        self.assertEqual(code, 0, out)
        self.assertIn('Saved download sizes', out)
        self.assertIn('Canning.ZIP: ~1.2 MB', out)
        self.assertEqual(self.state()[0]['id'], 'Canning.ZIP')
        self.assertEqual(self.downloaded, [])

    def test_unavailable_size_does_not_prevent_saving_or_downloading(self):
        self.stdin_tty.return_value = True
        with patch.object(app, 'selection_size', return_value=None), \
             patch('builtins.input', return_value='y'):
            code, out = self.run_action('starter', 'survivor')
        self.assertEqual(code, 0, out)
        self.assertIn('Canning.ZIP: size unknown', out)
        self.assertEqual(self.downloaded, ['Canning.ZIP'])

    def test_survivor_browse_uses_saved_catalog_without_network(self):
        self.stdin_tty.return_value = True
        item = {'id':'food', 'title':'Food', 'source_url':'https://www.survivorlibrary.com/index.php/library-food/', 'size':1234567}
        with patch.object(app.survivor, 'discover_categories', side_effect=AssertionError('network used')), \
             patch.object(app.download, 'remote_size', side_effect=AssertionError('network used')), \
             patch.object(app, 'select', return_value=[item]), \
             patch('builtins.input', return_value='n'):
            code, out = self.run_action('browse', 'survivor', 'food')
        self.assertEqual(code, 0, out)
        self.assertIn('Using the saved Survivor catalog', out)
        self.assertIn('Food: ~1.2 MB', out)
        self.assertEqual(self.state()[0]['id'], 'food')
        self.assertEqual(self.downloaded, [])

    def test_refresh_preview_does_not_make_network_requests_or_write(self):
        with patch.object(app.catalog, 'refresh', side_effect=AssertionError('network used')):
            code, out = self.run_action('refresh-catalog', preview=True)
        self.assertEqual(code, 0, out)
        self.assertIn('Would refresh', out)
        self.assertFalse(self.data.exists())

    def test_status_groups_sources_and_keeps_error_details(self):
        state = [{'source':'kiwix', 'id':'map|', 'title':'East Timor map'},
                 {'source':'survivor-file', 'id':'Food.ZIP', 'title':'Food',
                  'files':['survivor-library/zips/Food.ZIP'], 'error':'connection lost'},
                 {'source':'last-light', 'id':'standard', 'title':'Last Light'}]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            app.status(state)
        output = out.getvalue()
        self.assertIn('\nKiwix\n  East Timor map: ready to download', output)
        self.assertIn('\nSurvivor Library\n  Food: missing (0 of 1 files present)', output)
        self.assertIn('Last error: connection lost', output)
        self.assertIn('\nLast Light\n  Last Light: ready to download', output)
        self.assertNotIn('automatically checked for damage', output)

    def test_menu_empty_topic_browses_without_asking_twice(self):
        self.stdin_tty.return_value = True
        with patch('builtins.input', side_effect=['browse', 'kiwix', '', '']) as prompts, \
             patch.object(app.kiwix, 'categories', return_value=[{'id':'wikipedia','title':'Wikipedia'}]), \
             patch.object(app.kiwix, 'search_page', return_value=([], False)) as search, \
             patch.object(app, 'select', return_value=[]):
            code, out = self.run_action(preview=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(prompts.call_count, 4)
        self.assertIn('press Enter to browse everything', str(prompts.call_args))
        search.assert_called_once_with('', count=-1)

    def test_browse_does_not_show_an_incomplete_kiwix_catalog(self):
        self.stdin_tty.return_value = True
        with patch.object(app.kiwix, 'search_page', return_value=([{'id':'x','title':'X'}], True)), \
             patch.object(app, 'select') as select:
            with self.assertRaisesRegex(RuntimeError, 'Could not verify the complete Kiwix catalog'):
                self.run_action('browse', 'kiwix', 'example', preview=True)
        select.assert_not_called()

    def test_exit_leaves_the_menu(self):
        self.stdin_tty.return_value = True
        with patch('builtins.input', return_value='exit'):
            code, out = self.run_action()
        self.assertEqual(code, 0, out)
        self.assertIn('Cancelled.', out)

    def test_preview_does_not_change_existing_selection_or_log(self):
        self.run_action('starter', 'survivor')
        files = [self.data/'selections.json', self.data/'cache-in-case.log',
                 self.data/'survivor-library/zips/Canning.ZIP']
        before = [f.read_bytes() for f in files]
        code, out = self.run_action('sync', preview=True)
        self.assertEqual(code, 0, out)
        self.assertEqual([f.read_bytes() for f in files], before)
        self.assertEqual(self.downloaded.count('Canning.ZIP'), 1)

    def test_not_downloading_now_saves_selection_for_sync(self):
        self.stdin_tty.return_value = True
        with patch('builtins.input', return_value='n'):
            code, out = self.run_action('last-light')
        self.assertEqual(code, 0)
        self.assertIn('Selections saved.', out)
        self.assertIn('bash setup.sh download', out)
        self.assertNotIn('All selected downloads completed', out)
        self.assertEqual(self.state()[0]['source'], 'last-light')
        self.assertEqual(self.downloaded, [])
        self.stdin_tty.return_value = False
        code, out = self.run_action('sync')
        self.assertEqual(code, 0, out)
        self.assertTrue((self.data/'last-light/site/index.html').is_file())

    def test_browsed_kiwix_selection_can_be_downloaded_later(self):
        self.stdin_tty.return_value = True
        result = {'id':'example|', 'title':'Example', 'source_url':'https://example.org', 'size':1}
        resolved = {'filename':'example_2026-01.zim', 'download_url':'https://example.org/file.zim', 'size':1}
        with patch.object(app.kiwix, 'search_page', return_value=([result], False)), \
             patch.object(app, 'select', return_value=[result]), \
             patch('builtins.input', return_value='n'):
            code, out = self.run_action('browse', 'kiwix', 'example')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state()[0]['id'], 'example|')
        self.assertEqual(self.downloaded, [])
        _, out = self.run_action('status')
        self.assertIn('ready to download', out)
        self.stdin_tty.return_value = False
        with patch.object(app.kiwix, 'resolve', return_value=resolved):
            code, out = self.run_action('sync')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.downloaded, ['example_2026-01.zim'])

    def test_browsed_survivor_selection_can_be_downloaded_later(self):
        self.stdin_tty.return_value = True
        result = {'id':'food', 'title':'Food', 'source_url':'https://www.survivorlibrary.com/index.php/library-food/'}
        archive = {'filename':'Food.ZIP', 'download_url':'https://www.survivorlibrary.com/library/Food.ZIP', 'size':1}
        with patch.object(app.survivor, 'discover_categories', return_value=[result]), \
             patch.object(app.survivor, 'category_contents', return_value=([archive], [])) as lookup, \
             patch.object(app, 'select', return_value=[result]), \
             patch('builtins.input', return_value='n'):
            code, out = self.run_action('browse', 'survivor', 'food')
            self.assertEqual(code, 0, out)
            lookup.assert_not_called()
            self.assertEqual(self.state()[0]['id'], 'food')
            self.assertEqual(self.downloaded, [])
            self.stdin_tty.return_value = False
            code, out = self.run_action('sync')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.downloaded, ['Food.ZIP'])

    def test_category_without_zip_downloads_individual_books(self):
        category = {'source':'survivor-category', 'id':'books', 'title':'Books',
                    'url':'https://www.survivorlibrary.com/index.php/library-books/'}
        book = {'filename':'one.pdf', 'download_url':'https://www.survivorlibrary.com/library/one.pdf',
                'size':None, 'kind':'pdf'}
        app.save([category])
        with patch.object(app.survivor, 'category_contents', return_value=([], [book])):
            code, out = self.run_action('download')
        self.assertEqual(code, 0, out)
        self.assertIn('No category ZIP; checking 1 individual book.', out)
        self.assertEqual(self.downloaded, ['one.pdf'])
        self.assertTrue((self.data/'survivor-library/books/one.pdf').exists())
        self.assertEqual(self.state()[0]['files'], ['survivor-library/books/one.pdf'])

    def test_cancelling_checklist_does_not_save(self):
        self.stdin_tty.return_value = True
        with patch.object(app.kiwix, 'search_page', return_value=([{'id':'x','title':'X'}], False)), \
             patch.object(app, 'select', return_value=[]):
            code, out = self.run_action('browse', 'kiwix', 'example')
        self.assertEqual(code, 0, out)
        self.assertIn('No downloads selected.', out)
        self.assertFalse((self.data/'selections.json').exists())

    def test_previewing_browse_does_not_save_selection(self):
        self.stdin_tty.return_value = True
        result = {'id':'example|', 'title':'Example', 'source_url':'https://example.org', 'size':1}
        resolved = {'filename':'example_2026-01.zim', 'download_url':'https://example.org/file.zim', 'size':1}
        with patch.object(app.kiwix, 'search_page', return_value=([result], False)), \
             patch.object(app.kiwix, 'resolve', return_value=resolved), \
             patch.object(app, 'select', return_value=[result]), \
             patch('builtins.input', return_value='y'):
            code, out = self.run_action('browse', 'kiwix', 'example', preview=True)
        self.assertEqual(code, 0, out)
        self.assertIn('Would download:', out)
        self.assertFalse(self.data.exists())
        self.assertEqual(self.downloaded, [])

    def test_status_reports_missing_selected_file_without_network(self):
        self.run_action('starter', 'survivor')
        (self.data/'survivor-library/zips/Canning.ZIP').unlink()
        with patch.object(app.survivor, 'discover_categories', side_effect=AssertionError('network used')):
            code, out = self.run_action('status')
        self.assertEqual(code, 0)
        self.assertIn('missing', out.lower())

    def test_empty_selected_file_is_reported_and_not_overwritten(self):
        self.run_action('starter', 'survivor')
        file = self.data/'survivor-library/zips/Canning.ZIP'
        file.write_bytes(b'')
        code, out = self.run_action('status')
        self.assertEqual(code, 0)
        self.assertIn('missing (0 of 1 files present)', out)
        self.assertIn('Empty or unusable file:', out)
        code, out = self.run_action('sync')
        self.assertEqual(code, 1)
        self.assertIn('empty or unusable', out)
        self.assertEqual(file.read_bytes(), b'')
        self.assertEqual(self.downloaded.count('Canning.ZIP'), 1)

    def test_empty_kiwix_file_is_not_treated_as_complete(self):
        file = self.data/'kiwix/example.zim'
        file.parent.mkdir(parents=True)
        file.touch()
        item = {'source':'kiwix', 'id':'example|', 'title':'Example', 'files':['kiwix/example.zim']}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            app.status([item])
        self.assertIn('missing (0 of 1 files present)', out.getvalue())
        with self.assertRaisesRegex(RuntimeError, 'empty or unusable'):
            app.process(item, [item])
        self.assertEqual(file.stat().st_size, 0)

    def test_status_on_new_library_does_not_create_directory(self):
        code, out = self.run_action('status')
        self.assertEqual(code, 0, out)
        self.assertIn('No selections saved', out)
        self.assertFalse(self.data.exists())

    def test_all_survivor_records_discovered_files_for_status_and_retry(self):
        import zipfile
        categories = [
            {'id':'food', 'title':'Food', 'source_url':'https://www.survivorlibrary.com/index.php/library-food/'},
            {'id':'water', 'title':'Water', 'source_url':'https://www.survivorlibrary.com/index.php/library-water/'},
        ]
        def category_contents(url):
            name = 'Food.ZIP' if 'food' in url else 'Water.ZIP'
            return ([{'filename':name, 'download_url':'https://www.survivorlibrary.com/library/'+name, 'size':None, 'kind':'zip'}], [])
        def transfer(url, target, kind, **kwargs):
            self.downloaded.append(target.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(target, 'w') as archive:
                archive.writestr('book.pdf', b'%PDF-1.4 fixture')
        self.stdin_tty.return_value = True
        with patch.object(app.survivor, 'discover_categories', return_value=categories), \
             patch.object(app.survivor, 'category_contents', side_effect=category_contents), \
             patch.object(app.survivor, 'recent_downloads', return_value=(180, [])), \
             patch.object(app.download, 'download', side_effect=transfer), \
             patch('builtins.input', side_effect=['y', 'y']):
            code, out = self.run_action('survivor', 'all')
        self.assertEqual(code, 0, out)
        self.assertTrue((self.data/'survivor-library/zips/Food.ZIP').exists())
        self.assertTrue((self.data/'survivor-library/zips/Water.ZIP').exists())
        recorded = self.state()[0].get('files', [])
        self.assertEqual(set(recorded), {'survivor-library/zips/Food.ZIP', 'survivor-library/zips/Water.ZIP'})
        (self.data/'survivor-library/zips/Food.ZIP').unlink()
        _, status = self.run_action('status')
        self.assertIn('missing', status.lower())
        self.stdin_tty.return_value = False
        with patch.object(app.survivor, 'discover_categories', return_value=categories), \
             patch.object(app.survivor, 'category_contents', side_effect=category_contents), \
             patch.object(app.survivor, 'recent_downloads', return_value=(180, [])), \
             patch.object(app.download, 'download', side_effect=transfer):
            code, out = self.run_action('sync')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.downloaded.count('Food.ZIP'), 2)
        self.assertEqual(self.downloaded.count('Water.ZIP'), 1)


class SizeTests(unittest.TestCase):
    def test_selection_sizes_use_saved_data_without_network(self):
        saved = {'survivor':[{'id':'food', 'size':300}], 'files':{},
                 'kiwix':{'example':{'size':100}}}
        with patch.object(app, 'current_catalog', return_value=saved), \
             patch.object(app.download, 'remote_size', side_effect=AssertionError('network used')), \
             patch.object(app.survivor, 'category_downloads', side_effect=AssertionError('network used')):
            self.assertEqual(app.selection_size({'source':'survivor-category', 'id':'food'}), 300)
            self.assertEqual(app.selection_size({'source':'kiwix', 'id':'example'}), 100)
            self.assertIsNone(app.selection_size({'source':'survivor-category', 'id':'unknown'}))

    def test_kiwix_browser_uses_cached_size_for_the_same_file(self):
        items = [{'id':'first|', 'download_url':'https://example.org/1.zim', 'size':10},
                 {'id':'second|', 'download_url':'https://example.org/2.zim', 'size':20}]
        saved = {'survivor':[], 'kiwix':{}, 'files':{
            'https://example.org/1.zim':{'size':123, 'checked_at':'2026-11-20T00:00:00+00:00'}}}
        with patch.object(app, 'current_catalog', return_value=saved), \
             patch.object(app.download, 'remote_size', side_effect=AssertionError('network used')):
            app.apply_cached_kiwix_sizes(items)
        self.assertEqual(items[0]['size'], 123)
        self.assertEqual(items[0]['checked_at'], '2026-11-20T00:00:00+00:00')
        self.assertEqual(items[1]['size'], 20)

    def test_starter_preview_keeps_complete_download_plans(self):
        for group, filename in [('survivor', 'Food.ZIP'),
                                ('last-light', 'last-light-survival-guide-standard.zip')]:
            with self.subTest(group=group), tempfile.TemporaryDirectory() as temp:
                data = Path(temp) / 'unused'
                url = app.LAST_URL if group == 'last-light' else 'https://www.survivorlibrary.com/library/Food.ZIP'
                saved = {'survivor':[], 'files':{url:{'size':56428559}}, 'kiwix':{}}
                with patch.object(app, 'DATA', data), patch.object(app, 'STATE', data/'selections.json'), \
                     patch.object(app, 'LOG', data/'log'), patch.object(app, 'PREVIEW', True), \
                     patch.object(app, 'ARGS', ['starter', group]), \
                     patch.object(app, 'STARTER_SURVIVOR', ['Food.ZIP']), \
                     patch.object(app.catalog, 'load', return_value=saved), \
                     patch.object(app.download, 'remote_size', side_effect=AssertionError('network used')), \
                     patch.object(app.sys.stdin, 'isatty', return_value=False), \
                     patch.object(app.download, 'download', side_effect=AssertionError('downloaded during preview')), \
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    code = app.main()
                self.assertEqual(code, 0, out.getvalue())
                self.assertIn('56.4 MB', out.getvalue())
                self.assertIn('Would download: ' + filename, out.getvalue())
                self.assertFalse(data.exists())


if __name__ == '__main__':
    unittest.main()
