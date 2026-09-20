#!/usr/bin/env python3
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("export-architecture-graph.py")
SPEC = importlib.util.spec_from_file_location("export_architecture_graph", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class ExportGraphTest(unittest.TestCase):
    def test_filters_code_slice_and_links(self):
        graph = {
            "nodes": [
                {"id": "keep", "file_type": "code", "source_file": "apps/a.py", "_origin": "ast"},
                {"id": "semantic", "file_type": "code", "source_file": "apps/a.py", "_origin": "semantic"},
                {"id": "deleted", "file_type": "code", "source_file": "apps/deleted.py", "_origin": "ast"},
            ],
            "links": [
                {"source": "keep", "target": "keep", "_origin": "ast", "source_file": "apps/a.py"},
                {"source": "keep", "target": "keep", "_origin": "semantic", "source_file": "apps/a.py"},
                {"source": "keep", "target": "keep", "_origin": "ast", "source_file": "apps/deleted.py"},
                {"source": "keep", "target": "missing", "_origin": "ast"},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "graph.json"
            source.write_text(json.dumps(graph), encoding="utf-8")
            with patch.object(MODULE, "tracked_sources", return_value={"apps/a.py"}):
                result = MODULE.export_graph(source, Path(directory))
        self.assertEqual(["keep"], [node["id"] for node in result["nodes"]])
        self.assertEqual(1, len(result["links"]))


if __name__ == "__main__":
    unittest.main()
