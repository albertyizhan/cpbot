#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p .runtime
stage="$(mktemp "$ROOT/.runtime/cpbot-build.XXXXXX")"
trap 'unlink "$stage" 2>/dev/null || true' EXIT
CGO_ENABLED=0 "${CPBOT_GO:-go}" build -trimpath -o "$stage" ./cmd/cpbot
chmod 700 "$stage"
mv "$stage" "$ROOT/.runtime/cpbot-go"
