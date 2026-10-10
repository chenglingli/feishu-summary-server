"""Run from repository root: uv run python -m scripts.export_contract."""
import json
from pathlib import Path
from app.main import app
Path('contracts/openapi.json').write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + '\n')
