#!/usr/bin/env python3
"""Emit an apply_patch patch for an honest legacy-node coverage manifest.

Supply the pytest --collect-only -q log from scripts/run_tests.py legacy.
No tests are imported here. Assertions remain linked to their source hashes;
coverage must be reviewed against full behavior, not inferred from test names.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("collection_log", type=Path)
    args = parser.parse_args()
    nodes = [line.strip() for line in args.collection_log.read_text().splitlines()
             if line.startswith("tests/") and "::" in line]
    assert len(nodes) == len(set(nodes)), "collection log has duplicate nodes"
    definitions = {}

    def walk(body, path, source, scope=()):
        for node in body:
            if isinstance(node, ast.ClassDef):
                walk(node.body, path, source, (*scope, node.name))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                key = "::".join((str(path), *scope, node.name))
                assertions = [ast.get_source_segment(source, item) for item in ast.walk(node)
                              if isinstance(item, ast.Assert) or (
                                  isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
                                  and (item.func.attr.startswith("assert_") or item.func.attr == "raises"))]
                definitions[key] = {"line": node.lineno,
                                    "sha256": hashlib.sha256(ast.get_source_segment(source, node).encode()).hexdigest(),
                                    "assertions": assertions, "e2e_scenarios": [],
                                    "status": "unmapped"}

    for path in sorted(set(node.split("::")[0] for node in nodes)):
        source = (REPO / path).read_text()
        walk(ast.parse(source).body, Path(path), source)
    for node in nodes:
        assert node.split("[", 1)[0] in definitions, node
    reviewed = json.loads((REPO / "doc/e2e-reviewed-mappings.json").read_text())
    for key, mapping in reviewed.items():
        assert key in definitions, f"unknown legacy definition: {key}"
        for scenario in mapping["e2e_scenarios"]:
            path, function = scenario.split("::")
            tree = ast.parse((REPO / path).read_text())
            assert any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == function
                       for item in tree.body), f"unknown E2E scenario: {scenario}"
        definitions[key].update(mapping)
    payload = {"schema": 2, "note": "All legacy tests remain. Unmapped means no reviewed E2E equivalence; partial mappings list explicit gaps.",
               "collected_count": len(nodes), "nodes": nodes, "definitions": definitions}
    content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    target = "doc/e2e-legacy-manifest.json"
    if (REPO / target).exists():
        old = (REPO / target).read_text()
        print(f"*** Begin Patch\n*** Update File: {target}\n@@")
        print("\n".join("-" + line for line in old.splitlines()))
        print("\n".join("+" + line for line in content.splitlines()))
    else:
        print(f"*** Begin Patch\n*** Add File: {target}")
        print("\n".join("+" + line for line in content.splitlines()))
    print("*** End Patch")


if __name__ == "__main__":
    main()
