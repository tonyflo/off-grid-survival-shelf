"""Discover Survivor Library categories and their ZIPs from the publisher."""

from html.parser import HTMLParser
import http.client
import re
import time
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

INDEX = 'https://www.survivorlibrary.com/index.php/main-category-index/'


class NoCategoryZipError(RuntimeError):
    """The publisher lists books but does not offer a category ZIP."""


def fetch_page(url, attempts=3, timeout=30):
    """Fetch one publisher page; callers receive a useful error after retries."""
    last_error = None
    for attempt in range(attempts):
        try:
            request = Request(url, headers={'User-Agent': 'Off-Grid-Survival-Shelf/1.0'})
            with urlopen(request, timeout=timeout) as response:
                # Discovery only reads HTML pages, never the archive itself.
                data = response.read(20 * 1024 * 1024 + 1)
                if len(data) > 20 * 1024 * 1024:
                    raise ValueError('Publisher page is unexpectedly large')
                return data.decode(response.headers.get_content_charset() or 'utf-8', errors='replace')
        except (OSError, ValueError, http.client.HTTPException) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(attempt + 1)
    raise RuntimeError('Could not read Survivor Library page {}: {}'.format(url, last_error))


def official_url(href, base=INDEX):
    """Accept links only to the publisher and normalize links and spaces."""
    try:
        parts = urlsplit(urljoin(base, href.strip()))
        if parts.scheme.lower() not in ('http', 'https'):
            return None
        if parts.hostname is None or parts.hostname.lower() not in ('survivorlibrary.com', 'www.survivorlibrary.com'):
            return None
        if parts.username or parts.password or parts.port not in (None, 80, 443):
            return None
        path = quote(unquote(parts.path), safe='/@-._~')
        return urlunsplit(('https', 'www.survivorlibrary.com', path, '', ''))
    except ValueError:
        return None


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.anchor = None
        self.row = None

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self.row = {'links': [], 'text': []}
        if tag == 'a':
            href = dict(attrs).get('href')
            self.anchor = None
            if href:
                # Record the href immediately: publisher pages may contain
                # malformed or unclosed anchors before the next link.
                link = {'href': href, 'text': '', 'row': '', '_text': []}
                self.links.append(link)
                if self.row is not None:
                    self.row['links'].append(link)
                self.anchor = link

    def handle_data(self, text):
        if self.anchor is not None:
            self.anchor['_text'].append(text)
        if self.row is not None:
            self.row['text'].append(text)

    def handle_endtag(self, tag):
        if tag == 'a' and self.anchor is not None:
            self.anchor = None
        if tag in ('td', 'th') and self.anchor is not None:
            # Some category pages omit </a> in table cells.
            self.anchor = None
        if tag == 'tr' and self.row is not None:
            row_text = ' '.join(' '.join(self.row['text']).split())
            for link in self.row['links']:
                link['row'] = row_text
            self.row = None


def _links(html):
    if isinstance(html, bytes):
        html = html.decode('utf-8', errors='replace')
    if not isinstance(html, str):
        raise ValueError('Expected HTML text from Survivor Library')
    parser = Links()
    parser.feed(html)
    for link in parser.links:
        link['text'] = ' '.join(' '.join(link.pop('_text')).split())
    return parser.links


def _category_id(url):
    path = unquote(urlsplit(url).path)
    match = re.fullmatch(r'/index\.php/library-([^/]+)/?', path, flags=re.I)
    return match.group(1) if match else None


def _label(slug):
    return ' '.join(re.sub(r'[-_]+', ' ', slug).split()).strip().title()


def discover_categories(fetch=fetch_page):
    """Return all categories currently linked from the official index."""
    found = {}
    for link in _links(fetch(INDEX)):
        url = official_url(link['href'])
        if not url:
            continue
        slug = _category_id(url)
        if not slug:
            continue
        url = url.rstrip('/') + '/'
        title = link['text'].strip().replace('_', ' ') or _label(slug)
        # Some index links are navigation or repeat the same page.
        found.setdefault(slug.lower(), {'id': slug, 'title': title, 'source_url': url})
    if not found:
        raise RuntimeError('No Survivor Library categories found; the publisher page may have changed.')
    return sorted(found.values(), key=lambda item: item['title'].casefold())


def _size(text):
    # The site provides rounded values like "283 mb"; they are estimates.
    match = re.search(r'(?<![\w.])(\d+(?:\.\d+)?)\s*(kb|mb|gb|tb)\b', text, re.I)
    if not match:
        return None
    return int(float(match.group(1)) * 1000 ** ('kb', 'mb', 'gb', 'tb').index(match.group(2).lower()) * 1000)


def _library_downloads(html, page):
    """Collect publisher ZIPs and individual books from one index page."""
    found = {}
    for link in _links(html):
        download = official_url(link['href'], page)
        if not download:
            continue
        path = unquote(urlsplit(download).path)
        if not path.startswith('/library/') or not path.lower().endswith(('.zip', '.pdf', '.epub')):
            continue
        filename = path.rsplit('/', 1)[-1]
        if filename in ('', '.', '..') or '\\' in filename or any(ord(c) < 32 for c in filename):
            continue
        found.setdefault(download, {
            'id': filename, 'title': filename, 'source_url': page,
            'download_url': download, 'filename': filename,
            'size': _size(link['text']) or _size(link['row']), 'size_approx': True,
            'kind': filename.rsplit('.', 1)[-1].lower(),
        })
    return sorted(found.values(), key=lambda item: item['filename'].casefold())


def category_contents(url, fetch=fetch_page):
    """Return the ZIPs and book links from one category page."""
    page = official_url(url)
    if not page or not _category_id(page):
        raise ValueError('Not a Survivor Library category URL: {}'.format(url))
    page = page.rstrip('/') + '/'
    files = _library_downloads(fetch(page), page)
    if not files:
        raise RuntimeError('No ZIP or book links found; the publisher page may have changed: {}'.format(page))
    return [f for f in files if f['kind'] == 'zip'], [f for f in files if f['kind'] != 'zip']


def category_downloads(url, fetch=fetch_page):
    """Return every ZIP on a category page, without downloading the ZIPs."""
    zips, books = category_contents(url, fetch=fetch)
    if not zips:
        if books:
            page = official_url(url).rstrip('/') + '/'
            raise NoCategoryZipError('No ZIP links found on Survivor Library page: {}'.format(page))
    return zips


def recent_downloads(fetch=fetch_page):
    """Read the broadest recent-books index linked from the main index."""
    choices = {}
    index_html = fetch(INDEX)
    for link in _links(index_html):
        url = official_url(link['href'])
        if not url:
            continue
        match = re.fullmatch(r'/index\.php/last-(\d+)-days/?', urlsplit(url).path, re.I)
        if match:
            choices[int(match.group(1))] = url
    if not choices:
        raise RuntimeError('The publisher no longer lists a recent-books index.')
    days = max(choices)
    page = choices[days]
    files = _library_downloads(fetch(page), page)
    files += _library_downloads(index_html, INDEX)
    if not any(f['kind'] in ('pdf', 'epub') for f in files):
        raise RuntimeError('The publisher recent-books index has no book links.')
    unique = {f['download_url']: f for f in files}
    return days, list(unique.values())
