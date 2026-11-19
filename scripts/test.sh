#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Raphael Friedmann
# Runs the test suite in Docker against PVE 7 (py3.9) and PVE 9 (py3.13) Python versions.
# qemu-utils: pverestore tests convert real images.
set -euo pipefail
cd "$(dirname "$0")/.."
for v in ${PY_VERSIONS:-3.9 3.13}; do
  echo "== python:$v"
  docker run --rm -v "$PWD":/src:ro -w /src "python:$v-slim" sh -c \
    "apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends qemu-utils >/dev/null 2>&1 && python -m unittest discover -s tests -v" 2>&1 | tail -n "${TAIL:-8}"
done
