#!/usr/bin/env bash
set -euo pipefail
if [[ -z "${CE_B_BUNDLE_DIR:-}" ]]; then
    echo "Set CE_B_BUNDLE_DIR to the extracted B offline bundle directory." >&2
    exit 2
fi
if [[ "$#" -eq 0 ]]; then set -- start; fi
exec bash "$CE_B_BUNDLE_DIR/deploy.sh" "$@"
