#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Raphael Friedmann
# Splices lib/shared.py into every tool between the "# ==== shared:" markers.
set -euo pipefail
cd "$(dirname "$0")/.."
for tool in pvesnap pvedisk pvebackup; do
  [ -f "$tool" ] || continue
  awk -v src=lib/shared.py '
    /^# ==== shared:begin/ { print; while ((getline line < src) > 0) print line; close(src); skip = 1; next }
    /^# ==== shared:end/ { skip = 0 }
    !skip { print }
  ' "$tool" > "$tool.tmp"
  chmod --reference="$tool" "$tool.tmp"
  mv "$tool.tmp" "$tool"
done
