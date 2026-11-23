"""Saved publisher catalog and optional, explicit metadata refresh."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from sources import download, kiwix, survivor

BUNDLED = Path(__file__).with_name('catalog-snapshot.json')
CACHE_NAME = 'cache-in-case-catalog.json'


def _read(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('survivor'), list):
            return None
        if not all(isinstance(c, dict) and all(isinstance(c.get(k), str) for k in ('id', 'title', 'source_url')) for c in data['survivor']):
            return None
        if not isinstance(data.get('files', {}), dict) or not isinstance(data.get('kiwix', {}), dict):
            return None
        for category in data['survivor']:
            if type(category.get('size')) is not int or category['size'] <= 0:
                category.pop('size', None)
            if not isinstance(category.get('checked_at'), str):
                category.pop('checked_at', None)
            if category.get('no_zip') is not True:
                category.pop('no_zip', None)
        data.setdefault('files', {})
        data.setdefault('kiwix', {})
        return data
    except (OSError, ValueError):
        return None


def load(data_dir):
    """Use the personal catalog when valid, otherwise the bundled snapshot."""
    return _read(Path(data_dir) / CACHE_NAME) or _read(BUNDLED) or {
        'survivor': [], 'files': {}, 'kiwix': {}}


def save(data_dir, data):
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.catalog-', dir=data_dir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(data, output, indent=2, ensure_ascii=False)
            output.write('\n')
        os.replace(name, data_dir / CACHE_NAME)
    finally:
        if os.path.exists(name): os.unlink(name)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def file_size(data, url):
    entry = data.get('files', {}).get(url)
    if isinstance(entry, dict) and type(entry.get('size')) is int and entry['size'] > 0:
        return entry['size']
    return None


def _check_category(category):
    """Read a single category and HEAD its ZIPs; never fetch archive bodies."""
    try:
        fetch = lambda url: survivor.fetch_page(url, attempts=1, timeout=8)
        files = survivor.category_downloads(category['source_url'], fetch=fetch)
        checked = {f['download_url']: download.remote_size(f['download_url']) for f in files}
        if not checked or any(value is None for value in checked.values()):
            return checked, None, 'unavailable'
        return checked, sum(checked.values()), 'ok'
    except survivor.NoCategoryZipError:
        return {}, None, 'no_zip'
    except Exception:
        return {}, None, 'unavailable'


def _check_kiwix(item):
    entry = None
    for _ in range(2):
        try:
            if entry is None:
                fetch = lambda url: kiwix._fetch(url, attempts=1, timeout=15)
                resolve = kiwix.resolve_prefix if item.get('prefix') else kiwix.resolve
                entry = resolve(item['id'], fetch=fetch)
            size = download.remote_size(entry['download_url'], timeout=20)
            if size is not None: return entry, size
        except Exception:
            entry = None
    return None, None


def refresh(data_dir, state, group, starters, survivor_starters, last_url, say, progress, topic=''):
    """Refresh metadata only. Failed lookups keep their last successful sizes."""
    cache_path = Path(data_dir) / CACHE_NAME
    if cache_path.is_symlink() or (cache_path.exists() and _read(cache_path) is None):
        raise RuntimeError('Cannot read saved catalog: {}. Move it aside before refreshing.'.format(cache_path))
    data = load(data_dir)
    save(data_dir, data)
    errors = []
    no_zips = []
    if group in ('all', 'survivor'):
        say('Refreshing Survivor Library categories and sizes...')
        try:
            if topic:
                categories = [item for item in data['survivor']
                              if topic.casefold() in (item['id'] + ' ' + item['title']).casefold()]
                if not categories: raise ValueError('No Survivor categories match: ' + topic)
            else:
                fetch = lambda url: survivor.fetch_page(url, attempts=2, timeout=12)
                found = survivor.discover_categories(fetch=fetch)
                old = {c['id'].casefold(): c for c in data['survivor']}
                if len(old) >= 10 and len(found) < len(old) / 2:
                    raise RuntimeError('The publisher returned an incomplete category list')
                categories = []
                for entry in found:
                    previous = old.get(entry['id'].casefold(), {})
                    if previous.get('source_url') == entry['source_url']:
                        for field in ('size', 'checked_at', 'no_zip'):
                            if field in previous: entry[field] = previous[field]
                    categories.append(entry)
                data['survivor'] = categories
                data['survivor_checked_at'] = _now()
                save(data_dir, data)
            checked_urls = set()
            pool = ThreadPoolExecutor(max_workers=4)
            futures = {pool.submit(_check_category, item): item for item in categories}
            try:
                for count, future in enumerate(as_completed(futures), 1):
                    item = futures[future]
                    checked, total, status = future.result()
                    stamp = _now()
                    for url, size in checked.items():
                        if size is not None:
                            data['files'][url] = {'size': size, 'checked_at': stamp}
                            checked_urls.add(url)
                    if status == 'no_zip':
                        item.pop('size', None)
                        item['no_zip'], item['checked_at'] = True, stamp
                        no_zips.append(item['title'])
                    elif total is None:
                        errors.append('Survivor: ' + item['title'])
                    else:
                        item.pop('no_zip', None)
                        item['size'], item['checked_at'] = total, stamp
                    save(data_dir, data)
                    progress('Survivor categories checked: {} / {}'.format(count, len(categories)))
            except BaseException:
                for future in futures: future.cancel()
                raise
            finally:
                pool.shutdown(wait=True)
            # Starter files can exist even when their category page has no ZIP.
            urls = ['https://www.survivorlibrary.com/library/' + name for name in survivor_starters]
            missing = [url for url in urls if url not in checked_urls] if not topic else []
            if missing:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    sizes = list(pool.map(download.remote_size, missing))
                for url, size in zip(missing, sizes):
                    if size is None:
                        errors.append('Survivor starter: ' + url.rsplit('/', 1)[-1])
                    else:
                        data['files'][url] = {'size': size, 'checked_at': _now()}
                save(data_dir, data)
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append('Survivor catalog: ' + str(exc))

    if group in ('all', 'kiwix'):
        say('Refreshing Kiwix starter and saved selection sizes...')
        items = [{'id': name, 'prefix': True} for name in starters]
        items += [item for item in state if item.get('source') == 'kiwix']
        unique = {item['id']: item for item in items}
        pool = ThreadPoolExecutor(max_workers=4)
        futures = {pool.submit(_check_kiwix, item): item for item in unique.values()}
        try:
            for count, future in enumerate(as_completed(futures), 1):
                item = futures[future]
                entry, size = future.result()
                if size is None:
                    errors.append('Kiwix: ' + item['id'])
                else:
                    stamp = _now()
                    data['kiwix'][item['id']] = {'size': size, 'checked_at': stamp,
                                                 'download_url': entry['download_url']}
                    data['files'][entry['download_url']] = {'size': size, 'checked_at': stamp}
                    save(data_dir, data)
                progress('Kiwix selections checked: {} / {}'.format(count, len(unique)))
        except BaseException:
            for future in futures: future.cancel()
            raise
        finally:
            pool.shutdown(wait=True)

    if group in ('all', 'last-light'):
        say('Refreshing Last Light size...')
        size = download.remote_size(last_url)
        if size is None:
            errors.append('Last Light')
        else:
            data['files'][last_url] = {'size': size, 'checked_at': _now()}
            save(data_dir, data)

    say('Saved catalog: ' + str(Path(data_dir) / CACHE_NAME))
    if no_zips:
        say('No category ZIP found for: ' + ', '.join(no_zips))
    if errors:
        say('Could not check {} item(s); previous sizes were kept:'.format(len(errors)))
        for error in errors: say('  ' + error)
        say('Run the same command to try again.')
        return 1
    say('Catalog and sizes refreshed. No library files were downloaded.')
    return 0
