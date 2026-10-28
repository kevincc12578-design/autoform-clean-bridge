"""Auditable isolation checks; never imports or executes reference-project code."""

from __future__ import annotations

import ast
import importlib.metadata
import os
from pathlib import Path
import re
import site
import sys
from typing import Iterable, Mapping


FORBIDDEN_ROOTS = (
    "D:/ds库/autoform接口",
    "D:/ai库/autoform自动化",
)
FORBIDDEN_PACKAGES = frozenset({"af_workflow", "af_recorder", "autoform_bridge"})


def sanitized_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a child-only environment; never mutates os.environ."""
    values = os.environ if base is None else base
    result = {key: value for key, value in values.items()
              if key.upper() not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}}
    result["PYTHONNOUSERSITE"] = "1"
    result["PYTHONSAFEPATH"] = "1"
    return result


def _canonical(value: str | Path) -> str:
    # resolve follows filesystem symlinks and Windows directory junctions.
    return str(Path(value).resolve()).replace("\\", "/").rstrip("/").casefold()


def _inside(value: str | Path, parent: str | Path) -> bool:
    actual, root = _canonical(value), _canonical(parent)
    return actual == root or actual.startswith(root + "/")


def forbidden_path(value: str | Path, roots: Iterable[str | Path] = FORBIDDEN_ROOTS) -> bool:
    return any(_inside(value, root) for root in roots)


def _forbidden_text(value: str) -> bool:
    text = value.replace("\\", "/").casefold()
    return any(re.search(re.escape(root.casefold()) + r'''(?=$|[/\s"'<>])''', text)
               for root in FORBIDDEN_ROOTS)


def _qualified(node: ast.AST, aliases: Mapping[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        prefix = _qualified(node.value, aliases)
        return prefix + "." + node.attr
    return ""


def _literal(node: ast.AST, constants: Mapping[str, object]) -> object:
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [_literal(part, constants) for part in node.elts]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left, constants), _literal(node.right, constants)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
    return None


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def audit_python_sources(root: str | Path) -> list[dict]:
    """Inspect executable Python AST only, excluding comments and prose.

    This is a bounded static audit, not a proof against arbitrary obfuscation.
    Unresolved dynamic imports are rejected because their provenance is unknown.
    Normal subprocess calls are recorded by the runtime supervisor separately.
    """
    findings: list[dict] = []
    for candidate in Path(root).rglob("*"):
        if (candidate.is_symlink() or candidate.is_junction()) and forbidden_path(candidate):
            findings.append({"code": "reference_source_link", "path": str(candidate),
                             "resolved": str(candidate.resolve())})
    for path in sorted(Path(root).rglob("*.py")):
        def add(code: str, node: ast.AST | None, detail: str) -> None:
            findings.append({"code": code, "path": str(path.resolve()),
                             "line": getattr(node, "lineno", None), "detail": detail})
        if forbidden_path(path):
            add("source_resolves_to_reference", None, str(path.resolve()))
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except (OSError, SyntaxError) as error:
            add("source_unreadable", None, str(error))
            continue
        aliases: dict[str, str] = {}
        constants: dict[str, object] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for part in node.names:
                    aliases[part.asname or part.name.split(".")[0]] = (
                        part.name if part.asname else part.name.split(".")[0])
                    if part.name.split(".")[0] in FORBIDDEN_PACKAGES:
                        add("forbidden_import", node, part.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for part in node.names:
                    aliases[part.asname or part.name] = module + "." + part.name
                if module.split(".")[0] in FORBIDDEN_PACKAGES:
                    add("forbidden_import", node, module)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and node.value is not None:
                        constants[target.id] = _literal(node.value, constants)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _qualified(node.func, aliases)
            arguments = [_literal(arg, constants) for arg in node.args]
            arguments.extend(_literal(arg.value, constants) for arg in node.keywords)
            texts = _strings(arguments)
            if name in {"__import__", "builtins.__import__", "importlib.import_module"}:
                module = arguments[0] if arguments else None
                if not isinstance(module, str):
                    add("unresolved_dynamic_import", node, name)
                elif module.split(".")[0] in FORBIDDEN_PACKAGES:
                    add("forbidden_dynamic_import", node, module)
            if name in {"sys.path.insert", "sys.path.append", "sys.path.extend",
                        "site.addsitedir", "site.addpackage",
                        "importlib.util.spec_from_file_location", "runpy.run_path"}:
                if any(_forbidden_text(text) for text in texts):
                    add("reference_path_injection", node, name)
                elif not texts:
                    add("unresolved_path_injection", node, name)
            if name.startswith("subprocess.") or name in {"os.system", "os.popen",
                    "os.execv", "os.execve", "os.spawnv", "os.spawnve", "runpy.run_module"}:
                command = " ".join(texts)
                if _forbidden_text(command) or any(
                        package in command for package in FORBIDDEN_PACKAGES):
                    add("reference_program_invocation", node, command)
    return findings


def _pth_audit(directories: Iterable[str]) -> tuple[list[dict], list[dict]]:
    records, findings = [], []
    for directory in sorted(set(directories)):
        folder = Path(directory)
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.pth")):
            try:
                lines = path.read_text(encoding="utf-8-sig").splitlines()
            except (OSError, UnicodeError) as error:
                findings.append({"code": "pth_unreadable", "path": str(path), "detail": str(error)})
                continue
            for number, line in enumerate(lines, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                executable = line.startswith(("import ", "import\t"))
                resolved = None if executable else str((folder / line).resolve())
                records.append({"path": str(path.resolve()), "line": number,
                                "content": line, "executable": executable, "resolved": resolved})
                if (_forbidden_text(line) or
                    (resolved is not None and forbidden_path(resolved)) or
                    (executable and any(package in line for package in FORBIDDEN_PACKAGES))):
                    findings.append({"code": "reference_pth", "path": str(path.resolve()),
                                     "line": number, "detail": line})
    return records, findings


def isolation_report(project_root: str | Path | None = None, *, require_venv: bool = True) -> dict:
    """Read-only provenance report for this process and the new package source."""
    # Installed wheels live under .venv/Lib/site-packages: __file__.parents[2]
    # therefore identifies Lib, not the workspace. The independent environment
    # belongs directly to the project, and project markers must corroborate it.
    root_source = "explicit_argument" if project_root is not None else "independent_venv_parent"
    root = Path(project_root) if project_root is not None else Path(sys.prefix).parent
    root = root.resolve()
    findings: list[dict] = []
    if not (root / "pyproject.toml").is_file() or not (root / "src" / "af_r13_bridge").is_dir():
        findings.append({"code": "project_root_unverified", "path": str(root),
                         "root_source": root_source,
                         "detail": "Expected pyproject.toml and src/af_r13_bridge; pass the project_root explicitly."})
    runtime_paths = [{"entry": item, "resolved": str(Path(item or os.getcwd()).resolve())}
                     for item in sys.path]
    for item in runtime_paths:
        if forbidden_path(item["resolved"]):
            findings.append({"code": "reference_sys_path", **item})
    modules = []
    for name, module in sorted(list(sys.modules.items())):
        filename = getattr(module, "__file__", None)
        paths = list(getattr(module, "__path__", []))
        resolved = str(Path(filename).resolve()) if filename else None
        modules.append({"name": name, "file": resolved,
                        "namespace_paths": [str(Path(part).resolve()) for part in paths]})
        if name.split(".")[0] in FORBIDDEN_PACKAGES:
            findings.append({"code": "reference_module_name", "module": name, "path": resolved})
        for candidate in ([filename] if filename else []) + paths:
            if forbidden_path(candidate):
                findings.append({"code": "reference_loaded_module", "module": name,
                                 "path": str(Path(candidate).resolve())})
    cfg_path = Path(sys.prefix) / "pyvenv.cfg"
    config = {}
    if cfg_path.exists():
        for line in cfg_path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                config[key.strip().casefold()] = value.strip()
    expected_env = root / ".venv"
    if require_venv and (not _inside(sys.executable, expected_env) or
                         _canonical(sys.prefix) != _canonical(expected_env)):
        findings.append({"code": "unexpected_interpreter", "expected": str(expected_env),
                         "executable": sys.executable, "prefix": sys.prefix})
    if require_venv and config.get("include-system-site-packages", "").casefold() != "false":
        findings.append({"code": "system_site_packages_not_disabled", "config": config})
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        if os.environ.get(key):
            findings.append({"code": "python_environment_injection", "variable": key,
                             "value": os.environ[key]})
    directories = [item["resolved"] for item in runtime_paths]
    pth_records, pth_findings = _pth_audit(directories)
    findings.extend(pth_findings)
    source_root = root / "src" / "af_r13_bridge"
    findings.extend(audit_python_sources(source_root))
    if not source_root.is_dir():
        findings.append({"code": "source_root_missing", "path": str(source_root)})
    dependencies = sorted(({
        "name": dist.metadata.get("Name", "unknown"), "version": dist.version,
        "location": str(Path(dist.locate_file("")).resolve())
    } for dist in importlib.metadata.distributions()), key=lambda row: row["name"].casefold())
    for dependency in dependencies:
        if forbidden_path(dependency["location"]):
            findings.append({"code": "reference_distribution", **dependency})
    package = sys.modules.get("af_r13_bridge")
    package_file = getattr(package, "__file__", None)
    package_paths = [str(Path(path).resolve()) for path in getattr(package, "__path__", [])]
    if package_file is None and not package_paths:
        findings.append({"code": "package_not_loaded"})
    for candidate in ([package_file] if package_file else []) + package_paths:
        if not _inside(candidate, root):
            findings.append({"code": "package_outside_project", "path": str(candidate)})
    return {
        "ok": not findings,
        "verification_scope": "static Python AST plus current interpreter/module/path provenance",
        "limitations": ["Not a sandbox or a proof against arbitrary obfuscated runtime code.",
                        "Child commands must additionally be recorded by the process supervisor."],
        "project_root": str(root), "project_root_source": root_source, "executable": sys.executable,
        "prefix": sys.prefix, "base_prefix": sys.base_prefix,
        "isolated_mode": bool(sys.flags.isolated), "safe_path": bool(sys.flags.safe_path),
        "user_site_enabled": site.ENABLE_USER_SITE,
        "python_environment": {key: os.environ.get(key) for key in
                               ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONNOUSERSITE", "PYTHONSAFEPATH")},
        "venv_config": config, "sys_path": runtime_paths,
        "package_file": str(Path(package_file).resolve()) if package_file else None,
        "package_paths": package_paths, "loaded_modules": modules,
        "dependencies": dependencies, "pth_entries": pth_records, "findings": findings,
    }
