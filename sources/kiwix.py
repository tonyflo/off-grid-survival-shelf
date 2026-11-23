"""Find downloadable ZIMs in Kiwix's public OPDS catalog.

The user-facing ID combines the OPDS series name and flavour so later runs can
resolve a newer edition without accidentally changing Wikipedia variants.
"""
import html.parser
import http.client
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

CATALOG = 'https://opds.library.kiwix.org/catalog/v2/entries'
MAP_INDEX = 'https://lb.download.kiwix.org/zim/maps/'
BROWSE = 'https://browse.library.kiwix.org/'
MAX_CATALOG_BYTES = 20 * 1024 * 1024


def _fetch(url, attempts=3, timeout=30):
    req = urllib.request.Request(url, headers={'User-Agent': 'Off-Grid-Survival-Shelf/1.0'})
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = response.read(MAX_CATALOG_BYTES + 1)
                if len(data) > MAX_CATALOG_BYTES:
                    raise ValueError('Kiwix catalog response is unexpectedly large')
                return data
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt + 1 == attempts:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException):
            if attempt + 1 == attempts:
                raise
        time.sleep(attempt + 1)


def _local_name(tag):
    return tag.rsplit('}', 1)[-1]


def _child_text(entry, name):
    for child in entry:
        if _local_name(child.tag) == name:
            return ''.join(child.itertext()).strip()
    return ''


def _feed(query, fetch):
    url = CATALOG + '?' + urllib.parse.urlencode(query)
    try:
        root = ET.fromstring(fetch(url))
    except ET.ParseError as exc:
        raise ValueError('The Kiwix catalog returned an unreadable response') from exc
    if _local_name(root.tag) not in ('feed', 'entry'):
        raise ValueError('The Kiwix catalog returned an unexpected response')
    return root


def _entries(query, fetch):
    root = _feed(query, fetch)
    return [root] if _local_name(root.tag) == 'entry' else [x for x in root if _local_name(x.tag) == 'entry']


def catalog_pages(fetch=None):
    """Yield verified pages of the complete English catalog, without ZIMs."""
    fetch = fetch or _fetch
    start, total, fingerprints = 0, None, set()
    while True:
        root = _feed({'start': start, 'count': 100, 'lang': 'eng'}, fetch)
        if _local_name(root.tag) != 'feed':
            raise RuntimeError('Kiwix catalog returned an unexpected response')
        reported = _child_text(root, 'totalResults')
        if not reported.isdigit():
            raise RuntimeError('Kiwix catalog did not report its total; cannot verify the full list')
        current_total = int(reported)
        if total is None: total = current_total
        if current_total != total: raise RuntimeError('Kiwix catalog changed during discovery; retry')
        if not total: raise RuntimeError('Kiwix catalog returned no entries')
        page = [x for x in root if _local_name(x.tag) == 'entry']
        if not page: raise RuntimeError('Kiwix catalog ended before all entries were returned')
        fingerprint = tuple(_child_text(x, 'id') for x in page)
        if fingerprint in fingerprints: raise RuntimeError('Kiwix catalog repeated a page; retry')
        fingerprints.add(fingerprint)
        start += len(page)
        yield page, start, total
        if start >= total: return


def _item(entry):
    name = _child_text(entry, 'name')
    flavour = _child_text(entry, 'flavour')
    if not name:
        return None
    acquisition = None
    source = None
    for link in entry:
        if _local_name(link.tag) != 'link':
            continue
        href, rel, typ = link.get('href', ''), link.get('rel', ''), link.get('type', '')
        if 'acquisition' in rel and 'zim' in typ and href:
            acquisition = link
        if typ == 'text/html' and href:
            source = urllib.parse.urljoin(BROWSE, href)
    if acquisition is None:
        return None
    url = acquisition.get('href', '')
    url = urllib.parse.urljoin(CATALOG, url)
    parts = urllib.parse.urlsplit(url)
    path = parts.path[:-6] if parts.path.endswith('.meta4') else parts.path
    if not path.lower().endswith('.zim') or parts.scheme != 'https':
        return None
    url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, ''))
    filename = urllib.parse.unquote(path.rsplit('/', 1)[-1])
    if '/' in filename or '\\' in filename or filename in ('.', '..'):
        return None
    try:
        size = int(acquisition.get('length', ''))
        if size < 0: size = None
    except ValueError:
        size = None
    title = _child_text(entry, 'title') or name
    if flavour:
        title += ' (' + flavour + ')'
    return {
        'id': name + '|' + flavour,
        'title': title,
        'description': _child_text(entry, 'summary'),
        'size': size, 'size_approx': True,
        'source_url': source or BROWSE,
        'download_url': url,
        'filename': filename,
        'updated': _child_text(entry, 'updated'),
    }


class _MapLinks(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current = None
        self.after_link = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'a':
            href = dict(attrs).get('href') or ''
            if re.fullmatch(r'maps_en_[a-z0-9-]+_20\d\d-\d\d[a-z0-9-]*\.zim', href):
                self.current = {'filename': href, 'size': None}
                self.links.append(self.current)
            else:
                self.current = None
            self.after_link = False

    def handle_endtag(self, tag):
        if tag.lower() == 'a' and self.current is not None:
            self.after_link = True

    def handle_data(self, text):
        if not self.after_link:
            return
        # Apache's index puts the date and rounded size after each link.
        match = re.match(r'\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+([\d.]+)([KMGT])(?:\s|$)', text)
        if match:
            self.current['size'] = int(float(match.group(1)) * 1024 ** ('KMGT'.index(match.group(2)) + 1))
        self.after_link = False


def _maps(fetch):
    parser = _MapLinks()
    raw = fetch(MAP_INDEX)
    parser.feed(raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else raw)
    # A valid index with zero recognizable ZIMs should not be reported as empty.
    if not parser.links:
        raise ValueError('The Kiwix map index did not contain any map files')
    latest = {}
    for link in parser.links:
        filename = link['filename']
        match = re.fullmatch(r'(maps_en_[a-z0-9-]+)_(20\d\d-\d\d[a-z0-9-]*)\.zim', filename)
        prefix, date = match.groups()
        if prefix not in latest or date > latest[prefix][0]:
            latest[prefix] = (date, link)
    items = []
    for prefix, (date, link) in latest.items():
        filename = link['filename']
        region = prefix.removeprefix('maps_en_') if hasattr(str, 'removeprefix') else prefix[len('maps_en_'):]
        title = 'World map' if region == 'all' else region.replace('-', ' ').title() + ' map'
        items.append({'id': 'maps|' + prefix, 'title': title, 'description': 'Offline map',
                      'size': link['size'], 'size_approx': True, 'source_url': MAP_INDEX, 'download_url': MAP_INDEX + filename,
                      'filename': filename, 'updated': date})
    return items


def search_page(query, start=0, count=100, fetch=None):
    """Return English results and whether another catalog page exists."""
    language = 'eng'
    fetch = fetch or _fetch
    params = {'start': start, 'count': count}
    if query.strip(): params['q'] = query.strip()
    if language: params['lang'] = language
    root = _feed(params, fetch)
    entries = [root] if _local_name(root.tag) == 'entry' else [x for x in root if _local_name(x.tag) == 'entry']
    total_text = _child_text(root, 'totalResults')
    more = start + len(entries) < int(total_text) if total_text.isdigit() else len(entries) >= count
    items = [item for entry in entries if (item := _item(entry))]
    # Maps are not reliably found by OPDS text queries. Query the maps index
    # only for an explicit map search, not for every catalog browse.
    if start == 0 and language in ('', 'eng') and (not query.strip() or re.search(r'\bmaps?\b', query, re.I)):
        try:
            terms = [x.lower() for x in re.findall(r'[a-z0-9]+', query) if x.lower() not in ('map', 'maps')]
            maps = [item for item in _maps(fetch) if all(t in (item['title'] + ' ' + item['id']).lower() for t in terms)]
            # Prefer the map-index ID even when OPDS also includes this file;
            # that ID can always be resolved against the map index later.
            for map_item in maps:
                existing = next((item for item in items if item['filename'] == map_item['filename']), None)
                if existing:
                    existing['id'] = map_item['id']
                else:
                    items.append(map_item)
        except (OSError, ValueError):
            # Surface failure: returning incomplete results would suggest that
            # the index was checked successfully when it was not.
            raise ValueError('Could not discover Kiwix maps; try again')
    return items, more


def search(query, language='eng', start=0, count=100, fetch=None):
    """Return one page. English is the supported browsing language."""
    if language not in ('eng', ''):
        raise ValueError('Browsing is available in English')
    # Retain all-language catalog access for callers that need it.
    if not language:
        fetch = fetch or _fetch
        params = {'start': start, 'count': count}
        if query.strip(): params['q'] = query.strip()
        return [item for entry in _entries(params, fetch) if (item := _item(entry))]
    return search_page(query, start=start, count=count, fetch=fetch)[0]


def resolve(item_id, fetch=None):
    """Resolve a chosen series and flavour to its latest available edition."""
    fetch = fetch or _fetch
    if item_id.startswith('maps|'):
        matches = [x for x in _maps(fetch) if x['id'] == item_id]
    else:
        if '|' not in item_id:
            raise ValueError('Invalid Kiwix selection')
        name, flavour = item_id.split('|', 1)
        matches = [x for entry in _entries({'name': name, 'count': -1}, fetch)
                   if (x := _item(entry)) and x['id'] == item_id]
    if not matches:
        raise ValueError('Kiwix collection is unavailable: ' + item_id)
    return max(matches, key=lambda item: (item['updated'], item['filename']))


def resolve_prefix(prefix, fetch=None):
    """Resolve a curated filename prefix to its exact series and flavour."""
    fetch = fetch or _fetch
    if prefix.startswith('maps_en_'):
        return resolve('maps|' + prefix, fetch)
    match = re.fullmatch(r'(.+)_(maxi|mini|nopic|novid)', prefix)
    name, flavour = match.groups() if match else (prefix, '')
    return resolve(name + '|' + flavour, fetch)
