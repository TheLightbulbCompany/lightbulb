#!/usr/bin/env python3
"""Export the tracked, code-only slice of a Graphify JSON graph."""

from __future__ import annotations

import argparse
import gzip
import json
import subprocess
from pathlib import Path


def tracked_sources(repo: Path) -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--", "apps/", "packages/", ".github/"],
        check=True,
        capture_output=True,
        text=True,
    )
    return set(result.stdout.splitlines())


def export_graph(source: Path, repo: Path) -> dict:
    graph = json.loads(source.read_text(encoding="utf-8"))
    tracked = tracked_sources(repo)
    nodes = [
        node
        for node in graph.get("nodes", [])
        if node.get("_origin") == "ast"
        and node.get("file_type") == "code"
        and node.get("source_file") in tracked
    ]
    node_ids = {node["id"] for node in nodes}
    links = [
        link
        for link in graph.get("links", [])
        if link.get("_origin") == "ast"
        and (not link.get("source_file") or link["source_file"] in tracked)
        and link.get("source") in node_ids
        and link.get("target") in node_ids
    ]
    return {"directed": bool(graph.get("directed", False)), "nodes": nodes, "links": links}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="Graphify JSON input")
    parser.add_argument("output", type=Path, help="gzip JSON output")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args()
    result = export_graph(args.source, args.repo)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    with args.output.open("wb") as compressed:
        with gzip.GzipFile(filename="", fileobj=compressed, mode="wb", mtime=0) as stream:
            stream.write(payload)
    print(f"{len(result['nodes'])} nodes, {len(result['links'])} links → {args.output}")


if __name__ == "__main__":
    main()
