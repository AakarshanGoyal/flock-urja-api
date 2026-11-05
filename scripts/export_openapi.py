"""Write openapi.json from the FastAPI app (no portal access needed).

    python scripts/export_openapi.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import create_app  # noqa: E402

spec = create_app(warm_index=False).openapi()
(ROOT / "openapi.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
print(f"wrote openapi.json ({len(spec['paths'])} paths)")
