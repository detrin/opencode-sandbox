#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -x "$ROOT/node_modules/.bin/srt" ] || (cd "$ROOT" && npm ci --silent)
exec python3 "$ROOT/launch.py" "$@"
