#!/bin/bash
set -euo pipefail
SERVER_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -x "$SERVER_DIR/.venv/bin/python" ]; then
  echo '缺少后端虚拟环境，请先在后端目录执行 uv sync --locked。' >&2
  exit 1
fi
exec "$SERVER_DIR/.venv/bin/python" "$SERVER_DIR/scripts/local_services.py" "$@"
