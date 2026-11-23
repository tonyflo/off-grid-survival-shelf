#!/usr/bin/env bash
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3 is needed. Install Python 3, then run this command again.' >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))'; then
  echo 'Python 3.8 or newer is needed.' >&2
  exit 1
fi
exec python3 "$HERE/cache_in_case.py" "$@"
