"""Reliable file transfers and safe extraction; no third-party packages required."""
import json
import hashlib
import http.client
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
import zipfile


class DownloadError(Exception):
    pass


class InsufficientSpaceError(DownloadError):
    pass


class ChecksumMismatchError(DownloadError):
    pass


class _HeadRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and req.get_method() == 'HEAD':
            redirected.method = 'HEAD'
        return redirected


def remote_metadata(url, timeout=8):
    """Read the publisher's file headers without starting a download."""
    request = urllib.request.Request(url, method='HEAD', headers={
        'User-Agent': 'Off-Grid-Survival-Shelf/1.0', 'Accept-Encoding': 'identity'})
    try:
        # urllib otherwise changes a redirected HEAD into a GET, which could
        # start a large download even when the user is only checking sizes.
        with urllib.request.build_opener(_HeadRedirect).open(request, timeout=timeout) as response:
            if response.getcode() != 200:
                return None
            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                return None
            if response.headers.get('Content-Type', '').lower().startswith('text/html'):
                return None
            length = response.headers.get('Content-Length', '')
            etag = response.headers.get('ETag', '')
            return {'size': int(length) if length.isdigit() and int(length) > 0 else None,
                    'etag': etag if etag and not etag.startswith('W/') else '',
                    'last_modified': response.headers.get('Last-Modified', '')}
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        return None


def remote_size(url, timeout=8):
    """Read the server's current file size without starting a download."""
    metadata = remote_metadata(url, timeout=timeout)
    return metadata.get('size') if metadata else None


def _kiwix_checksum(url, timeout):
    """Return a publisher SHA-256 when a sidecar is available."""
    parts = urllib.parse.urlsplit(url)
    checksum_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path + '.sha256', parts.query, ''))
    try:
        request = urllib.request.Request(checksum_url, headers={'User-Agent': 'Off-Grid-Survival-Shelf/1.0'})
        with urllib.request.urlopen(request, timeout=min(timeout, 15)) as response:
            text = response.read(2048).decode('ascii', errors='replace')
        match = re.match(r'\s*([a-fA-F0-9]{64})(?:\s|$)', text)
        return match.group(1).lower() if match else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path, data):
    temp = path.with_name(path.name + '.tmp')
    try:
        temp.write_text(json.dumps(data), encoding='utf-8')
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _valid(path, kind):
    if not path.is_file() or path.stat().st_size == 0:
        return False
    if kind == 'zim':
        with path.open('rb') as stream:
            return stream.read(4) == b'ZIM\x04'
    if kind == 'pdf':
        with path.open('rb') as stream:
            return b'%PDF-' in stream.read(1024)
    if kind in ('zip', 'epub'):
        try:
            with zipfile.ZipFile(path) as archive:
                return bool(archive.infolist()) and archive.testzip() is None
        except (OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
            return False
    raise ValueError('Unsupported file type: ' + str(kind))


def download(url, destination, kind, *, attempts=3, timeout=60, progress=None, progress_update=None, metadata_out=None):
    """Download to a temporary file, then validate and publish it.

    Existing files are left untouched and are not verified. A partial transfer is
    resumed only when the server supplied a validator for an If-Range request.
    Returns 'existing' or 'downloaded'; raises DownloadError on failure.
    """
    emit = progress if progress is not None else lambda message: print(message, flush=True)
    update = progress_update if progress_update is not None else emit
    dest = Path(destination)
    if dest.exists():
        if dest.is_file() and dest.stat().st_size:
            return 'existing'
        raise DownloadError('Destination already exists: ' + str(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + '.part')
    metadata = dest.with_name(dest.name + '.part.json')
    last_error = None
    for attempt in range(attempts):
        try:
            info = {}
            try:
                info = json.loads(metadata.read_text(encoding='utf-8'))
            except (FileNotFoundError, ValueError, OSError):
                pass
            if not isinstance(info, dict):
                info = {}
            offset = part.stat().st_size if part.is_file() else 0
            validator = info.get('validator') if info.get('url') == url else None
            if not isinstance(validator, str) or not validator:
                offset = 0
            headers = {'User-Agent': 'Off-Grid-Survival-Shelf/1.0', 'Accept-Encoding': 'identity'}
            if offset:
                headers.update({'Range': 'bytes=%d-' % offset, 'If-Range': validator})
            req = urllib.request.Request(url, headers=headers)
            try:
                response = urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as exc:
                if exc.code == 416 and offset:
                    # The server no longer accepts this partial range: restart.
                    metadata.unlink(missing_ok=True)
                    part.unlink(missing_ok=True)
                raise
            with response:
                code = response.getcode()
                if code not in (200, 206):
                    raise DownloadError('Unexpected HTTP status: %s' % code)
                if code == 206:
                    content_range = response.headers.get('Content-Range', '')
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+|\*)', content_range)
                    if not offset or not match or int(match.group(1)) != offset:
                        raise DownloadError('Invalid resumed download range: ' + content_range)
                    end = int(match.group(2))
                    total = match.group(3)
                    if end < offset or (total != '*' and end >= int(total)):
                        raise DownloadError('Invalid resumed download range: ' + content_range)
                else:
                    offset = 0
                etag = response.headers.get('ETag', '')
                valid_etag = etag if etag and not etag.startswith('W/') else ''
                last_modified = response.headers.get('Last-Modified', '')
                new_validator = valid_etag or last_modified
                if code == 206:
                    # A noncompliant server might ignore If-Range and serve a
                    # different edition. Compare the same validator type.
                    comparable = etag if validator.startswith('"') else response.headers.get('Last-Modified', '')
                    if comparable and comparable != validator:
                        metadata.unlink(missing_ok=True)
                        raise DownloadError('Publisher file changed during the download; restarting')
                length_value = response.headers.get('Content-Length')
                length = int(length_value) if length_value and length_value.isdigit() else None
                if length is not None:
                    # A restarted partial can be truncated and its space reused.
                    reusable = part.stat().st_size if not offset and part.is_file() else 0
                    free = shutil.disk_usage(dest.parent).free + reusable
                    if length > free:
                        raise InsufficientSpaceError('Not enough free space for {} (need {:.1f} MB; available {:.1f} MB)'.format(dest.name, length / 1e6, free / 1e6))
                written = 0
                last_report = time.monotonic()
                emit(('Resuming: ' if offset else 'Downloading: ') + dest.name)
                def report():
                    received = offset + written
                    total_size = offset + length if length is not None else None
                    unit, divisor = ('GB', 1e9) if (total_size or received) >= 1e9 else ('MB', 1e6)
                    if total_size:
                        ratio = min(1, received / total_size)
                        filled = int(ratio * 20)
                        bar = '#' * filled + '-' * (20 - filled)
                        update('  [%s] %.1f / %.1f %s (%d%%)' %
                               (bar, received / divisor, total_size / divisor, unit, int(100 * ratio)))
                    else:
                        update('  %.1f %s received (total size unavailable)' % (received / divisor, unit))
                report()
                reported = offset
                with part.open('ab' if offset else 'wb') as output:
                    # Truncate a restarted partial before saving its new validator;
                    # otherwise a crash could pair old bytes with a new validator.
                    _atomic_json(metadata, {'url': url, 'validator': new_validator})
                    read_chunk = getattr(response, 'read1', response.read)
                    while True:
                        chunk = read_chunk(256 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        written += len(chunk)
                        now = time.monotonic()
                        if now - last_report >= 1:
                            report()
                            reported = offset + written
                            last_report = now
                if length is not None and written != length:
                    raise DownloadError('Download stopped early: expected %d bytes, received %d' % (length, written))
                if code == 206 and written != end - offset + 1:
                    raise DownloadError('Resumed response did not match its Content-Range')
                if code == 206 and total != '*' and part.stat().st_size != int(total):
                    raise DownloadError('Incomplete download: expected %s bytes' % total)
                if offset + written != reported:
                    report()
            if kind in ('zip', 'epub'):
                emit('Checking {}: {}'.format(kind.upper(), dest.name))
            if not _valid(part, kind):
                # Invalid complete transfers cannot safely be resumed.
                part.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
                raise DownloadError('Downloaded file failed %s validation: %s' % (kind.upper(), dest.name))
            if kind == 'zim':
                expected = _kiwix_checksum(url, timeout)
                if expected:
                    emit('Checking SHA-256: ' + dest.name)
                    if _sha256(part) != expected:
                        part.unlink(missing_ok=True)
                        metadata.unlink(missing_ok=True)
                        raise ChecksumMismatchError('SHA-256 verification failed: ' + dest.name)
                    emit('SHA-256 verified: ' + dest.name)
                else:
                    emit('Checksum unavailable; basic ZIM check only: ' + dest.name)
            # Never replace a file that appeared while downloading.
            try:
                os.link(part, dest)
            except FileExistsError:
                raise DownloadError('Destination appeared during download: ' + str(dest))
            except OSError as exc:
                # Some filesystems do not support hard links. O_EXCL guarantees no
                # existing file is overwritten; keep .part until the copy succeeds.
                if dest.exists():
                    raise DownloadError('Destination appeared during download: ' + str(dest)) from exc
                try:
                    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
                except FileExistsError as race:
                    raise DownloadError('Destination appeared during download: ' + str(dest)) from race
                try:
                    with os.fdopen(fd, 'wb') as out, part.open('rb') as source:
                        shutil.copyfileobj(source, out, 1024 * 1024)
                except Exception:
                    # Only remove the file we created with O_EXCL.
                    dest.unlink(missing_ok=True)
                    raise
            part.unlink()
            metadata.unlink(missing_ok=True)
            if metadata_out is not None:
                metadata_out.update({'etag': valid_etag, 'last_modified': last_modified,
                                     'size': dest.stat().st_size})
            return 'downloaded'
        except (InsufficientSpaceError, ChecksumMismatchError):
            raise
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException, DownloadError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                emit('Download interrupted (%s). Retrying %d/%d...' % (exc, attempt + 2, attempts))
                time.sleep(min(2 ** attempt, 4))
    raise DownloadError('%s: %s' % (dest.name, last_error))


def install_last_light(zip_path, site_dir):
    """Verify and safely extract a guide to a new site directory."""
    zip_path = Path(zip_path)
    site = Path(site_dir)
    resume = False
    if site.exists() or site.is_symlink():
        if (site / '.cache-in-case-installing').exists():
            resume = site.is_dir() and not site.is_symlink()
        elif site.is_dir() and (site / 'index.html').is_file():
            return 'existing'
        if not resume:
            raise DownloadError('Site destination already exists: ' + str(site))
    if not _valid(zip_path, 'zip'):
        raise DownloadError('Last Light ZIP failed validation: ' + str(zip_path))
    site.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix='.last-light-', dir=site.parent))
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                name = info.filename.replace('\\', '/')
                relative = Path(name)
                if relative.is_absolute() or '..' in relative.parts or (relative.parts and ':' in relative.parts[0]):
                    raise DownloadError('Unsafe path in Last Light archive: ' + info.filename)
                # Reject Unix symlinks; ZIP symlinks must not escape staging.
                if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                    raise DownloadError('Unsafe symlink in Last Light archive: ' + info.filename)
            archive.extractall(temp)
        candidates = [p.parent for p in temp.rglob('index.html') if p.is_file()]
        if not candidates:
            raise DownloadError('Last Light archive does not contain index.html')
        # Prefer the shallowest index; secondary pages may also be named index.html.
        source = min(candidates, key=lambda p: len(p.relative_to(temp).parts))
        # mkdir is exclusive: unlike rename on POSIX, it cannot replace a
        # concurrently created empty directory. The marker prevents a partly
        # moved directory from being mistaken for a completed installation.
        if not resume:
            try:
                site.mkdir()
            except FileExistsError as exc:
                raise DownloadError('Site destination appeared during extraction: ' + str(site)) from exc
        marker = site / '.cache-in-case-installing'
        marker.touch()
        for entry in source.iterdir():
            target = site / entry.name
            # Top-level entries are moved atomically on the same filesystem.
            # An entry already moved during an interrupted run is left alone.
            if not target.exists() and not target.is_symlink():
                entry.rename(target)
        marker.unlink()
        return 'installed'
    except (OSError, zipfile.BadZipFile) as exc:
        raise DownloadError('Could not extract Last Light: %s' % exc) from exc
    finally:
        shutil.rmtree(temp, ignore_errors=True)
