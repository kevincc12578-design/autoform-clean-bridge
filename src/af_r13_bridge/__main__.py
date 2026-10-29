"""JSON command line. Real UI actions require an explicit --live switch."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import uuid

from .api import Bridge, capabilities, error_result
from .isolation import isolation_report
from .models import BridgeError, Result


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise BridgeError("invalid_arguments", message)


def _project_root() -> Path:
    candidates = [Path(sys.prefix).parent, *Path(__file__).resolve().parents]
    for candidate in candidates:
        if (candidate / "pyproject.toml").is_file() and (candidate / "src" / "af_r13_bridge").is_dir():
            return candidate
    raise BridgeError("project_root_missing", "Cannot locate this independent project's source and environment.")


def _adapter_factory(directory: Path, **options):
    from .adapters.windows import WindowsAdapter
    return WindowsAdapter(directory, **options)


def _discover():
    from .adapters.native import discover
    return discover()


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _fresh_directory(value: str | None) -> Path:
    if not isinstance(value, str) or not value:
        raise BridgeError("invalid_request", "An output directory is required.")
    directory = Path(value).resolve()
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise BridgeError("output_exists", "The output directory must be new; existing evidence is never overwritten.", path=str(directory)) from error
    return directory


def _request(path: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise BridgeError("invalid_request", "Unable to read request JSON.", detail=str(error)) from error
    if not isinstance(value, dict):
        raise BridgeError("invalid_request", "Request JSON must be an object.")
    return value


def _parser() -> Parser:
    parser = Parser(prog="python -m af_r13_bridge")
    commands = parser.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit", help="Read-only dependency and source isolation audit")
    audit.add_argument("--out", required=True)
    commands.add_parser("capabilities", help="Implementation status; no UI access")
    catalog = commands.add_parser("catalog", help="Read-only official help index; no UI access or execution")
    catalog.add_argument("--query", default="")
    catalog.add_argument("--source", help="Optional helpLinks.cfg path; defaults to the local R13 installation")
    catalog.add_argument("--out", help="Optional new evidence directory")
    discovery = commands.add_parser("discover", help="Read-only R13 process/window discovery")
    discovery.add_argument("--out")
    chain=commands.add_parser('chain',help='Owned baogu business workflow including same-file persistence and final close')
    chain.add_argument('--workflow',choices=['baogu'],default='baogu')
    chain.add_argument('--runs',type=int,default=3)
    chain.add_argument('--out',required=True)
    chain.add_argument('--part')
    chain.add_argument('--material')
    chain.add_argument('--until',help='Development only, single run; never full acceptance')
    chain.add_argument('--live',action='store_true')
    chain.add_argument('--launch',action='store_true')
    chain.add_argument('--license-server')
    chain.add_argument('--allow-selected-license',action='store_true')
    for name in ("lifecycle", "batch", "call"):
        entry = commands.add_parser(name)
        if name == "call":
            entry.add_argument("operation")
        if name != "lifecycle":
            entry.add_argument("--request", required=True)
        entry.add_argument("--out")
        entry.add_argument("--live", action="store_true")
        entry.add_argument("--launch", action="store_true")
        entry.add_argument("--license-server")
        entry.add_argument("--allow-selected-license", action="store_true")
        if name == "lifecycle":
            entry.add_argument("--filename", default="lifecycle.afd")
    return parser


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    directory: Path | None = None
    result: dict | None = None
    operation = "cli"
    adapter = None
    bridge = None
    root = None
    before = None
    try:
        args = _parser().parse_args(argv)
        operation = args.command
        if args.command == "capabilities":
            result = Result(True, "capabilities", uuid.uuid4().hex, data=capabilities(),
                            verification_scope="Implementation declarations; no UI access.").to_dict()
        elif args.command == "catalog":
            from .catalog import query_catalog
            directory = _fresh_directory(args.out) if args.out else None
            data = query_catalog(args.query, args.source)
            result = Result(True, "catalog", uuid.uuid4().hex, data=data,
                            verification_scope=data["verification_scope"]).to_dict()
        elif args.command=='chain':
            if not args.live:
                raise BridgeError('live_opt_in_required','Real business operations require --live')
            directory=_fresh_directory(args.out)
            from .workflow import run_chain,DEFAULT_PART,DEFAULT_MATERIAL
            data=run_chain(directory,runs=args.runs,part=args.part or DEFAULT_PART,material=args.material or DEFAULT_MATERIAL,
                launch=args.launch,license_server=args.license_server,allow_selected_license=args.allow_selected_license,until=args.until)
            result=Result(data['ok'],'chain',uuid.uuid4().hex,data=data,
                error=None if data['ok'] else {'code':'workflow_incomplete','development_ok':data.get('development_ok',False)},
                verification_scope='Frozen baogu workflow; full success requires all stages, native Springback and same-file close/reopen/close.').to_dict()
        elif args.command in {"audit", "discover"}:
            directory = _fresh_directory(args.out) if args.out else None
            root = _project_root()
            before = isolation_report(root)
            if directory:
                _write(directory / "isolation-before.json", before)
            if not before["ok"]:
                raise BridgeError("isolation_failed", "The current process failed dependency isolation checks.", findings=before["findings"])
            if args.command == "audit":
                result = Result(True, "audit", uuid.uuid4().hex, data=before,
                                verification_scope=before["verification_scope"]).to_dict()
            else:
                result = Result(True, "discover", uuid.uuid4().hex, data={"processes": _discover()},
                                verification_scope="Read-only process identity and top-level window metadata; no design ownership.").to_dict()
        else:
            request = _request(args.request) if hasattr(args, "request") else {}
            operation = args.operation if args.command == "call" else args.command
            if operation not in {"lifecycle", "batch"}:
                known_individual = {"attach", "create_design", "save_design", "close_design", "reopen_design", "readback", "cleanup", "disconnect", "list_designs"}
                from .api import BUSINESS_OPERATIONS
                code = "session_required" if operation in known_individual | BUSINESS_OPERATIONS else "not_implemented"
                raise BridgeError(code, "Use one batch process for individual owned-design operations." if code == "session_required" else "This operation is not implemented in the new Bridge.", operation=operation)
            if not args.live:
                raise BridgeError("live_opt_in_required", "Real application actions require --live on the command line; request JSON cannot grant it.")
            directory = _fresh_directory(args.out or request.get("out"))
            _write(directory / "request.json", {"operation": operation, "request": request, "arguments": vars(args)})
            root = _project_root()
            before = isolation_report(root)
            _write(directory / "isolation-before.json", before)
            if not before["ok"]:
                raise BridgeError("isolation_failed", "Refusing UI startup because dependency isolation failed.", findings=before["findings"])
            adapter = _adapter_factory(directory,
                launch=bool(args.launch or request.get("launch", False)),
                license_server=args.license_server or request.get("license_server"),
                allow_selected_license=bool(args.allow_selected_license or request.get("allow_selected_license", False)))
            bridge = Bridge(adapter, directory)
            if operation == "lifecycle":
                filename = request.get("filename", getattr(args, "filename", "lifecycle.afd"))
                result = bridge.call("lifecycle", {"filename": filename})
            else:
                result = bridge.batch(request.get("operations"))
    except (Exception, KeyboardInterrupt) as error:
        result = error_result(operation, error, elapsed_ms=(time.monotonic() - started) * 1000)
    finally:
        # Constructor failures remain the adapter's responsibility; once a
        # reference exists, shutdown is guaranteed even if Bridge setup failed.
        try:
            if bridge is not None and not bridge.lifecycle.disconnected:
                try:
                    if bridge.lifecycle.baseline is not None:
                        cleaned = bridge.lifecycle.cleanup()
                        if not cleaned["cleanup_ok"]:
                            result = result or error_result(operation, BridgeError("cleanup_failed", "Final cleanup was not verified."))
                            result["ok"] = False
                            result.setdefault("data", {})["final_cleanup"] = cleaned
                finally:
                    bridge.lifecycle.disconnect()
            elif adapter is not None and bridge is None:
                adapter.shutdown()
        except (Exception, KeyboardInterrupt) as error:
            result = result or error_result(operation, error)
            result["ok"] = False
            result["error"] = result.get("error") or error_result(operation, error)["error"]
        try:
            if root is not None:
                after = isolation_report(root)
                if directory:
                    _write(directory / "isolation-after.json", after)
                if not after["ok"]:
                    result = result or error_result(operation, BridgeError("isolation_failed", "Final dependency audit failed."))
                    result["ok"] = False
                    result["error"] = result.get("error") or {"code": "isolation_failed", "findings": after["findings"]}
                    if "lifecycle_ok" in result.get("data", {}):
                        result["data"]["lifecycle_ok"] = False
            result = result or error_result(operation, BridgeError("no_result", "Command produced no result."))
            result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            if directory:
                result.setdefault("evidence", []).extend(str(directory / name) for name in ("isolation-before.json", "isolation-after.json") if (directory / name).is_file())
                _write(directory / "result.json", result)
        except (Exception, KeyboardInterrupt) as error:
            result = result or error_result(operation, error)
            result["ok"] = False
            result["error"] = result.get("error") or {"code": "evidence_write_failed", "message": str(error)}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
