"""Static passive/untrusted boundary for project files."""

from __future__ import annotations

import ast
import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ProjectSecurityTests(unittest.TestCase):
    def test_project_backend_has_no_pickle_eval_exec_network_shell_or_can(self):
        violations = []
        root = os.path.join(ROOT, "cansniff", "investigation")
        for name in os.listdir(root):
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    call = node.func.id if isinstance(node.func, ast.Name) else (
                        node.func.attr if isinstance(node.func, ast.Attribute) else "")
                    if call in {"eval", "exec", "system", "popen", "urlopen", "Bus"}:
                        violations.append((name, node.lineno, call))
                if isinstance(node, ast.Import):
                    modules = [item.name for item in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    modules = []
                if any(module == "pickle" or module.startswith(("requests", "urllib",
                                                                 "socket", "subprocess", "can"))
                       for module in modules):
                    violations.append((name, node.lineno, modules))
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
