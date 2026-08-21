"""Static untrusted-import boundary for industrial definitions."""

from __future__ import annotations

import ast
import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = [
    os.path.join(ROOT, "cansniff", "analysis", "definitions.py"),
    os.path.join(ROOT, "cansniff", "analysis", "canopen_definitions.py"),
]


class DefinitionSecurityTests(unittest.TestCase):
    def test_parser_has_no_eval_exec_network_process_or_source_access(self):
        violations = []
        forbidden_calls = {"eval", "exec", "system", "popen", "urlopen"}
        forbidden_imports = {"socket", "subprocess", "urllib", "requests",
                             "can", "PySide6", "cansniff.sources"}
        for path in MODULES:
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = (node.func.id if isinstance(node.func, ast.Name)
                            else node.func.attr if isinstance(node.func, ast.Attribute)
                            else "")
                    if name in forbidden_calls:
                        violations.append((path, node.lineno, name))
                if isinstance(node, ast.Import):
                    names = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    names = []
                if any(any(name == forbidden or name.startswith(forbidden + ".")
                           for forbidden in forbidden_imports) for name in names):
                    violations.append((path, node.lineno, names))
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
