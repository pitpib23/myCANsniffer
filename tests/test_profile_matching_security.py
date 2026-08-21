"""Static local/passive boundary for profile matching."""

from __future__ import annotations

import ast
import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ProfileMatchingSecurityTests(unittest.TestCase):
    def test_matcher_has_no_qt_network_shell_hardware_or_execution_surface(self):
        path = os.path.join(ROOT, "cansniff", "analysis", "matching.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), path)
        forbidden_calls = {
            "eval", "exec", "system", "popen", "urlopen", "Bus",
            "send", "send_many", "send_periodic", "transmit",
        }
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                call = node.func.id if isinstance(node.func, ast.Name) else (
                    node.func.attr if isinstance(node.func, ast.Attribute) else "")
                if call in forbidden_calls:
                    violations.append((node.lineno, call))
            if isinstance(node, ast.Import):
                modules = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                modules = []
            if any(module == "can" or module.startswith((
                    "PySide6", "requests", "urllib", "socket", "subprocess",
                    "can."))
                   for module in modules):
                violations.append((node.lineno, modules))
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
