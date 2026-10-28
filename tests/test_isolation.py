"""Offline isolation tests: no application process or UI is accessed."""

import os
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from af_r13_bridge.isolation import (
    _pth_audit, audit_python_sources, forbidden_path, isolation_report, sanitized_environment,
)


class IsolationTests(unittest.TestCase):
    def scan(self, source):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "sample.py").write_text(source, encoding="utf-8")
            return audit_python_sources(directory)

    def test_child_environment_removes_case_variants_without_mutating_parent(self):
        original = {"PYTHONPATH": "reference", "PythonHome": "reference", "PATH": "kept"}
        result = sanitized_environment(original)
        self.assertEqual(original["PYTHONPATH"], "reference")
        self.assertNotIn("PYTHONPATH", result)
        self.assertNotIn("PythonHome", result)
        self.assertEqual(result["PATH"], "kept")
        self.assertEqual(result["PYTHONNOUSERSITE"], "1")

    def test_path_checks_directory_boundary(self):
        self.assertTrue(forbidden_path("D:/ai库/autoform自动化/af_recorder"))
        self.assertFalse(forbidden_path("D:/ai库/autoform自动化-clean"))

    def test_normal_and_aliased_dynamic_reference_imports_are_rejected(self):
        findings = self.scan("import af_workflow\nfrom af_recorder import worker\n"
                             "from importlib import import_module as load\nload('autoform_bridge.api')\n")
        self.assertEqual([x["code"] for x in findings].count("forbidden_import"), 2)
        self.assertIn("forbidden_dynamic_import", [x["code"] for x in findings])

    def test_subprocess_constant_and_concatenated_old_command_rejected(self):
        findings = self.scan("import subprocess as child\n"
                             "PACKAGE = 'af_' + 'workflow'\n"
                             "child.run(['python', '-m', PACKAGE])\n")
        self.assertEqual(findings[0]["code"], "reference_program_invocation")

    def test_comments_docstrings_and_reference_constants_not_runtime_dependencies(self):
        findings = self.scan('''"""Reference D:/ai库/autoform自动化 and af_workflow only as historical context."""
# import autoform_bridge
REFERENCE = "D:/ds库/autoform接口"
import pathlib
''')
        self.assertEqual(findings, [])

    def test_dynamic_unknown_import_fails_closed(self):
        findings = self.scan("import importlib\ndef run(name):\n    return importlib.import_module(name)\n")
        self.assertEqual(findings[0]["code"], "unresolved_dynamic_import")

    def test_path_injection_and_old_run_path_rejected(self):
        findings = self.scan("import sys, runpy\n"
                             "OLD = 'D:/ds库/autoform接口'\n"
                             "sys.path.insert(0, OLD)\nrunpy.run_path(OLD + '/main.py')\n")
        self.assertEqual([x["code"] for x in findings], ["reference_path_injection"] * 2)

    def test_new_supervisor_command_allowed(self):
        findings = self.scan("import subprocess\nsubprocess.run(['python', '-I', '-m', 'af_r13_bridge'])\n")
        self.assertEqual(findings, [])

    def test_runtime_module_with_benign_name_still_rejects_reference_origin(self):
        module = types.ModuleType("benign_test_module")
        module.__file__ = "D:/ai库/autoform自动化/something.py"
        with patch.dict(sys.modules, {module.__name__: module}):
            report = isolation_report()
        self.assertFalse(report["ok"])
        matches = [row for row in report["findings"] if row["code"] == "reference_loaded_module"]
        self.assertTrue(any(row["module"] == module.__name__ for row in matches))

    def test_namespace_package_without_file_cannot_hide_reference_path(self):
        module = types.ModuleType("benign_namespace")
        module.__path__ = ["D:/ds库/autoform接口"]
        with patch.dict(sys.modules, {module.__name__: module}):
            report = isolation_report()
        self.assertTrue(any(row.get("module") == module.__name__
                            and row["code"] == "reference_loaded_module"
                            for row in report["findings"]))

    def test_pth_code_and_path_injections_report_exact_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "injected.pth").write_text(
                "# ignored\nD:/ai库/autoform自动化\nimport af_recorder\n", encoding="utf-8")
            records, findings = _pth_audit([directory])
        self.assertEqual(len(records), 2)
        self.assertEqual([row["line"] for row in findings], [2, 3])

    def test_default_project_root_is_independent_venv_parent(self):
        report = isolation_report()
        self.assertEqual(Path(report["project_root"]), Path(sys.prefix).parent)
        self.assertEqual(report["project_root_source"], "independent_venv_parent")
        self.assertNotIn("project_root_unverified", [row["code"] for row in report["findings"]])

    def test_unmarked_project_root_is_not_reported_as_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            report = isolation_report(directory, require_venv=False)
        self.assertFalse(report["ok"])
        self.assertIn("project_root_unverified", [row["code"] for row in report["findings"]])

    def test_installed_wheel_report_defaults_work_from_unrelated_directory(self):
        # -I removes cwd and PYTHONPATH import resolution, so this specifically
        # exercises the installed package rather than the source checkout.
        program = (
            "import json,af_r13_bridge; from af_r13_bridge.isolation import isolation_report; "
            "r=isolation_report(); print(json.dumps({'ok':r['ok'],'findings':r['findings'],"
            "'project_root':r['project_root'],'package_file':af_r13_bridge.__file__}))"
        )
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run([sys.executable, "-I", "-c", program], cwd=directory,
                                       env=sanitized_environment(), capture_output=True,
                                       text=True, encoding="utf-8", timeout=20, check=True)
        report = json.loads(completed.stdout)
        self.assertTrue(Path(report["package_file"]).is_relative_to(Path(sys.prefix)))
        self.assertEqual(Path(report["project_root"]), Path(sys.prefix).parent)
        self.assertTrue(report["ok"], report["findings"])

    def test_symlink_to_forbidden_directory_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "reference"
            destination.mkdir()
            link = root / "linked"
            try:
                link.symlink_to(destination, target_is_directory=True)
            except OSError:
                self.skipTest("Host does not permit creating directory symlinks")
            self.assertTrue(forbidden_path(link / "module.py", roots=[destination]))


if __name__ == "__main__":
    unittest.main()
