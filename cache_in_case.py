#!/usr/bin/env python3
"""Download a selected offline library without installing a server."""
import json
import fcntl
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from datetime import datetime
from sources import catalog, kiwix, survivor, download

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'config.json'
LEGACY_LOCATION = ROOT / '.cache-in-case-location'
DEFAULT_LIBRARY = 'offline-library'
config_error = None
try:
    if CONFIG.exists():
        config = json.loads(CONFIG.read_text(encoding='utf-8'))
        if not isinstance(config, dict) or not isinstance(config.get('library_path'), str) or not config['library_path'].strip():
            raise ValueError('library_path must be a non-empty path')
        configured_location = config['library_path'].strip()
    elif LEGACY_LOCATION.exists():
        configured_location = LEGACY_LOCATION.read_text(encoding='utf-8').strip() or DEFAULT_LIBRARY
    elif (ROOT / 'data').exists() and not (ROOT / DEFAULT_LIBRARY).exists():
        configured_location = 'data'
    else:
        configured_location = DEFAULT_LIBRARY
except (OSError, ValueError) as exc:
    config_error = 'Cannot read {}: {}'.format(CONFIG, exc)
    configured_location = DEFAULT_LIBRARY
location = Path(os.environ.get('CACHE_DATA') or configured_location).expanduser()
DATA = (location if location.is_absolute() else ROOT / location).absolute()
STATE = DATA / 'selections.json'
LOG = DATA / 'cache-in-case.log'
PREVIEW = '--preview' in sys.argv
ARGS = [x for x in sys.argv[1:] if x != '--preview']
STARTER_KIWIX = ['wikipedia_en_medicine_maxi', 'ready.gov_en_all', 'medlineplus.gov_en_all', 'nhs.uk_en_medicines', 'cd3wdproject.org_en_all', 'solar.lowtechmagazine.com_mul_all', 'based.cooking_en_all']
STARTER_KIWIX_TOPICS = {
    'medical': ['wikipedia_en_medicine_maxi', 'medlineplus.gov_en_all', 'nhs.uk_en_medicines'],
    'preparedness': ['ready.gov_en_all', 'cd3wdproject.org_en_all', 'solar.lowtechmagazine.com_mul_all'],
    'food': ['based.cooking_en_all'],
}
STARTER_KIWIX_TITLES = {
    'wikipedia_en_medicine_maxi': 'Wikipedia: Medicine',
    'ready.gov_en_all': 'Ready.gov: Emergency Preparedness',
    'medlineplus.gov_en_all': 'MedlinePlus',
    'nhs.uk_en_medicines': 'NHS: Medicines',
    'cd3wdproject.org_en_all': 'CD3WD: Practical Skills',
    'solar.lowtechmagazine.com_mul_all': 'Low-tech Magazine',
    'based.cooking_en_all': 'Based Cooking: Recipes',
}
STARTER_SURVIVOR = ['Canning.ZIP', 'Food.ZIP', 'Shelter.ZIP', 'Medical_Emergency.ZIP', 'Survival_Individual.ZIP']
LAST_URL = 'https://bunkerbot.co.uk/downloads/last-light-survival-guide-standard.zip'


_progress_width = 0
_last_progress_log = 0


def say(text='', *, update=False, log=True):
    global _progress_width
    if update and sys.stdout.isatty():
        message = str(text)
        padding = ' ' * max(0, _progress_width - len(message))
        print('\r' + message + padding, end='', flush=True)
        _progress_width = len(message)
    else:
        if _progress_width:
            print(flush=True)
            _progress_width = 0
        print(text, flush=True)
    if not PREVIEW and log:
        with LOG.open('a', encoding='utf-8') as f:
            f.write(str(text) + '\n')


def show_progress(text):
    global _last_progress_log
    now = time.monotonic()
    log = now - _last_progress_log >= 5 or text.endswith('(100%)')
    say(text, update=True, log=log)
    if log: _last_progress_log = now


def size_label(item):
    size = item.get('size')
    if type(size) is not int or size <= 0:
        return 'size unknown'
    unit, divisor = ('GB', 1e9) if size >= 1e9 else ('MB', 1e6)
    return '{}{:.1f} {}'.format('~' if item.get('size_approx') else '', size / divisor, unit)


_catalog_cache = None
_catalog_dir = None


def current_catalog():
    global _catalog_cache, _catalog_dir
    if _catalog_cache is None or _catalog_dir != DATA:
        _catalog_cache = catalog.load(DATA)
        _catalog_dir = DATA
    return _catalog_cache


def selection_size(item):
    """Read a previously checked size without making a network request."""
    data = current_catalog()
    source = item['source']
    if source == 'kiwix':
        entry = data['kiwix'].get(item['id'], {})
        size = entry.get('size') if isinstance(entry, dict) else None
        return size if type(size) is int and size > 0 else item.get('size')
    if source == 'survivor-category':
        size = next((c.get('size') for c in data['survivor'] if c['id'] == item['id']), None)
        return size if type(size) is int and size > 0 else item.get('size')
    if source == 'survivor-file':
        return catalog.file_size(data, 'https://www.survivorlibrary.com/library/' + item['id']) or item.get('size')
    if source == 'last-light':
        return catalog.file_size(data, LAST_URL) or item.get('size')
    return None


def apply_cached_kiwix_sizes(items):
    data = current_catalog()
    for item in items:
        url = item.get('download_url')
        size = catalog.file_size(data, url)
        if size is not None:
            item['size'], item['size_approx'] = size, True
            entry = data['files'].get(url, {})
            if isinstance(entry.get('checked_at'), str):
                item['checked_at'] = entry['checked_at']


def show_selection_sizes(items, known_sizes):
    print('\nSaved download sizes (may have changed):')
    sizes = []
    for item in items:
        value = known_sizes.get((item['source'], item['id']))
        if value is None: value = selection_size(item)
        sizes.append(value)
        print('  {}: {}'.format(item['title'], size_label({'size': value, 'size_approx': True})))
    if len(items) > 1:
        if all(size is not None for size in sizes):
            print('  Total selected: ' + size_label({'size': sum(sizes), 'size_approx': True}))
        else:
            print('  Total selected: size unavailable for some items')
    if any(size is None for size in sizes):
        print('  Run bash setup.sh update to check missing sizes.')


def show_full_library_estimate(source, category=None):
    """Estimate whole-library sizes before a user selects a full catalog.

    Kiwix publishes sizes in its catalog. Survivor's saved category sizes are
    faster to use than checking hundreds of pages and files on each run.
    Neither is a remaining-download estimate: installed files are not deducted.
    """
    if source == 'survivor':
        categories = current_catalog()['survivor']
        sizes = [c['size'] for c in categories
                 if type(c.get('size')) is int and c['size'] > 0]
        if sizes:
            label = size_label({'size': sum(sizes), 'size_approx': True})
            print('Estimated category ZIPs: {} ({} of {} categories have sizes).'.format(
                label, len(sizes), len(categories)))
            print('Based on saved sizes; some ZIPs may be counted more than once.')
        else:
            print('Estimated category ZIP size: unavailable. Run bash setup.sh update survivor.')
        print('Additional individual books and any categories without sizes are not included.')
    else:
        print('Checking the Kiwix catalog for an estimated size...')
        try:
            entries, unreadable = [], 0
            pages = kiwix.catalog_pages(category=category) if category else kiwix.catalog_pages()
            for page, _, _ in pages:
                for node in page:
                    entry = kiwix._item(node)
                    if entry is None: unreadable += 1
                    elif category and entry.get('category', '').casefold() != category.casefold():
                        raise ValueError('Kiwix returned an item outside the selected category')
                    else: entries.append(entry)
            if category and not entries and not unreadable:
                print('No English downloads found in this Kiwix category.')
                return False
            if not category:
                entries.extend(kiwix._maps(kiwix._fetch))
            # Match the downloader's one latest edition per series and flavour,
            # and count a file only once if it is also listed in the map index.
            latest = {}
            for entry in entries:
                old = latest.get(entry['id'])
                if old is None or (entry.get('updated', ''), entry['filename']) > (old.get('updated', ''), old['filename']):
                    latest[entry['id']] = entry
            files = {entry['filename']: entry for entry in latest.values()}
            sizes = [entry['size'] for entry in files.values()
                     if type(entry.get('size')) is int and entry['size'] > 0]
            unknown = len(files) - len(sizes) + unreadable
            if sizes:
                label = size_label({'size': sum(sizes), 'size_approx': True})
                scope = 'Kiwix category' if category else 'Kiwix catalog and maps'
                print('Estimated {}: {}{}'.format(scope,
                    label, ' + {} {} with unknown sizes'.format(
                        unknown, 'file' if unknown == 1 else 'files') if unknown else ''))
            else:
                print('Estimated Kiwix {} size: unavailable.'.format('category' if category else 'catalog and maps'))
        except Exception as exc:
            print('Estimated Kiwix catalog size: unavailable ({}).'.format(exc))
    print('Existing downloads are not deducted from these estimates.')


def choose_kiwix_category(requested=None):
    """Choose a publisher category; None means the unfiltered catalog."""
    if requested is not None and requested.casefold() in ('', 'all'):
        return None
    categories = kiwix.categories()
    if requested is None:
        print('\nKiwix categories (provided by Kiwix):')
        for category in categories:
            name = category['title']
            if name.casefold() != category['id'].casefold():
                name += ' ({})'.format(category['id'])
            print('  ' + name)
        print('  all - includes uncategorized content and separately indexed maps')
        print('A category contains only items labeled with that category by Kiwix.')
        requested = input('Choose a category by name (Enter for all; q to cancel): ').strip()
    if requested.casefold() in ('q', 'quit'):
        return False
    if requested.casefold() in ('', 'all'):
        return None
    for category in categories:
        if requested.casefold() in (category['id'].casefold(), category['title'].casefold()):
            return category['id']
    raise ValueError('Unknown Kiwix category: {}. Run the command again to see the list.'.format(requested))


def read_state():
    if not STATE.exists(): return []
    try:
        data = json.loads(STATE.read_text(encoding='utf-8'))
        if not isinstance(data, list): raise ValueError('not a list')
        return data
    except (ValueError, OSError) as e:
        raise RuntimeError('Cannot read saved selections: {}'.format(e))


def save(state):
    if PREVIEW: return
    DATA.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix='.selections-', dir=DATA)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
            f.write('\n')
        os.replace(path, STATE)
    finally:
        if os.path.exists(path): os.unlink(path)


def remember(state, items):
    for item in items:
        existing = next((i for i in state if i['source'] == item['source'] and i['id'] == item['id']), None)
        if existing is None:
            state.append(item)
        elif type(item.get('size')) is int and item['size'] > 0:
            existing['size'] = item['size']
    save(state)


def confirm(prompt):
    try: return input(prompt + ' [y/N] ').strip().lower() in ('y', 'yes')
    except EOFError: return False


def usable_file(path):
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def file_size(path):
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def check_existing(path):
    if (path.exists() or path.is_symlink()) and not usable_file(path):
        raise RuntimeError('Existing file is empty or unusable: {}. Move it aside, then retry.'.format(path))


def select(items, source_name='Content'):
    if not items:
        say('No matching results.'); return []
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError('Browsing needs an interactive terminal.')
    import curses
    def run(screen):
        try: curses.curs_set(0)
        except curses.error: pass
        screen.keypad(True)
        # The catalog can list multiple dated editions of the same selection.
        unique = {}
        for item in items:
            old = unique.get(item['id'])
            if old is None or (item.get('updated', ''), item.get('filename', '')) > (old.get('updated', ''), old.get('filename', '')):
                unique[item['id']] = item
        items[:] = unique.values()
        items.sort(key=lambda item: item['title'].casefold())
        pos = 0
        chosen = set()
        while True:
            height, width = screen.getmaxyx()
            if height < 10 or width < 38:
                screen.erase(); screen.addnstr(0, 0, 'Make the terminal larger. Press q to cancel.', max(0, width-1)); screen.refresh()
                if screen.getch() in (ord('q'), 27): return []
                continue
            visible = height - 8
            start = max(0, min(pos - visible + 1, len(items) - visible))
            screen.erase()
            screen.addnstr(0, 0, 'Browse {}  |  {} results  |  {} selected'.format(source_name, len(items), len(chosen)), width-1, curses.A_BOLD)
            action = 'preview' if PREVIEW else 'save choices'
            screen.addnstr(1, 0, 'Up/down: move  Space: select  Enter: {}  q: cancel'.format(action), width-1)
            screen.addnstr(2, 0, 'PgUp/PgDn: page', width-1)
            for row, idx in enumerate(range(start, min(len(items), start+visible)), 3):
                item = items[idx]
                text = ('[x] ' if item['id'] in chosen else '[ ] ') + item['title']
                size = item.get('size')
                if item.get('no_zip'): text += '  (individual books)'
                elif size: text += '  ({})'.format(size_label(item))
                screen.addnstr(row, 0, text, width-1, curses.A_REVERSE if idx == pos else 0)
            if items:
                item = items[pos]
                screen.addnstr(height-4, 0, str(item.get('description') or ''), width-1)
                size_text = 'unknown (individual books, possibly many)' if item.get('no_zip') else (size_label(item) if item.get('size') else 'unknown')
                if not item.get('size') and not item.get('no_zip') and source_name == 'Survivor Library':
                    size_text += ' (run update survivor)'
                if item.get('checked_at'): size_text += ' (checked ' + item['checked_at'][:10] + ')'
                screen.addnstr(height-3, 0, 'Size: ' + size_text, width-1)
                screen.addnstr(height-2, 0, 'Details: ' + item.get('source_url', ''), width-1)
            screen.refresh()
            key = screen.getch()
            if key in (ord('q'), 27): return []
            if key == curses.KEY_UP and items: pos = max(0, pos-1)
            elif key == curses.KEY_DOWN and items: pos = min(len(items)-1, pos+1)
            elif key == curses.KEY_PPAGE and items: pos = max(0, pos-visible)
            elif key == curses.KEY_NPAGE and items: pos = min(len(items)-1, pos+visible)
            elif key == ord(' ') and items:
                item = items[pos]
                item_id = item['id']
                if item_id in chosen:
                    chosen.remove(item_id)
                else:
                    chosen.add(item_id)
            elif key in (10, 13, curses.KEY_ENTER): return [item for item in items if item['id'] in chosen]
    return curses.wrapper(run)


def folder_size(folder):
    """Count file sizes without following symlinks outside the library."""
    if not folder.exists(): return 0
    total, errors = 0, []
    for root, dirs, files in os.walk(folder, onerror=errors.append, followlinks=False):
        for name in files:
            path = Path(root) / name
            try:
                if not path.is_symlink(): total += path.stat().st_size
            except OSError as exc:
                errors.append(exc)
    return None if errors else total


def status(state):
    print('Library:', DATA)
    if not state: print('No selections saved.'); return
    waiting_sizes = []
    groups = [('Kiwix', 'kiwix'), ('Survivor Library', 'survivor'),
              ('Last Light', 'last-light'), ('Other', None)]
    for heading, source in groups:
        items = [item for item in state if
                 (item['source'].startswith(source) if source is not None else
                  not item['source'].startswith(('kiwix', 'survivor', 'last-light')))]
        if not items: continue
        print('\n' + heading)
        for item in items:
            files = item.get('files', [])
            present = sum(usable_file(DATA / p) for p in files)
            downloaded_size = sum(file_size(DATA / p) for p in set(files))
            pending = False
            if item['source'] == 'last-light':
                site = DATA / 'last-light/site'
                if (site / '.cache-in-case-installing').exists():
                    label = 'extraction incomplete'
                elif (site / 'index.html').exists() and not usable_file(site / 'index.html'):
                    label = 'missing or empty'
                else:
                    label = 'downloaded' if usable_file(site / 'index.html') else 'ready to download'
                installed = usable_file(site / 'index.html') and not (site / '.cache-in-case-installing').exists()
                pending = not installed
                archive = DATA / 'last-light/last-light-survival-guide-standard.zip'
                if pending and usable_file(archive):
                    if label == 'ready to download': label = 'ready to extract'
                    label += ' (ZIP already downloaded)'
                    pending = False
                if installed:
                    if downloaded_size:
                        label += ' (ZIP {})'.format(size_label({'size': downloaded_size}))
                    else:
                        site_size = folder_size(site)
                        if site_size: label += ' ({} on disk)'.format(size_label({'size': site_size}))
                if usable_file(DATA / 'last-light/site-update/index.html') and not (DATA / 'last-light/site-update/.cache-in-case-installing').exists():
                    label += ' (update staged)'
            elif not files:
                label = 'ready to download'
                pending = True
            else:
                if present == len(files):
                    label = 'downloaded' if len(files) == 1 else 'downloaded ({} files)'.format(len(files))
                    if downloaded_size: label += ' ({})'.format(size_label({'size': downloaded_size}))
                else:
                    label = 'missing ({} of {} files present)'.format(present, len(files))
                    pending = True
            if pending:
                estimated = selection_size(item) if not files or present == 0 or len(files) == 1 else None
                if type(estimated) is not int or estimated <= 0: estimated = None
                waiting_sizes.append(estimated)
                label += ' (estimated {})'.format(size_label({'size': estimated, 'size_approx': True}))
            print('  {}: {}'.format(item['title'], label))
            for p in files:
                path = DATA / p
                if (path.exists() or path.is_symlink()) and not usable_file(path):
                    print('    Empty or unusable file: {}'.format(path))
            if item.get('error'): print('    Last error:', item['error'])
    used = folder_size(DATA)
    print('\nLibrary folder size: {}'.format(size_label({'size': used}) if used else ('0 MB' if used == 0 else 'unavailable')))
    if waiting_sizes:
        known = sum(value for value in waiting_sizes if value is not None)
        unknown = sum(value is None for value in waiting_sizes)
        label = size_label({'size': known, 'size_approx': True}) if known else 'unavailable'
        if known and unknown: label += ' + unknown sizes'
        print('Estimated downloads remaining: ' + label)


def plan_files(item):
    source = item['source']
    if source == 'kiwix':
        found = kiwix.resolve_prefix(item['id']) if item.get('prefix') else kiwix.resolve(item['id'])
        return [found]
    if source == 'survivor-category':
        zips, books = survivor.category_contents(item['url'])
        return zips or books
    if source == 'survivor-file':
        return [{'filename': item['id'], 'download_url': 'https://www.survivorlibrary.com/library/' + item['id'], 'size': None}]
    if source == 'last-light': return [{'filename': 'last-light-survival-guide-standard.zip', 'download_url': LAST_URL, 'size': None}]
    raise ValueError('Unknown source: ' + source)


def target_path(item, entry):
    source = item['source']
    if source == 'kiwix': folder = 'kiwix'
    elif source == 'last-light': folder = 'last-light'
    elif entry.get('kind') in ('pdf', 'epub'): folder = 'survivor-library/books'
    else: folder = 'survivor-library/zips'
    return DATA / folder / entry['filename']


def process_survivor_all(item, state):
    """Download indexed ZIPs, followed by books not found inside those ZIPs."""
    errors, zips, books, old_links = [], {}, {}, set()
    old_paths = list(item.get('files', []))
    tracked = [dict(c) for c in item.get('categories', [])]
    by_id = {c['id'].casefold(): c for c in tracked}

    def problem(message):
        errors.append(message)
        say('FAILED: ' + message)

    def add_file(entry, previous=False):
        name = entry['filename']
        kind = entry.get('kind') or name.rsplit('.', 1)[-1].lower()
        if (kind not in ('zip', 'pdf', 'epub') or name in ('', '.', '..') or
                '/' in name or '\\' in name or any(ord(c) < 32 for c in name)):
            raise ValueError('Invalid Survivor filename: ' + name)
        target = zips if kind == 'zip' else books
        key = name.casefold()
        link_key = (kind == 'zip', key)
        if (key in target and target[key]['download_url'] != entry['download_url']
                and link_key not in old_links):
            raise ValueError('Multiple Survivor files have the same name: ' + name)
        target[key] = dict(entry, kind=kind)
        if previous:
            old_links.add(link_key)
        else:
            old_links.discard(link_key)

    def relative(entry):
        folder = 'zips' if entry['kind'] == 'zip' else 'books'
        return 'survivor-library/{}/{}'.format(folder, entry['filename'])

    # Remember previously discovered URLs so a retry still works if a publisher
    # removes a link after an interrupted download.
    for entry in item.get('known_files', []):
        add_file(entry, previous=True)
    categories = survivor.discover_categories()
    say('Discovered {} Survivor Library categories.'.format(len(categories)))
    for category in categories:
        key = category['id'].casefold()
        if key not in by_id:
            by_id[key] = {'source': 'survivor-category', 'id': category['id'],
                          'title': category['title'], 'url': category['source_url']}
            tracked.append(by_id[key])
        else:
            by_id[key]['title'], by_id[key]['url'] = category['title'], category['source_url']

    for sub in tracked:
        say('Checking category: ' + sub['title'])
        try:
            category_zips, category_books = survivor.category_contents(sub['url'])
            for entry in category_zips + category_books:
                add_file(entry)
            # Preserve older selections when the publisher changes its page.
            sub['files'] = list(dict.fromkeys(sub.get('files', []) +
                                              [relative(f) for f in category_zips]))
            sub.pop('error', None)
            sub.pop('no_zip', None)
        except Exception as exc:
            problem('{}: {}'.format(sub['title'], exc))
            sub['error'] = str(exc)

    try:
        days, recent = survivor.recent_downloads()
        say('Checking the publisher\'s last {} days of books.'.format(days))
        for entry in recent:
            add_file(entry)
    except Exception as exc:
        problem('Recent-books index: {}'.format(exc))

    old_paths += [p for category in tracked for p in category.get('files', [])]
    zip_entries = sorted(zips.values(), key=lambda f: f['filename'].casefold())
    zip_paths = [relative(entry) for entry in zip_entries]
    paths = list(dict.fromkeys(old_paths + zip_paths))
    if not PREVIEW:
        item['categories'] = tracked
        item['files'] = paths
        item['known_files'] = zip_entries + [f for f in item.get('known_files', [])
                                               if f.get('kind') in ('pdf', 'epub')]
        save(state)

    incomplete_zips = False
    for entry in zip_entries:
        target = DATA / relative(entry)
        try:
            check_existing(target)
            if usable_file(target):
                say('Already present: ' + entry['filename'])
            elif PREVIEW:
                incomplete_zips = True
                say('Would download: {} ({})'.format(entry['filename'], size_label(entry)))
            else:
                say('Download: {} ({})'.format(entry['filename'], size_label(entry)))
                download.download(entry['download_url'], target, 'zip', progress=say,
                                  progress_update=show_progress)
        except Exception as exc:
            incomplete_zips = True
            problem('{}: {}'.format(entry['filename'], exc))
            if isinstance(exc, download.InsufficientSpaceError):
                break

    # Include ZIPs from older selections, even when no longer linked.
    archived_books = set()
    for rel in paths:
        if not rel.lower().endswith('.zip'):
            continue
        path = DATA / rel
        if not usable_file(path):
            if rel not in zip_paths:
                problem('Previously selected ZIP is missing and no longer linked: ' + rel)
            incomplete_zips = True
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                for member in archive.infolist():
                    name = member.filename.replace('\\', '/').rsplit('/', 1)[-1]
                    if name.lower().endswith(('.pdf', '.epub')):
                        archived_books.add(name.casefold())
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            incomplete_zips = True
            problem('Cannot read ZIP {}: {}. Move it aside and retry.'.format(path, exc))

    if incomplete_zips:
        if PREVIEW and not errors:
            say('After downloading ZIPs, up to {} individual books may also be downloaded.'.format(len(books)))
        else:
            say('Individual books will be checked after the ZIPs are available.')
    else:
        # Previously selected loose books remain selected even if a ZIP now
        # includes a file with the same name.
        prior_books = {p.casefold() for p in old_paths if '/books/' in p}
        loose = [f for f in books.values() if f['filename'].casefold() not in archived_books
                 or relative(f).casefold() in prior_books]
        loose.sort(key=lambda f: f['filename'].casefold())
        say('{} individual books are not covered by the downloaded ZIPs.'.format(len(loose)))
        paths = list(dict.fromkeys(paths + [relative(f) for f in loose]))
        if not PREVIEW:
            item['files'] = paths
            item['known_files'] = zip_entries + loose
            save(state)
        for entry in loose:
            target = DATA / relative(entry)
            try:
                check_existing(target)
                if usable_file(target):
                    continue
                action = 'Would download' if PREVIEW else 'Download'
                say('{}: {} ({})'.format(action, entry['filename'], size_label(entry)))
                if not PREVIEW:
                    download.download(entry['download_url'], target, entry['kind'], progress=say,
                                      progress_update=show_progress)
            except Exception as exc:
                problem('{}: {}'.format(entry['filename'], exc))
                if isinstance(exc, download.InsufficientSpaceError):
                    break

    if errors:
        raise RuntimeError('{} Survivor error(s): {}'.format(len(errors), '; '.join(errors[:5])))


def process(item, state, update=False, planned_files=None):
    if update and (item['source'].startswith('survivor') or item['source'] == 'kiwix-all'):
        if not any(usable_file(DATA / path) for path in item.get('files', [])):
            say('Skip upgrade (not downloaded): ' + item['title'])
            return 'skipped'
    if update and item['source'].startswith('survivor'):
        say('Survivor ZIPs keep the same names, so newer versions cannot be detected.')
        say('Run download to get missing Survivor files.')
        return 'skipped'
    if item['source'] == 'kiwix':
        for p in item.get('files', []): check_existing(DATA / p)
    if item['source'] == 'kiwix' and update and (not item.get('files') or not any(usable_file(DATA / p) for p in item['files'])):
        say('Skip upgrade (not downloaded): ' + item['title'])
        return 'skipped'
    if item['source'] == 'kiwix' and not update and item.get('files') and all(usable_file(DATA / p) for p in item['files']):
        say('Already present: ' + item['title'])
        return
    if item['source'] == 'last-light' and update:
        site = DATA / 'last-light/site'
        check_existing(site / 'index.html')
        if not (site / 'index.html').is_file() or (site / '.cache-in-case-installing').exists():
            say('Skip upgrade (Last Light is not fully downloaded). Run download first.')
            return 'skipped'
        staged = DATA / 'last-light/site-update'
        archive = DATA / 'last-light/last-light-update.zip'
        if staged.is_dir() and (staged / 'index.html').is_file() and not (staged / '.cache-in-case-installing').exists():
            say('Last Light update is already staged at: ' + str(staged))
            return
        if (staged.exists() or staged.is_symlink()) and not (staged / '.cache-in-case-installing').exists():
            raise RuntimeError('Update destination already exists: ' + str(staged))
        current = DATA / 'last-light/last-light-survival-guide-standard.zip'
        previous = item.get('last_light_source', {})
        if not isinstance(previous, dict): previous = {}
        latest = download.remote_metadata(LAST_URL)
        if latest is None:
            raise RuntimeError('Could not check the Last Light publisher. Try again later.')
        if usable_file(current) and latest.get('size') and current.stat().st_size != latest['size']:
            changed = True
        elif previous.get('etag') and latest.get('etag'):
            changed = previous['etag'] != latest['etag']
        elif previous.get('last_modified') and latest.get('last_modified'):
            changed = previous['last_modified'] != latest['last_modified']
        else:
            changed = None
        if changed is False:
            say('Last Light is already up to date.')
            return
        if changed is None:
            say('Cannot tell whether Last Light has changed since it was downloaded.')
            if PREVIEW:
                say('Would ask before downloading a copy to compare.')
                return 'skipped'
            if not sys.stdin.isatty() or not confirm('Download a copy anyway?'):
                say('No Last Light download started.')
                return 'skipped'
        else:
            say('The publisher\'s Last Light package has changed.')
        if PREVIEW:
            say(('Would use existing update ZIP: ' if archive.exists() else 'Would download current Last Light package: ') + str(archive))
            say('Would stage Last Light at: ' + str(staged))
        else:
            if archive.exists(): say('Using previously downloaded update ZIP: ' + str(archive))
            download.download(LAST_URL, archive, 'zip', progress=say, progress_update=show_progress)
            download.install_last_light(archive, staged)
            say('Last Light package staged at: ' + str(staged))
            say('Your current guide remains in: ' + str(site))
        return
    if item['source'] == 'last-light':
        check_existing(DATA / 'last-light/site/index.html')
    if item['source'] == 'last-light' and usable_file(DATA / 'last-light/site/index.html') and not (DATA / 'last-light/site/.cache-in-case-installing').exists():
        say('Already present: Last Light')
        return
    if item['source'] == 'survivor-all':
        return process_survivor_all(item, state)
    if item['source'] == 'kiwix-all':
        # Store the files as they are discovered, so a stopped run can be
        # resumed and a later status check can identify missing downloads.
        tracked = [dict(entry) for entry in item.get('entries', [])]
        by_id = {entry['id']: index for index, entry in enumerate(tracked)}
        installed = {entry['id'] for entry in tracked
                     if usable_file(DATA / 'kiwix' / entry['filename'])} if update else set()
        saved_entries = [dict(entry) for entry in tracked]
        errors = []

        def record(entry):
            if update and entry['id'] not in installed:
                return
            existing = by_id.get(entry['id'])
            if existing is None:
                by_id[entry['id']] = len(tracked)
                tracked.append(entry)
            else:
                old = tracked[existing]
                old_path = DATA / 'kiwix' / old['filename']
                # A normal sync keeps an installed edition. If it was deleted,
                # choose the newest discovered edition for the retry.
                if (update or not old_path.is_file()) and (entry.get('updated', ''), entry['filename']) > (old.get('updated', ''), old['filename']):
                    tracked[existing] = entry

        def persist():
            if not PREVIEW and not update:
                item['entries'] = tracked
                item['files'] = list(dict.fromkeys('kiwix/' + entry['filename'] for entry in tracked))
                save(state)

        category = item.get('category')
        try:
            pages = kiwix.catalog_pages(category=category) if category else kiwix.catalog_pages()
            found_page = False
            for page, start, total in pages:
                found_page = True
                for node in page:
                    entry = kiwix._item(node)
                    if entry is None:
                        errors.append('A Kiwix catalog entry has no usable ZIM download')
                    elif category and entry.get('category', '').casefold() != category.casefold():
                        errors.append('Kiwix returned an item outside the selected category')
                    else:
                        record(entry)
                persist()
                say('Found Kiwix catalog entries: {} of {}'.format(start, total))
            if category and not found_page:
                raise RuntimeError('No English downloads found in Kiwix category: ' + category)
        except Exception as exc:
            errors.append('Kiwix catalog discovery: ' + str(exc))
        if not category:
            try:
                for entry in kiwix._maps(kiwix._fetch): record(entry)
                persist()
            except Exception as exc:
                errors.append('Kiwix maps discovery: ' + str(exc))

        seen_files = set()
        for entry in tracked:
            if update and entry['id'] not in installed: continue
            filename = entry['filename']
            if filename in seen_files: continue
            seen_files.add(filename)
            target = DATA / 'kiwix' / filename
            try: check_existing(target)
            except RuntimeError as exc:
                errors.append(str(exc))
                say('FAILED: ' + str(exc))
                continue
            action = 'Already present' if usable_file(target) else ('Would download' if PREVIEW else 'Download')
            size = {'size': target.stat().st_size} if usable_file(target) else entry
            say('{}: {} ({})'.format(action, filename, size_label(size)))
            if not usable_file(target) and not PREVIEW:
                try: download.download(entry['download_url'], target, 'zim', progress=say, progress_update=show_progress)
                except Exception as exc:
                    errors.append('{}: {}'.format(filename, exc))
                    say('FAILED: {}: {}'.format(filename, exc))
            if update and not PREVIEW and usable_file(target):
                saved_entries[by_id[entry['id']]] = entry
                item['entries'] = saved_entries
                item['files'] = list(dict.fromkeys('kiwix/' + e['filename'] for e in saved_entries))
                save(state)
        if errors:
            raise RuntimeError('{} Kiwix discovery or download error(s): {}'.format(len(errors), '; '.join(errors[:5])))
        return
    files = planned_files if planned_files is not None else plan_files(item)
    if item['source'] == 'survivor-category' and files and files[0].get('kind') in ('pdf', 'epub'):
        say('No category ZIP; {} {} individual {}.'.format(
            'would check' if PREVIEW else 'checking', len(files),
            'book' if len(files) == 1 else 'books'))
    if update and item['source'] == 'kiwix' and len(files) == 1:
        # Don't replace a newer installed edition with an older catalog result.
        def edition_date(name):
            match = re.search(r'_(20\d\d-\d\d[a-z]?)\.zim$', name)
            return match.group(1) if match else ''
        installed = [edition_date(p) for p in item.get('files', []) if (DATA / p).is_file()]
        offered = edition_date(files[0]['filename'])
        if offered and any(date and date > offered for date in installed):
            say('Already have a newer edition: ' + item['title'])
            return
    targets = []
    for f in files:
        target = target_path(item, f)
        check_existing(target)
        targets.append(str(target.relative_to(DATA)))
        if target.is_file():
            size = {'size': target.stat().st_size}
        else:
            saved = catalog.file_size(current_catalog(), f['download_url'])
            size = {'size': saved, 'size_approx': True} if saved is not None else f
        action = 'Already present' if target.exists() else ('Would download' if PREVIEW else 'Download')
        say('{}: {} ({})'.format(action, f['filename'], size_label(size)))
    if not PREVIEW and not (update and item['source'] == 'kiwix'):
        item['files'] = targets
        save(state)
    for f, rel in zip(files, targets):
        target = DATA / rel
        if not target.exists() and not PREVIEW:
            kind = 'zim' if item['source'] == 'kiwix' else f.get('kind', 'zip')
            if item['source'] == 'last-light':
                source_metadata = {}
                result = download.download(f['download_url'], target, kind, progress=say,
                                           progress_update=show_progress, metadata_out=source_metadata)
                if result == 'downloaded' and source_metadata:
                    item['last_light_source'] = source_metadata
                    save(state)
            else:
                download.download(f['download_url'], target, kind, progress=say, progress_update=show_progress)
        if item['source'] == 'last-light':
            site = DATA / 'last-light/site'
            if PREVIEW: say('Would extract Last Light into: ' + str(site))
            elif not (site / 'index.html').exists() or (site / '.cache-in-case-installing').exists(): download.install_last_light(target, site)
    if not PREVIEW and update and item['source'] == 'kiwix':
        item['files'] = targets
        save(state)


def download_summary(targets):
    """Resolve individual selections once, then summarize missing downloads."""
    say('\nChecking selected downloads...')
    plans, errors, rows, seen = {}, {}, [], set()
    total, unknown = 0, 0
    for item in targets:
        source = item['source']
        if source in ('kiwix-all', 'survivor-all'):
            rows.append('  {}: remaining download size unknown'.format(item['title']))
            unknown += 1
            continue
        if source == 'kiwix' and item.get('files') and all(usable_file(DATA / p) for p in item['files']):
            continue
        if source == 'last-light':
            site = DATA / 'last-light/site'
            if usable_file(site / 'index.html') and not (site / '.cache-in-case-installing').exists():
                continue
        try:
            if source == 'kiwix':
                for p in item.get('files', []): check_existing(DATA / p)
            if source == 'last-light': check_existing(DATA / 'last-light/site/index.html')
            if source in ('kiwix', 'survivor-category'):
                say('  Checking: ' + item['title'], log=False)
            files = plan_files(item)
            plans[id(item)] = files
            missing = []
            for entry in files:
                path = target_path(item, entry)
                check_existing(path)
                if not usable_file(path) and str(path) not in seen:
                    seen.add(str(path))
                    value = catalog.file_size(current_catalog(), entry['download_url'])
                    if value is None: value = entry.get('size')
                    if value is None and len(files) == 1: value = selection_size(item)
                    if type(value) is not int or value <= 0: value = None
                    missing.append((entry, value))
            if not missing:
                if source == 'last-light':
                    rows.append('  Last Light: extract the existing ZIP (no download)')
                continue
            values = [value for _, value in missing]
            known = sum(value for value in values if value is not None)
            missing_sizes = sum(value is None for value in values)
            total += known
            unknown += missing_sizes
            label = size_label({'size': known, 'size_approx': True}) if known else 'size unknown'
            if known and missing_sizes: label += ' + unknown sizes'
            count = ' ({} files)'.format(len(missing)) if len(missing) > 1 else ''
            rows.append('  {}{}: {}'.format(item['title'], count, label))
        except Exception as exc:
            errors[id(item)] = str(exc)
            rows.append('  {}: could not check ({})'.format(item['title'], exc))
            unknown += 1
    say('\nDownload summary:')
    if not rows:
        say('  No missing downloads found.')
    else:
        for row in rows: say(row)
        if total:
            label = size_label({'size': total, 'size_approx': True})
            say('  Estimated total: {}{}'.format(label, ' + unknown sizes' if unknown else ''))
        elif unknown:
            say('  Estimated total: unavailable')
        else:
            say('  Estimated download: 0 MB')
        if total: say('  Estimates are full file sizes; partial downloads may resume.')
        try:
            probe = DATA
            while not probe.exists() and probe != probe.parent: probe = probe.parent
            say('  Free space: {:.1f} GB'.format(shutil.disk_usage(probe).free / 1e9))
        except OSError:
            pass
    return plans, errors, bool(rows)


def sync(state, runlist=None, update=False, ask=False):
    targets = state if runlist is None else runlist
    if not targets: say('No selections saved. Choose starter or browse first.'); return 1
    plans, planning_errors = {}, {}
    if not update:
        plans, planning_errors, has_work = download_summary(targets)
        if ask and has_work and sys.stdin.isatty() and not PREVIEW and not confirm('Start downloading?'):
            say('Cancelled. Your selections are still saved.')
            return 0
    failed, partial = [], []
    for item in targets:
        say('\n' + item['title'])
        try:
            if id(item) in planning_errors:
                raise RuntimeError(planning_errors[id(item)])
            result = process(item, state, update=update, planned_files=plans.get(id(item)))
            if result == 'partial': partial.append(item['title'])
            if not PREVIEW:
                if result != 'skipped': item.pop('error', None)
                save(state)
        except Exception as e:
            failed.append(item['title']); say('FAILED: {}: {}'.format(item['title'], e))
            if not PREVIEW: item['error'] = str(e); save(state)
    if failed:
        say('\nCould not complete: ' + ', '.join(failed)); say('Rerun ' + ('upgrade' if update else 'download') + ' to retry.'); return 1
    if PREVIEW: say('\nPreview complete. No files or selections changed.')
    elif update: say('\nUpgrade check complete.')
    elif partial: say('\nAvailable downloads completed. Some categories had no ZIP; see the links above.')
    else: say('\nAll selected downloads completed or were already present.')
    return 0


def choose_source(include_all=False):
    print('\nSources:')
    if include_all:
        print('  all         The complete starter: all three sources')
        print('  kiwix       7 health, emergency, and practical references')
        print('  survivor    5 book collections: food, shelter, and survival')
        print('  last-light  The Last Light survival guide')
    else:
        print('  kiwix       Encyclopedias, maps, and practical guides')
        print('  survivor    Historical books by subject')
        print('  last-light  One offline survival guide and reference books')
    if include_all:
        return input('Choose all, kiwix, survivor, or last-light [all]: ').strip().lower() or 'all'
    return input('Choose kiwix, survivor, or last-light: ').strip().lower()


def run():
    global _catalog_cache
    _catalog_cache = None
    command = ARGS[0] if ARGS else ''
    if command in ('-h', '--help', 'help'):
        print('''Off-Grid Survival Shelf
Build your own offline library of survival knowledge.

Start:
  bash setup.sh                          Open the menu
  bash setup.sh starter                  Choose the starter library

Add content:
  bash setup.sh starter kiwix            Get the Kiwix starter collection
  bash setup.sh starter survivor         Get the Survivor starter collection
  bash setup.sh starter last-light       Get Last Light
  bash setup.sh browse kiwix [topic]     Browse Kiwix or search by topic
  bash setup.sh browse kiwix --category NAME [topic]
                                         Browse within a Kiwix category
  bash setup.sh browse survivor [topic]  Browse Survivor or search by topic
  bash setup.sh browse last-light        View and select the Last Light guide
  bash setup.sh kiwix all [category]     Select a category or all of Kiwix
  bash setup.sh survivor all             Select the Survivor library

Manage your library:
  bash setup.sh status                   Show saved selections and their status
  bash setup.sh download                 Download missing selections or retry
  bash setup.sh update                   Refresh the catalogs and sizes
  bash setup.sh update kiwix             Refresh Kiwix sizes
  bash setup.sh update survivor [topic]  Refresh Survivor categories and sizes
  bash setup.sh update last-light        Refresh the Last Light size
  bash setup.sh upgrade                  Get updates to downloaded content
  bash setup.sh log                      Follow the log (Ctrl+C to stop)

The starter includes 7 Kiwix references, 5 Survivor collections, and Last Light.
Last Light is one guide; Kiwix and Survivor have many items to browse.
Kiwix categories come from its catalog; all also includes unclassified items.
Add --preview to see a plan without downloading or saving selections.
Set your library folder in config.json (default: ./offline-library).
See README.md for instructions and how to open files offline.''')
        return 0
    state = read_state()
    if command == 'status': status(state); return 0
    if not PREVIEW:
        DATA.mkdir(parents=True, exist_ok=True)
    say('\nOFF-GRID SURVIVAL SHELF')
    say('Build your own offline library of survival knowledge.')
    if PREVIEW: say('Preview mode: no files or selections will be changed.')
    say('Library: ' + str(DATA))
    if not command:
        if not sys.stdin.isatty(): raise RuntimeError('Run interactively for setup, or use download for saved selections.')
        print('''\nNew here? The starter library includes:
  7 Kiwix references for health, emergencies, and practical skills
  5 Survivor book collections for food, shelter, medical care, and survival
  The Last Light survival guide

  starter   Select the starter library, or choose sources
  browse    Find and select more content
  download  Get saved selections or retry downloads
  update    Refresh catalog listings and size estimates
  upgrade   Check downloaded content for newer versions
  status    See saved selections and downloaded content
  help      Show all commands
  quit      Exit''')
        command = input('Choose an action: ').strip().lower()
        if command == 'browse':
            source = choose_source()
            ARGS[:] = ['browse', source]
        elif command == 'starter':
            group = choose_source(include_all=True)
            ARGS[:] = ['starter', group]
        elif command in ('help', '?'):
            ARGS[:] = ['help']
            return run()
        elif command == 'refresh':
            command = 'update'
            ARGS[:] = ['update']
        elif command in ('', 'quit', 'exit', 'q'): print('Cancelled.'); return 0
    known_sizes = {}
    if command in ('update', 'refresh-catalog'):
        group = ARGS[1] if len(ARGS) > 1 else 'all'
        topic = ' '.join(ARGS[2:]).strip()
        if group not in ('all', 'kiwix', 'survivor', 'last-light') or (topic and group != 'survivor'):
            raise ValueError('Use update [kiwix|survivor|last-light] [survivor topic].')
        if PREVIEW:
            print('Would refresh the saved catalog and sizes; no files would be downloaded.')
            return 0
        with (DATA / '.catalog-refresh.lock').open('a') as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('A catalog refresh is already running for this library.') from exc
            try:
                return catalog.refresh(DATA, state, group, STARTER_KIWIX, STARTER_SURVIVOR,
                                       LAST_URL, say, show_progress, topic=topic)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    elif command == 'starter':
        group = ARGS[1] if len(ARGS)>1 else 'all'
        if group not in ('all','kiwix','survivor','last-light'): raise ValueError('Choose all, kiwix, survivor, or last-light.')
        if len(ARGS) > 3 or (len(ARGS) == 3 and (group != 'kiwix' or ARGS[2] not in STARTER_KIWIX_TOPICS)):
            raise ValueError('Choose a Kiwix starter topic: medical, preparedness, or food.')
        chosen = []
        if group in ('all','kiwix'):
            prefixes = STARTER_KIWIX_TOPICS[ARGS[2]] if len(ARGS) == 3 else STARTER_KIWIX
            chosen += [{'source':'kiwix','id':p,'title':STARTER_KIWIX_TITLES.get(p, p),'prefix':True} for p in prefixes]
        if group in ('all','survivor'):
            chosen += [{'source':'survivor-file','id':p,'title':p} for p in STARTER_SURVIVOR]
        if group in ('all','last-light'): chosen.append({'source':'last-light','id':'standard','title':'Last Light Standard'})
    elif command == 'last-light': chosen = [{'source':'last-light','id':'standard','title':'Last Light Standard'}]
    elif command in ('survivor','kiwix') and len(ARGS)>1 and ARGS[1] == 'all':
        if not sys.stdin.isatty(): raise RuntimeError('Selecting the full catalog requires an interactive confirmation.')
        if command == 'survivor':
            if len(ARGS) > 2: raise ValueError('Use survivor all without a category.')
            print('The Survivor Library can be extremely large and may take days or longer.')
            print('This includes category ZIPs and other books found in its indexes.')
            prompt, title = 'Select the Survivor library?', 'Full Survivor Library'
            category = None
        else:
            if len(ARGS) > 3: raise ValueError('Use kiwix all [category].')
            category = choose_kiwix_category(ARGS[2] if len(ARGS) == 3 else None)
            if category is False: print('Cancelled.'); return 0
            if category:
                print('Selecting the Kiwix category: ' + category)
                print('Uncategorized content and maps from the separate map index are not included.')
                prompt, title = 'Select this Kiwix category?', 'Kiwix: ' + category
            else:
                print('The complete English Kiwix catalog can be extremely large and may take days or longer.')
                prompt, title = 'Select the full Kiwix catalog?', 'Full Kiwix catalog'
        if show_full_library_estimate(command, category=category) is False:
            return 0
        if not confirm(prompt): print('Cancelled.'); return 0
        chosen = [{'source':command+'-all','id':'category:' + category if category else 'all','title':title}]
        if category: chosen[0]['category'] = category
    elif command == 'browse':
        if len(ARGS) == 1 and sys.stdin.isatty():
            ARGS.append(choose_source())
        if len(ARGS)<2 or ARGS[1] not in ('kiwix','survivor','last-light'):
            raise ValueError('Choose browse kiwix, browse survivor, or browse last-light.')
        source = ARGS[1]; topic = ' '.join(ARGS[2:]).strip()
        category = None
        if source == 'kiwix' and len(ARGS) > 2 and ARGS[2] == '--category':
            if len(ARGS) < 4: raise ValueError('Use browse kiwix --category <name> [topic].')
            category = choose_kiwix_category(ARGS[3])
            if category is False: print('Cancelled.'); return 0
            topic = ' '.join(ARGS[4:]).strip()
        if source == 'last-light':
            print('Last Light is one offline survival guide with reference books.')
            print('Details: https://bunkerbot.co.uk/download.html')
            chosen = [{'source':'last-light','id':'standard','title':'Last Light Standard'}]
        else:
            if len(ARGS) == 2:
                if not sys.stdin.isatty(): raise RuntimeError('Use an interactive terminal to browse.')
                if source == 'kiwix':
                    category = choose_kiwix_category()
                    if category is False: print('Cancelled.'); return 0
                    if category:
                        print('Showing Kiwix category: ' + category)
                        print('Uncategorized content and separately indexed maps are not included.')
                topic = input('Search by topic (press Enter to browse everything): ').strip()
            if source == 'kiwix': say('Searching the Kiwix catalog...')
        if source == 'kiwix':
            if category:
                items = []
                for page, _, _ in kiwix.catalog_pages(category=category, query=topic):
                    for node in page:
                        entry = kiwix._item(node)
                        if entry is None:
                            raise RuntimeError('A Kiwix result has no usable ZIM download; try again.')
                        if entry.get('category', '').casefold() != category.casefold():
                            raise RuntimeError('Kiwix returned an item outside the selected category; try again.')
                        items.append(entry)
                more = False
            else:
                items, more = kiwix.search_page(topic, count=-1)
            if more:
                raise RuntimeError('Could not verify the complete Kiwix catalog. Try again.')
            apply_cached_kiwix_sizes(items)
            selected = select(items, 'Kiwix')
            for x in selected:
                print('\n{}\n  Details: {}\n  Size: {}'.format(x['title'], x['source_url'], size_label(x) if x.get('size') else 'unknown'))
                cached = catalog.file_size(current_catalog(), x['download_url']) if x.get('download_url') else None
                known_sizes[('kiwix', x['id'])] = cached if cached is not None else x.get('size')
            chosen = [{'source':'kiwix','id':x['id'],'title':x['title'],
                       'size':known_sizes.get(('kiwix', x['id']))} for x in selected]
        elif source == 'survivor':
            items = [dict(item) for item in current_catalog()['survivor']]
            if not items:
                raise RuntimeError('No saved Survivor catalog. Run bash setup.sh update survivor.')
            for item in items:
                if item.get('size'): item['size_approx'] = True
            items = [x for x in items if topic.casefold() in (x['title']+' '+x['id']).casefold()]
            print('Using the saved Survivor catalog. Sizes may have changed.')
            selected = select(items, 'Survivor Library')
            for x in selected:
                print('\n{}\n  Books: {}'.format(x['title'], x['source_url']))
                if x.get('size'): known_sizes[('survivor-category', x['id'])] = x['size']
            chosen = [{'source':'survivor-category','id':x['id'],'title':x['title'],
                       'url':x['source_url'], 'size':x.get('size')} for x in selected]
        if not chosen: print('No downloads selected.'); return 0
    elif command in ('download', 'sync'): return sync(state, ask=True)
    elif command == 'upgrade':
        if sys.stdin.isatty() and not PREVIEW and not confirm('Check for updated content?'):
            print('Cancelled.'); return 0
        return sync(state, update=True)
    elif command == 'status': status(state); return 0
    else: raise ValueError('Unknown action. Run bash setup.sh --help.')
    print('\nSelected: ' + ', '.join(i['title'] for i in chosen))
    try:
        probe = DATA
        while not probe.exists() and probe != probe.parent: probe = probe.parent
        print('Free space: {:.1f} GB'.format(shutil.disk_usage(probe).free / 1e9))
    except OSError:
        print('Free space: unavailable')
    if not PREVIEW:
        remember(state, chosen)
        say('Selections saved.')
        chosen = [next(x for x in state if x['source'] == i['source'] and x['id'] == i['id']) for i in chosen]
    if not any(item['source'] in ('kiwix-all', 'survivor-all') for item in chosen):
        show_selection_sizes(chosen, known_sizes)
    if PREVIEW:
        if sys.stdin.isatty() and not confirm('Preview these choices?'):
            print('Cancelled.'); return 0
    else:
        if sys.stdin.isatty() and not confirm('Download these selections now?'):
            say('Run bash setup.sh download when you are ready to download them.')
            return 0
    # Only run the newly chosen items, not other saved selections.
    return sync(state, chosen)


def main():
    command = ARGS[0] if ARGS else ''
    if config_error and not os.environ.get('CACHE_DATA') and command not in ('-h', '--help', 'help'):
        raise RuntimeError(config_error)
    if command == 'log':
        if not LOG.exists(): raise RuntimeError('No download log found at ' + str(LOG))
        os.execvp('tail', ['tail', '-F', str(LOG)])
    # Create an editable configuration on first normal use. A legacy location
    # is carried forward; environment overrides remain temporary.
    if not PREVIEW and command not in ('status', '-h', '--help', 'help') and not CONFIG.exists() and not os.environ.get('CACHE_DATA'):
        try:
            with CONFIG.open('x', encoding='utf-8') as f:
                json.dump({'library_path': configured_location}, f, indent=2)
                f.write('\n')
        except FileExistsError:
            pass
    if PREVIEW or command in ('status', '-h', '--help', 'help'):
        return run()
    DATA.mkdir(parents=True, exist_ok=True)
    if command in ('update', 'refresh-catalog'):
        return run()
    with (DATA / '.cache-in-case.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another Off-Grid Survival Shelf download is already running for this library.') from exc
        try:
            return run()
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


if __name__ == '__main__':
    try: sys.exit(main())
    except (Exception, KeyboardInterrupt) as exc:
        print('\nStopped: {}'.format(exc or 'Interrupted; rerun your command to retry.'), file=sys.stderr)
        sys.exit(1)
