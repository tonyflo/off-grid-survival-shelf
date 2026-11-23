import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sources import catalog


def category(name, size=None):
    item = {'id': name, 'title': name.title(),
            'source_url': 'https://www.survivorlibrary.com/index.php/library-' + name + '/'}
    if size is not None:
        item.update(size=size, checked_at='2026-01-01T00:00:00+00:00')
    return item


class SavedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root/'library'
        self.bundled = self.root/'bundled.json'
        self.base = {'survivor':[category('food', 100), category('shelter', 250)],
                     'files':{'https://example.org/shelter.zip':{'size':250}}, 'kiwix':{}}
        self.bundled.write_text(json.dumps(self.base))
        p = patch.object(catalog, 'BUNDLED', self.bundled); p.start(); self.addCleanup(p.stop)

    def refresh(self, group='survivor', state=None):
        output = []
        code = catalog.refresh(self.data, state or [], group, [], [],
                               'https://example.org/last.zip', output.append, output.append)
        return code, '\n'.join(output)

    def test_bundled_catalog_fallback_when_saved_catalog_is_invalid(self):
        self.data.mkdir()
        (self.data/catalog.CACHE_NAME).write_text('{broken')
        self.assertEqual(catalog.load(self.data)['survivor'], self.base['survivor'])
        self.assertEqual((self.data/catalog.CACHE_NAME).read_text(), '{broken')
        with self.assertRaisesRegex(RuntimeError, 'Move it aside'):
            self.refresh()
        self.assertEqual((self.data/catalog.CACHE_NAME).read_text(), '{broken')

    def test_refresh_saves_successes_and_keeps_sizes_for_failures(self):
        found = [category('food'), category('shelter')]
        def check(item):
            if item['id'] == 'food': return {'https://example.org/food.zip': 300}, 300, 'ok'
            return {'https://example.org/shelter.zip': None}, None, 'unavailable'
        with patch.object(catalog.survivor, 'discover_categories', return_value=found), \
             patch.object(catalog, '_check_category', side_effect=check):
            code, out = self.refresh()
        self.assertEqual(code, 1, out)
        saved = catalog.load(self.data)
        items = {item['id']: item for item in saved['survivor']}
        self.assertEqual(items['food']['size'], 300)
        self.assertEqual(items['shelter']['size'], 250)
        self.assertEqual(items['shelter']['checked_at'], '2026-01-01T00:00:00+00:00')
        self.assertEqual(catalog.file_size(saved, 'https://example.org/food.zip'), 300)
        self.assertEqual(catalog.file_size(saved, 'https://example.org/shelter.zip'), 250)
        self.assertIn('Survivor: Shelter', out)

    def test_incomplete_index_does_not_replace_saved_categories(self):
        self.base['survivor'] = [category('book' + str(i), 10) for i in range(12)]
        self.bundled.write_text(json.dumps(self.base))
        with patch.object(catalog.survivor, 'discover_categories', return_value=[category('food')]), \
             patch.object(catalog, '_check_category', side_effect=AssertionError('should not check')):
            code, out = self.refresh()
        self.assertEqual(code, 1, out)
        self.assertIn('incomplete category list', out)
        self.assertEqual(len(catalog.load(self.data)['survivor']), 12)

    def test_topic_refresh_checks_only_matches_using_saved_index(self):
        output = []
        with patch.object(catalog.survivor, 'discover_categories', side_effect=AssertionError('index fetched')), \
             patch.object(catalog, '_check_category', return_value=({'https://example.org/food.zip': 300}, 300, 'ok')) as check:
            code = catalog.refresh(self.data, [], 'survivor', [], [], 'https://example.org/last.zip',
                                   output.append, output.append, topic='food')
        self.assertEqual(code, 0, output)
        self.assertEqual(check.call_count, 1)
        items = {item['id']: item for item in catalog.load(self.data)['survivor']}
        self.assertEqual(items['food']['size'], 300)
        self.assertEqual(items['shelter']['size'], 250)

    def test_no_zip_is_reported_as_unavailable_not_a_failed_refresh(self):
        output = []
        error = catalog.survivor.NoCategoryZipError('No ZIP links found on Survivor Library page: example')
        with patch.object(catalog.survivor, 'category_downloads', side_effect=error):
            code = catalog.refresh(self.data, [], 'survivor', [], [], 'https://example.org/last.zip',
                                   output.append, output.append, topic='food')
        self.assertEqual(code, 0, output)
        item = next(c for c in catalog.load(self.data)['survivor'] if c['id'] == 'food')
        self.assertTrue(item['no_zip'])
        self.assertNotIn('size', item)
        self.assertIn('No category ZIP found for: Food', '\n'.join(output))

    def test_kiwix_refresh_covers_starter_and_saved_selections(self):
        state = [{'source':'kiwix', 'id':'saved|', 'title':'Saved'}]
        def check(item):
            return {'download_url':'https://example.org/' + item['id'] + '.zim'}, 123
        output = []
        with patch.object(catalog, '_check_kiwix', side_effect=check):
            code = catalog.refresh(self.data, state, 'kiwix', ['starter'], [],
                                   'https://example.org/last.zip', output.append, output.append)
        self.assertEqual(code, 0, output)
        saved = catalog.load(self.data)
        self.assertEqual(saved['kiwix']['starter']['size'], 123)
        self.assertEqual(saved['kiwix']['saved|']['size'], 123)
        self.assertEqual(state, [{'source':'kiwix', 'id':'saved|', 'title':'Saved'}])

    def test_kiwix_size_lookup_retries_a_temporary_failure(self):
        item = {'id':'starter', 'prefix':True}
        entry = {'download_url':'https://example.org/starter.zim'}
        with patch.object(catalog.kiwix, 'resolve_prefix', return_value=entry) as resolve, \
             patch.object(catalog.download, 'remote_size', side_effect=[None, 123]) as size:
            self.assertEqual(catalog._check_kiwix(item), (entry, 123))
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(size.call_count, 2)

    def test_survivor_refresh_checks_starter_files_missing_from_categories(self):
        output = []
        with patch.object(catalog.survivor, 'discover_categories', return_value=[category('food')]), \
             patch.object(catalog, '_check_category', return_value=({'https://example.org/food.zip': 300}, 300, 'ok')), \
             patch.object(catalog.download, 'remote_size', return_value=456):
            code = catalog.refresh(self.data, [], 'survivor', [], ['Canning.ZIP'],
                                   'https://example.org/last.zip', output.append, output.append)
        self.assertEqual(code, 0, output)
        saved = catalog.load(self.data)
        self.assertEqual(catalog.file_size(saved, 'https://www.survivorlibrary.com/library/Canning.ZIP'), 456)

    def test_last_light_failure_keeps_previous_size(self):
        self.base['files']['https://example.org/last.zip'] = {'size': 456, 'checked_at':'old'}
        self.bundled.write_text(json.dumps(self.base))
        with patch.object(catalog.download, 'remote_size', return_value=None):
            code, out = self.refresh('last-light')
        self.assertEqual(code, 1, out)
        self.assertEqual(catalog.file_size(catalog.load(self.data), 'https://example.org/last.zip'), 456)


if __name__ == '__main__': unittest.main()
