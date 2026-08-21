"""Static guardrails for the repository's receive-only boundary."""

from __future__ import annotations

import ast
import os
import unittest

from cansniff.sources import CanFrameSource
from cansniff.sources.live import LiveSource


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCTION = [os.path.join(ROOT, "main.py")]
for directory, _subdirs, names in os.walk(os.path.join(ROOT, "cansniff")):
    PRODUCTION.extend(os.path.join(directory, name)
                      for name in names if name.endswith(".py"))


class RepositorySafetyTests(unittest.TestCase):
    def test_no_transmit_calls_in_production_ast(self):
        forbidden = {
            "send", "send_many", "send_periodic", "sendto", "sendall",
            "transmit", "inject", "write_frame",
        }
        found = []
        for path in PRODUCTION:
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in forbidden):
                    found.append((os.path.relpath(path, ROOT), node.lineno,
                                  node.func.attr))
        self.assertEqual(found, [], "transmit-capable calls found: {!r}".format(found))

    def test_python_can_bus_is_constructed_only_in_live_source(self):
        found = []
        for path in PRODUCTION:
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (isinstance(func, ast.Attribute) and func.attr == "Bus") \
                        or (isinstance(func, ast.Name) and func.id == "Bus"):
                    found.append((os.path.relpath(path, ROOT), node.lineno))
        self.assertEqual(len(found), 1, "unexpected bus constructors: {!r}".format(found))
        self.assertEqual(found[0][0], os.path.join("cansniff", "sources", "live.py"))

    def test_source_interfaces_expose_no_transmit_api(self):
        for cls in (CanFrameSource, LiveSource):
            for name in ("send", "write", "transmit", "tx", "inject", "replay"):
                self.assertFalse(hasattr(cls, name),
                                 "{} exposes {}".format(cls.__name__, name))

    def test_protocol_analysis_has_no_source_qt_or_transmit_surface(self):
        root = os.path.join(ROOT, "cansniff", "analysis", "protocols")
        forbidden_functions = {
            "send", "write", "transmit", "tx", "inject", "probe", "request",
        }
        violations = []
        for directory, _subdirs, names in os.walk(root):
            for name in names:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(directory, name)
                with open(path, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read(), filename=path)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        modules = [node.module or ""]
                    else:
                        modules = []
                    if any("sources" in module or module.startswith("PySide6")
                           for module in modules):
                        violations.append((os.path.relpath(path, ROOT), node.lineno,
                                           modules))
                    if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and node.name.lower() in forbidden_functions):
                        violations.append((os.path.relpath(path, ROOT), node.lineno,
                                           node.name))
        self.assertEqual(violations, [],
                         "protocol analysis crossed passive boundary: {!r}".format(
                             violations))

    def test_diagnostic_analysis_exposes_no_active_client_surface(self):
        paths = [os.path.join(ROOT, "cansniff", "analysis", name)
                 for name in ("isotp.py", "uds.py", "diagnostics.py")]
        forbidden_names = {
            "send", "transmit", "write", "request_diagnostic",
            "send_uds_request", "send_flow_control", "emit_flow_control",
            "send_tester_present", "calculate_security_key",
            "generate_security_key", "brute_force_security_access",
        }
        violations = []
        for path in paths:
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    modules = []
                if any("sources" in module or module.startswith("PySide6")
                       or module == "can" for module in modules):
                    violations.append((os.path.relpath(path, ROOT), node.lineno,
                                       modules))
                if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef))
                        and node.name.lower() in forbidden_names):
                    violations.append((os.path.relpath(path, ROOT), node.lineno,
                                       node.name))
        self.assertEqual(
            violations, [],
            "diagnostic analysis crossed passive boundary: {!r}".format(
                violations))

    def test_reverse_engineering_has_no_source_qt_or_active_event_surface(self):
        root = os.path.join(ROOT, "cansniff", "analysis", "compare")
        forbidden_functions = {
            "send", "write", "transmit", "tx", "inject", "probe", "request",
            "generate_event", "replay",
        }
        violations = []
        for directory, _subdirs, names in os.walk(root):
            for name in names:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(directory, name)
                with open(path, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read(), filename=path)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        modules = [node.module or ""]
                    else:
                        modules = []
                    if any("sources" in module or module.startswith("PySide6")
                           for module in modules):
                        violations.append((os.path.relpath(path, ROOT), node.lineno,
                                           modules))
                    if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and node.name.lower() in forbidden_functions):
                        violations.append((os.path.relpath(path, ROOT), node.lineno,
                                           node.name))
        self.assertEqual(violations, [],
                         "comparison analysis crossed passive boundary: {!r}".format(
                             violations))


if __name__ == "__main__":
    unittest.main()
