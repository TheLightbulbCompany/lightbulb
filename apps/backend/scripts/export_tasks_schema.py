"""Write `packages/shared/fixtures/super-agent/tasks-schema.json` from `core.tasks_schema`.

    uv run python -m scripts.export_tasks_schema

`tests/unit/super_agent/test_tasks_schema.py` fails while the checked-in file is stale.
"""

import json
from pathlib import Path

from core.tasks_schema import export_schema

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "packages/shared/fixtures/super-agent/tasks-schema.json"


def main() -> None:
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(json.dumps(export_schema(), indent=2) + "\n")
    print(f"wrote {SCHEMA_PATH}")


if __name__ == "__main__":
    main()
