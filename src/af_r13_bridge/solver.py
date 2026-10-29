"""R13 native solver jobs and strict completion evidence.

Selected validated business parsers are maintained in this package. Provenance
and the previous validation scope are recorded in docs/provenance-solver.json.
Process ownership uses this Bridge's Windows Job Object and evidence facilities.
No GUI process is ever admitted to or terminated by a native solver job.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

from .evidence import Evidence, hash_file
from .isolation import sanitized_environment
from .models import BridgeError
from .supervisor import WindowsWorkerJob

INSTALL_BIN = Path('D:/Program Files/AutoForm/AFplus/R13F/bin')
PROGRAM_END_RE = re.compile(r"Program END[^\[]*\[(?P<pid>\d+)\s+(?P<code>-?\d+)\.?\]")

def program_end_evidence(text):
    """Every raw END marker must parse and every parsed native code must be 0."""
    markers = []
    raw_count = 0
    for line_number, line in enumerate(text.splitlines(), 1):
        raw_count += len(re.findall(r"\bProgram\s+END\b", line, re.IGNORECASE))
        for match in PROGRAM_END_RE.finditer(line):
            markers.append({"line": line_number, "pid": int(match.group("pid")),
                            "code": int(match.group("code")), "text": line.strip()})
    codes = [marker["code"] for marker in markers]
    return {"ok": bool(codes and raw_count == len(markers) and all(code == 0 for code in codes)),
            "codes": codes, "markers": markers, "raw_marker_count": raw_count,
            "unparsed_marker_count": raw_count-len(markers)}


def solver_completion_evidence(text):
    """Prove executed Springback using the latest recording's native log grammar.

    The latest manual success (frame_00139, 138.002 s) shows the exact `nl:`
    convergence, Springback process-step summary, `ct:` success, and END form.
    A planned/disabled Springback name or its appearance in input does not count.
    Evidence is ordered within one END-delimited output segment; service log
    repetition is retained, and a later nonzero/malformed END rejects the run.
    """
    ends = program_end_evidence(text)
    starts, summaries, convergence, finishes, fatal_errors = [], [], [], [], []
    for number, line in enumerate(text.splitlines(), 1):
        start = re.match(r'^\s*ct:\s*Start of process step\b.*?"([^"]+)"', line, re.IGNORECASE)
        summary = re.match(r'^\s*Process step\s*\.{2,}\s*:\s*"([^"]+)"', line, re.IGNORECASE)
        converged = re.match(r'^\s*nl:\s*Springback solution converged in\s+([1-9]\d*)\s+iterations?\.\s*$', line, re.IGNORECASE)
        finished = re.match(r'^\s*ct:\s*Simulation successfully finished\.\s*$', line, re.IGNORECASE)
        if start:
            starts.append({"line": number, "name": start.group(1), "text": line.strip()})
        if summary:
            summaries.append({"line": number, "name": summary.group(1), "text": line.strip()})
        if converged:
            convergence.append({"line": number, "iterations": int(converged.group(1)), "text": line.strip()})
        if finished:
            finishes.append({"line": number, "text": line.strip()})
        if re.match(r'^\s*(?:[a-z]+:\s*)?(?:ERROR:|FATAL(?:\s+ERROR)?\b)', line, re.IGNORECASE):
            fatal_errors.append({"line": number, "text": line.strip()})
    completed = []
    for record in convergence:
        previous_end = max((marker["line"] for marker in ends["markers"] if marker["line"] < record["line"]), default=0)
        following_end = next((marker for marker in ends["markers"] if marker["line"] > record["line"]), None)
        if following_end is None:
            continue
        success = next((finish for finish in finishes if record["line"] < finish["line"] < following_end["line"]), None)
        if success is None:
            continue
        active_starts = [step for step in starts if previous_end < step["line"] < record["line"]]
        active = active_starts[-1] if active_starts else None
        summary = next((step for step in summaries if record["line"] < step["line"] < success["line"]
                        and re.search(r'\bSpringback\b', step["name"], re.IGNORECASE)), None)
        context = active if active and re.search(r'\bSpringback\b', active["name"], re.IGNORECASE) else summary
        if context is not None:
            completed.append({"convergence": record, "process_step": context,
                              "simulation_finished": success, "program_end": following_end})
    missing = []
    if not convergence:
        missing.append("springback_convergence")
    if not completed:
        missing.append("ordered_springback_completion")
    if not ends["ok"]:
        missing.append("all_program_end_zero")
    if fatal_errors:
        missing.append("no_fatal_errors")
    return {"ok": bool(completed and ends["ok"] and not fatal_errors),
            "springback_completed": bool(completed and not fatal_errors),
            "convergence_records": convergence, "completed_sequences": completed,
            "program_end_codes": ends["codes"], "program_end_validation": ends,
            "fatal_errors": fatal_errors, "missing": missing,
            "scope": "current_run_native_solver_stdout", "numeric_springback_field_read": False}


def native_geometry_evidence(text):
    """Verify simulation-ready geometry from this run's native kinematic check.

    This is engine acceptance of mesh and both tools, not an independent reading
    of every CAD edge or a product-tolerance assessment.
    """
    mesh = [int(n) for n in re.findall(r"New number of elements:\s*(\d+)", text)]
    mesh += [int(n) for n in re.findall(r"InitialElements\s+\S+\s+(\d+)", text)]
    tools = re.findall(r'Tool center initial coordinates of "([^"]+)"', text)
    evidence = {"operation_c20": 'Initializing operation "C-20"' in text,
                "blank_material": "Blank material data" in text,
                "positive_blank_mesh": any(n > 0 for n in mesh),
                "both_tools_initialized": {"凸模", "凹模"}.issubset(set(tools)),
                "fatal_input_errors": bool(re.search(r"(?im)^\s*(?:ERROR:|Error:|FATAL)", text)),
                "element_counts": mesh, "tool_names": sorted(set(tools)),
                "topological_closure_read_directly": False,
                "scope": "native_simulation_input_acceptance"}
    evidence["ok"] = all(evidence[k] for k in ("operation_c20", "blank_material", "positive_blank_mesh", "both_tools_initialized")) and not evidence["fatal_input_errors"]
    return evidence


def _write_state(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + '.writing')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def status_job(work_dir: str | Path) -> dict:
    """Observe one native invocation, never infer ownership from a live PID."""
    path = Path(work_dir).resolve() / 'native-job.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    if value.get('kind') != 'af_r13_bridge.native_solver' or value.get('work_dir') != str(path.parent):
        raise BridgeError('invalid_solver_job', 'Not a native job created in this directory.')
    return value


def cancel_job(work_dir: str | Path, job_id: str) -> dict:
    """Request cooperative cancellation; the owning runner stops its private job.

    This does not cancel GUI-submitted jobs and does not select any PID to kill.
    A return of requested is not confirmation of cancellation; poll status_job.
    """
    directory = Path(work_dir).resolve()
    state = status_job(directory)
    if state.get('job_id') != job_id:
        raise BridgeError('solver_job_mismatch', 'Cancellation job_id does not match the recorded invocation.')
    if state.get('status') not in {'starting', 'running'}:
        return {'ok': False, 'job_id': job_id, 'status': state.get('status'), 'cancel_requested': False,
                'error': 'job_not_running'}
    marker = directory / 'cancel-request.json'
    payload = {'job_id': job_id, 'requested_at_utc': datetime.now(timezone.utc).isoformat()}
    if not marker.exists():
        with marker.open('x', encoding='utf-8') as stream:
            json.dump(payload, stream)
    return {'ok': True, 'job_id': job_id, 'cancel_requested': True, 'cancelled': False,
            'scope': 'request_only; owning native runner must confirm its job has exited'}


def _cancel_requested(path: Path, job_id: str) -> bool:
    if not path.exists():
        return False
    try:
        return json.loads(path.read_text(encoding='utf-8')).get('job_id') == job_id
    except (OSError, ValueError):
        return False


def run_job(project: str | Path, *, check: bool = True, threads: int = 4,
            license_server: str | None = None, timeout: float = 900,
            work_dir: str | Path | None = None, cancel_event: threading.Event | None = None) -> dict:
    """Run a copied AFD with a fresh manifest and a private contained process tree."""
    source = Path(project).resolve(strict=True)
    directory = Path(work_dir).resolve() if work_dir is not None else source.parent
    if source.suffix.casefold() != '.afd' or not source.is_file():
        raise BridgeError('invalid_solver_input', 'Native solver input must be an existing AFD.')
    if not source.is_relative_to(directory):
        raise BridgeError('solver_input_not_owned', 'Native job input must be its working-directory copy.')
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise BridgeError('invalid_solver_timeout', 'Native solver timeout must be finite and positive.')
    if not isinstance(threads, int) or isinstance(threads, bool) or not 1 <= threads <= 64:
        raise BridgeError('invalid_solver_threads', 'Solver threads must be an integer from 1 to 64.')
    executable = INSTALL_BIN / 'AFFormingJob.exe'
    if not executable.is_file():
        raise BridgeError('solver_installation_missing', 'AFFormingJob.exe was not found.', path=str(executable))
    license_value = license_server or os.environ.get('AF_LICENSE_SERVER') or '2375@127.0.0.1'
    if not isinstance(license_value, str) or not license_value.strip():
        raise BridgeError('invalid_license_server', 'A nonempty license server is required.')
    license_value = license_value.strip().replace('@localhost', '@127.0.0.1')
    command = [str(executable), '-jn', str(source), '-puse', str(threads)]
    if check:
        command.append('-check')
    command.extend(['-ls', license_value])
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / 'native-job.json'
    if any((directory / name).exists() for name in ('native-job.json', 'stdout.log', 'stderr.log', 'cancel-request.json')):
        raise BridgeError('solver_output_exists', 'A native invocation needs fresh output paths.')
    evidence = Evidence(directory)
    job_id = uuid.uuid4().hex
    state = {'kind': 'af_r13_bridge.native_solver', 'job_id': job_id, 'status': 'starting',
             'ok': False, 'check': check, 'project': str(source), 'work_dir': str(directory),
             'command': command, 'source_sha256': hash_file(source), 'executable_sha256': hash_file(executable),
             'created_at_utc': datetime.now(timezone.utc).isoformat(), 'pid': None,
             'timed_out': False, 'cancelled': False, 'workers_stopped': False,
             'application_terminated': False, 'cancellation_scope': 'this private native process tree only'}
    _write_state(manifest, state)
    environment = sanitized_environment()
    environment['AF_LICENSE_SERVER'] = license_value
    environment['PATH'] = str(INSTALL_BIN) + os.pathsep + environment.get('PATH', '')
    stdout_path, stderr_path = directory / 'stdout.log', directory / 'stderr.log'
    state.update(stdout_path=str(stdout_path), stderr_path=str(stderr_path))
    started = time.monotonic()
    process = None
    job = None
    try:
        if os.name != 'nt':
            raise BridgeError('unsupported_platform', 'Native R13 solver execution requires Windows.')
        job = WindowsWorkerJob(evidence, job_id)
        with stdout_path.open('xb') as stdout, stderr_path.open('xb') as stderr:
            process = subprocess.Popen(command, cwd=directory, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, creationflags=WindowsWorkerJob.creation_flags)
            job.assign_and_resume(process)
            state.update(status='running', pid=process.pid)
            _write_state(manifest, state)
            evidence.append('native_solver_started', job_id=job_id, command=command, pid=process.pid)
            while process.poll() is None:
                job.refresh()
                if (cancel_event is not None and cancel_event.is_set()) or _cancel_requested(directory / 'cancel-request.json', job_id):
                    state.update(cancelled=True, status='cancelling')
                    break
                if time.monotonic() - started >= timeout:
                    state.update(timed_out=True, status='timing_out')
                    break
                time.sleep(.1)
            if state['cancelled'] or state['timed_out']:
                _write_state(manifest, state)
                job.terminate_and_confirm()
            process.wait(timeout=5)
            state['exit_code'] = process.returncode
        # Confirm all descendants gone even when the executable exited normally.
        job.terminate_and_confirm()
        state['workers_stopped'] = True
        stdout_text = stdout_path.read_text(encoding='utf-8', errors='replace')
        ends = program_end_evidence(stdout_text)
        state.update(program_end_validation=ends,
                     program_end=ends['markers'][-1] if ends['markers'] else None,
                     program_end_codes=ends['codes'], stdout_sha256=hash_file(stdout_path),
                     stderr_sha256=hash_file(stderr_path), final_project_sha256=hash_file(source))
        state['ok'] = bool(state['exit_code'] == 0 and ends['ok'] and not state['timed_out'] and not state['cancelled'])
        state['status'] = ('cancelled' if state['cancelled'] else 'timed_out' if state['timed_out']
                           else 'completed' if state['ok'] else 'failed')
    except BaseException as error:
        state.update(ok=False, status='failed', error={'type': type(error).__name__, 'message': str(error)})
        if isinstance(error, KeyboardInterrupt):
            state.update(cancelled=True, status='cancelled')
        if job is not None:
            job.terminate_and_confirm()
        if process is not None:
            if process.poll() is None:
                process.kill()  # only the exact Popen before/after failed Job admission
            process.wait(timeout=5)
            state['exit_code'] = process.returncode
        state['workers_stopped'] = True
    finally:
        state['elapsed_seconds'] = round(time.monotonic() - started, 3)
        _write_state(manifest, state)
        evidence.append('native_solver_finished', job_id=job_id, status=state['status'],
                        workers_stopped=state['workers_stopped'], application_terminated=False)
    return state


def solve(project: str | Path, out: str | Path, timeout: float = 900,
          license_server: str | None = None, cancel_event: threading.Event | None = None) -> dict:
    """Check and solve separate copies; leave the GUI original byte-for-byte intact."""
    source = Path(project).resolve(strict=True)
    directory = Path(out).resolve()
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise BridgeError('invalid_solver_timeout', 'Total check/solve timeout must be finite and positive.')
    directory.mkdir(parents=True, exist_ok=False)
    original_hash = hash_file(source)
    started = time.monotonic()
    check_dir = directory / 'input-check'
    check_dir.mkdir()
    check_copy = check_dir / source.name
    shutil.copy2(source, check_copy)
    if hash_file(check_copy) != original_hash:
        raise BridgeError('solver_copy_mismatch', 'The input-check copy differs from the GUI original.')
    check_result = run_job(check_copy, check=True, threads=2, timeout=min(timeout, 180),
                           work_dir=check_dir, license_server=license_server, cancel_event=cancel_event)
    text = Path(check_result['stdout_path']).read_text(encoding='utf-8', errors='replace') if check_result.get('stdout_path') and Path(check_result['stdout_path']).is_file() else ''
    geometry = native_geometry_evidence(text)
    geometry.update(ok=bool(geometry['ok'] and check_result['ok']), check=check_result,
                    program_end_validation=program_end_evidence(text))
    result = {'ok': False, 'geometry_input_validation': geometry, 'springback': False,
              'numeric_springback_field_read': False, 'source_project': str(source),
              'source_sha256': original_hash, 'file_roles': {'gui_original': str(source), 'input_check_copy': str(check_copy)},
              'gui_results_updated_by_native_copy': False}
    if geometry['ok']:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            result['error'] = 'NATIVE_TOTAL_TIMEOUT'
        else:
            solve_dir = directory / 'full-solve'
            solve_dir.mkdir()
            working = solve_dir / source.name
            shutil.copy2(source, working)
            if hash_file(working) != original_hash:
                raise BridgeError('solver_copy_mismatch', 'The solve copy differs from the GUI original.')
            execution = run_job(working, check=False, threads=4, timeout=remaining,
                                work_dir=solve_dir, license_server=license_server, cancel_event=cancel_event)
            text = Path(execution['stdout_path']).read_text(encoding='utf-8', errors='replace') if execution.get('stdout_path') and Path(execution['stdout_path']).is_file() else ''
            completion = solver_completion_evidence(text)
            result.update(ok=bool(execution['ok'] and completion['ok']), job_id=execution['job_id'],
                          native_job=execution, solver_completion_evidence=completion,
                          springback=completion['springback_completed'], program_end_codes=completion['program_end_codes'],
                          program_end=execution.get('program_end'))
            result['file_roles']['native_solve_copy'] = str(working)
            if not result['ok']:
                result['error'] = 'SOLVER_END_OR_SPRINGBACK_MISSING'
    else:
        result['error'] = 'NATIVE_GEOMETRY_INPUT_CHECK_FAILED'
    result['source_unchanged'] = hash_file(source) == original_hash
    result['ok'] = bool(result['ok'] and result['source_unchanged'])
    if not result['source_unchanged']:
        result['error'] = 'GUI_ORIGINAL_CHANGED_DURING_NATIVE_SOLVE'
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    _write_state(directory / 'solver-result.json', result)
    return result
