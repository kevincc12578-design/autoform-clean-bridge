"""GUI Start, exact saved-design binding, and current-job completion evidence.

Selected validated operations; source records are in docs/provenance-solver.json.
This operation runs inside the new Bridge's bounded, owned business worker.
It neither owns nor closes a document and never terminates attached AutoForm.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

import uiautomation as auto

from .ui import common, dialogs, pages, uia, win32
from ..evidence import Evidence, hash_file
from ..isolation import sanitized_environment
from ..supervisor import WindowsWorkerJob
from ..solver import INSTALL_BIN

MAX_LOG_BYTES = 64 * 1024 * 1024

def _fail(error, **evidence):
    return {"ok":False,"error":error,**evidence}


def _controls(root, *, name=None, cls=None, kind=None):
    if root is None:
        return []
    result = []
    for control, _ in uia.walk(root):
        try:
            rect = uia.control_rect(control)
            if (control.IsOffscreen or not rect or rect[2] <= rect[0] or rect[3] <= rect[1]
                    or name is not None and control.Name.strip() != name
                    or cls is not None and control.ClassName != cls
                    or kind is not None and control.ControlTypeName != kind):
                continue
            result.append(control)
        except Exception:
            continue
    return result


def _one(root, **criteria):
    found = _controls(root,**criteria)
    return found[0] if len(found) == 1 else None


def _tab_selected(control):
    try:
        return bool(control.GetSelectionItemPattern().IsSelected)
    except Exception:
        pass
    try:
        value = int(control.GetTogglePattern().ToggleState)
        return bool(value) if value in (0,1) else None
    except Exception:
        return None


def _open_tab(session, name):
    page = _one(session.main_window,cls="AutoForm::SimulationPageView")
    tab = _one(page,name=name,cls="AFFixedFontSizeToolButton",kind="CheckBoxControl")
    if tab is None or not tab.IsEnabled:
        return _fail("SIMULATION_TAB_NOT_UNIQUE_OR_ENABLED",tab=name)
    if _tab_selected(tab) is True:
        return {"ok":True,"tab":name,"already_selected":True}
    action = common.click(session,tab)
    if not action.get("ok"):
        return action
    selected = common.wait_for(lambda: _tab_selected(tab) is True,timeout=5)
    return {"ok":bool(selected),"tab":name,"input":action,
            "error":None if selected else "SIMULATION_TAB_NOT_CONFIRMED"}


def _log_root():
    return Path(os.environ["LOCALAPPDATA"]) / "AutoForm/AFplus" / INSTALL_BIN.parent.name / "log"


def _normalize(value):
    # R13 diagnostics sign-extend individual UTF-8 bytes into U+FF80..U+FFFF.
    # Recover only reversible runs; raw bytes remain in the evidence directory.
    def recover(match):
        try:
            return bytes(ord(char) & 255 for char in match.group()).decode("utf-8")
        except UnicodeDecodeError:
            return match.group()
    return re.sub(r"[\uff80-\uffff]+",recover,str(value))


def _path_key(value):
    return _normalize(value).strip().strip('"').replace("\\","/").casefold()


def _decode(raw):
    encodings = ("utf-16",) if raw.startswith((b"\xff\xfe",b"\xfe\xff")) else ("utf-8-sig","gb18030")
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError("UNSUPPORTED_LOG_ENCODING")


def _snapshot(path):
    if not path.exists():
        return {"exists":False,"size":0}
    stat = path.stat()
    return {"exists":True,"size":stat.st_size,"ctime_ns":stat.st_ctime_ns,"mtime_ns":stat.st_mtime_ns}


def _read_raw(path, offset=0):
    with path.open("rb") as stream:
        stream.seek(offset)
        raw = stream.read(MAX_LOG_BYTES+1)
    if len(raw) > MAX_LOG_BYTES:
        raise ValueError("LOG_EXCEEDS_64_MIB")
    return raw


def _gui_delta(path, before):
    now = _snapshot(path)
    if not now["exists"]:
        return _fail("CURRENT_GUI_LOG_MISSING")
    if before["exists"] and (now["size"] < before["size"] or now["ctime_ns"] != before["ctime_ns"]):
        return _fail("CURRENT_GUI_LOG_ROTATED_OR_TRUNCATED")
    raw = _read_raw(path,before["size"])
    return {"ok":True,"raw":raw,"text":_normalize(_decode(raw)),"offset":before["size"]}


def current_job_evidence(gui_text, target_path):
    """Only a new GUI Started job command with this full path binds the job."""
    pattern = re.compile(r"Started job:\s*AFFormingJob\s+-jobId\s+"
                         r"(?P<name>SingleSim_[A-Za-z0-9]+)#(?P<id>[A-Za-z0-9-]+)"
                         r"\s+-jn\s+(?P<path>.*?)\s+-ls\s+[^\r\n]+")
    records = [{"name":m["name"],"id":m["id"],"source":_normalize(m["path"]).strip('"'),
                "command":_normalize(m.group()),"line":gui_text[:m.start()].count("\n")+1}
               for m in pattern.finditer(gui_text) if _path_key(m["path"]) == _path_key(target_path)]
    unique = {(r["name"],r["id"]) for r in records}
    if len(unique) != 1:
        return _fail("CURRENT_STARTED_JOB_NOT_UNIQUE",candidates=records)
    job = records[-1]
    saved = any(_path_key(line.split("#FILE_LOG#Save:",1)[1]) == _path_key(target_path)
                for line in gui_text.splitlines() if "#FILE_LOG#Save:" in line)
    return {"ok":saved,"job":job,"full_path_verified":True,"gui_save_verified":saved,
            "error":None if saved else "CURRENT_GUI_SAVE_PATH_NOT_CONFIRMED"}


def job_status_evidence(text, job, target_path):
    """Read exact job/path status; service Completed alone is insufficient."""
    normalized = _normalize(text)
    inputs = re.findall(r"Message: Input file:\s*([^\r\n]+)",normalized)
    source_ok = any(_path_key(value) == _path_key(target_path) for value in inputs)
    statuses = []
    matches = list(re.finditer(r"<singleSimJobStatus\b[^>]*>.*?</singleSimJobStatus>",normalized,re.S))
    starts = list(re.finditer(r"<singleSimJobStatus\b",normalized))
    incomplete = bool(starts and (not matches or starts[-1].start() > matches[-1].start()))
    for match in matches:
        try:
            node = ET.fromstring(match.group())
        except ET.ParseError:
            incomplete = True
            continue
        if (node.findtext("jobId_R11/id") != job["id"]
                or node.findtext("jobId_R11/name") != job["name"]
                or _path_key(node.findtext("fileName","")) != _path_key(target_path)):
            continue
        statuses.append({key:node.findtext(key) for key in ("status","failing","canceling","check")})
    latest = statuses[-1] if statuses else None
    completed_signal = bool(re.search(r"sig(?:JobStatus)?Changed\s+Completed\s+"+re.escape(job["id"])+r"\b",normalized))
    terminal_failure = bool(latest and (latest["failing"] == "true" or latest["canceling"] == "true"
                                       or latest["status"] in {"Failed","Canceled","Cancelled"}))
    completed = bool(source_ok and latest and not incomplete and completed_signal
                     and latest["status"] in {"Calculated","Completed"}
                     and all(latest[k] == "false" for k in ("failing","canceling","check")))
    return {"ok":source_ok and bool(statuses),"job_id":job["id"],"source_verified":source_ok,
            "status":latest,"incomplete_status":incomplete,"completed":completed,
            "terminal_failure":terminal_failure,"native_solver_completion_verified":False}


def _read_active_log(main_hwnd, pid):
    """Read-only sidecar body; never focus, click, or enumerate hidden designs."""
    from .ui import WindowScopedControl, client_bounds
    info = win32.window_info(main_hwnd)
    if info.pid != pid or not info.visible:
        return _fail("LOG_READER_MAIN_WINDOW_MISMATCH")
    main = WindowScopedControl(auto.ControlFromHandle(main_hwnd),client_bounds(main_hwnd))
    page = _one(main,cls="AutoForm::SimulationPageView")
    if page is None:
        return _fail("ACTIVE_SIMULATION_PAGE_NOT_UNIQUE")
    candidates = []
    for control, _ in uia.walk(page):
        try:
            if control.IsOffscreen:
                continue
            if (control.ControlTypeName not in {"EditControl","DocumentControl"}
                    and control.ClassName not in {"QTextEdit","QPlainTextEdit","QTextBrowser"}):
                continue
            texts = []
            try:
                text = control.GetTextPattern().DocumentRange.GetText(-1)
                if isinstance(text,str) and text:
                    texts.append(("text_pattern",text))
            except Exception:
                pass
            try:
                text = control.GetValuePattern().Value
                if isinstance(text,str) and text:
                    texts.append(("value_pattern",text))
            except Exception:
                pass
            # Prefer an actual multiline TextPattern over a flattened Value.
            texts.sort(key=lambda pair:("\n" in pair[1],pair[0] == "text_pattern"),reverse=True)
            if texts:
                method,text = texts[0]
                if len(text.encode("utf-8")) > MAX_LOG_BYTES:
                    return _fail("GUI_NATIVE_LOG_EXCEEDS_64_MIB")
                if re.search(r"Program\s+END|Sending job|Starting execution|^\s*(?:ct|nl|post):",text,re.M):
                    candidates.append({"text":text,"method":method,"class":control.ClassName,
                                       "type":control.ControlTypeName,"rect":list(uia.control_rect(control))})
        except Exception:
            continue
    if len(candidates) != 1:
        return _fail("ACTIVE_NATIVE_LOG_NOT_UNIQUE",candidates=len(candidates))
    return {"ok":True,"hwnd":main_hwnd,"pid":pid,"scope":"active_simulation_page",**candidates[0]}


def _gui_log_worker(main_hwnd, pid):
    print(json.dumps(_read_active_log(main_hwnd,pid),ensure_ascii=True))


def _probe_active_log(main_hwnd, pid, out, timeout=3):
    """Bounded read-only sidecar; reuse the new Bridge's process containment."""
    directory = Path(out)
    directory.mkdir(parents=True, exist_ok=True)
    call_id = uuid.uuid4().hex
    output_path, error_path = directory / f'{call_id}.json', directory / f'{call_id}.stderr.log'
    command = [sys.executable, '-I', '-m', 'af_r13_bridge.operations.simulation',
               '--read-log', str(main_hwnd), str(pid)]
    evidence = Evidence(directory)
    job, process, outcome = None, None, None
    try:
        job = WindowsWorkerJob(evidence, call_id)
        with output_path.open('xb') as stdout, error_path.open('xb') as stderr:
            process = subprocess.Popen(command, cwd=directory, env=sanitized_environment(),
                                       stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       creationflags=WindowsWorkerJob.creation_flags)
            job.assign_and_resume(process)
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                outcome = _fail('ACTIVE_LOG_READ_TIMED_OUT', timeout_s=timeout)
            else:
                if code != 0:
                    outcome = _fail('ACTIVE_LOG_READER_FAILED', exit_code=code)
        job.terminate_and_confirm()
        process.wait(timeout=5)
        if outcome is not None:
            return outcome
        parsed = json.loads(output_path.read_text(encoding='utf-8'))
        if parsed.get('ok') and (parsed.get('hwnd') != main_hwnd or parsed.get('pid') != pid
                                 or parsed.get('scope') != 'active_simulation_page'):
            return _fail('ACTIVE_LOG_READER_IDENTITY_MISMATCH')
        return parsed
    except (OSError, ValueError) as error:
        return _fail('ACTIVE_LOG_READER_FAILED', detail=repr(error))
    finally:
        if job is not None:
            job.terminate_and_confirm()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def _design_tabs(session):
    tabs = []
    for control, _ in uia.walk(session.main_window):
        if control.ClassName == 'AutoForm::DesignBarItem':
            tabs.append(list(control.GetRuntimeId()))
    return sorted(tabs)


def _save_identity(session, target, before_tabs, dialog_result):
    before_rid = list(getattr(session, 'design_runtime_id', []) or [])
    after_tabs = _design_tabs(session)
    title = session.title()
    path_readback = dialog_result.get('path_readback')
    title_matches = title.startswith(f'AutoForm Forming R13 - {target.name} ')
    identity_ok = bool(before_rid and before_rid in before_tabs and after_tabs == before_tabs)
    ok = bool(identity_ok and title_matches and dialog_result.get('ok')
              and path_readback == str(target))
    return {'ok': False, 'identity_verified': ok, 'path': str(target), 'path_readback': path_readback,
            'before_rid': before_rid, 'after_rid': before_rid if identity_ok else None,
            'before_tabs': before_tabs, 'after_tabs': after_tabs, 'title': title,
            'sha256': None, 'file_size': None, 'file_bytes_verified': False,
            'scope': 'GUI Start file-dialog path + same live tab + title; file bytes pending; dirty remains unknown'}


def _stable_file_bytes(target, *, timeout, interval=.25):
    """After job completion, require two matching readable, whole-file samples.

    A file can expose positive size while its writer denies reads. Only transient
    access/missing-file failures are retried; locks are never bypassed. Metadata
    must be unchanged across each hash read and between the two samples.
    """
    started = time.monotonic()
    deadline = started + max(0, timeout)
    previous = None
    samples = []
    retries = []
    while time.monotonic() < deadline:
        try:
            before = target.stat()
            if not target.is_file() or before.st_size <= 0:
                previous = None
                retries.append({'error': 'FILE_MISSING_OR_EMPTY'})
            else:
                digest = hash_file(target)
                after = target.stat()
                def signature(value):
                    return (value.st_size, value.st_mtime_ns, value.st_ino, value.st_dev)
                if signature(before) != signature(after):
                    previous = None
                    retries.append({'error': 'FILE_CHANGED_DURING_HASH'})
                else:
                    current = {'sha256': digest, 'size': after.st_size, 'mtime_ns': after.st_mtime_ns,
                               'file_id': after.st_ino, 'device': after.st_dev}
                    samples.append(current)
                    if previous == current:
                        return {'ok': True, 'sha256': digest, 'file_size': after.st_size,
                                'samples': samples[-2:], 'transient_observations': retries,
                                'elapsed_s': round(time.monotonic() - started, 3), 'timed_out': False}
                    previous = current
        except (PermissionError, FileNotFoundError) as error:
            previous = None
            retries.append({'error': type(error).__name__, 'errno': error.errno,
                            'winerror': getattr(error, 'winerror', None), 'message': str(error)})
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval, remaining))
    return {'ok': False, 'error': 'FINAL_FILE_BYTES_NOT_STABLE_OR_READABLE', 'sha256': None,
            'file_size': None, 'samples': samples[-2:], 'transient_observations': retries,
            'elapsed_s': round(time.monotonic() - started, 3), 'timed_out': True}


def _save_transaction(session, target, before_tabs, dialog_result, *, completion_verified=False, timeout=20):
    proof = _save_identity(session, target, before_tabs, dialog_result)
    if not proof['identity_verified']:
        proof['error'] = 'SAVED_DOCUMENT_IDENTITY_NOT_CONFIRMED'
        return proof
    if not completion_verified:
        proof['verification_pending'] = 'current-job completion and readable final bytes'
        return proof
    readable = _stable_file_bytes(target, timeout=timeout)
    # Reading may take time. Re-observe the same owned tab after the final hash.
    identity = _save_identity(session, target, before_tabs, dialog_result)
    proof.update(identity, file_verification=readable)
    if not readable['ok'] or not identity['identity_verified']:
        proof['error'] = readable.get('error') or 'SAVED_DOCUMENT_IDENTITY_CHANGED'
        return proof
    proof.update(ok=True, sha256=readable['sha256'], file_size=readable['file_size'],
                 file_bytes_verified=True, completion_verified=True,
                 scope='GUI Start path + same owned tab + completed current job + stable final file bytes; dirty remains unknown')
    return proof


def bind_native_log(text, job, target_path, before_hash=None):
    """Isolate this job's native text before applying the strict runner parser."""
    normalized = _normalize(text)
    if hashlib.sha256(text.encode("utf-8")).hexdigest() == before_hash:
        return _fail("GUI_NATIVE_LOG_UNCHANGED")
    lines = normalized.splitlines(keepends=True)
    job_lines = [i for i,line in enumerate(lines) if re.search(r"\b"+re.escape(job["name"])+r"\b",line)]
    path_lines = [i for i,line in enumerate(lines) if _path_key(target_path) in _path_key(line)]
    mode = None
    start = None
    if job_lines:
        start,mode = job_lines[0],"current_job_name"
    elif path_lines:
        start,mode = path_lines[0],"current_full_path"
    else:
        # A random per-run basename makes the native Postfile header unambiguous.
        # Common reused names (e.g. project.afd) are deliberately insufficient.
        stem = Path(target_path).stem
        unique = bool(re.search(r"(?i)(?:^|[-_])(?:[a-f0-9]{8,32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$",stem))
        post = re.compile(r'^\s*post:\s*Postfile\s+"'+re.escape(Path(target_path).name)+r'"\s+opened\.\s*$',re.I)
        matches = [i for i,line in enumerate(lines) if post.match(line)]
        if unique and matches:
            start,mode = matches[0],"unique_basename_after_current_job_start"
    if start is None:
        return _fail("NATIVE_LOG_CURRENT_JOB_BINDING_MISSING")
    current = "".join(lines[start:])
    from ..solver import solver_completion_evidence
    completion = solver_completion_evidence(current)
    return {"ok":True,"text":current,"binding_mode":mode,"start_line":start+1,
            "sha256":hashlib.sha256(current.encode("utf-8")).hexdigest(),"completion":completion}


def _save_dialogs(pid):
    return [window for window in win32.enum_top_windows(pid)
            if window.visible and window.enabled and window.class_name == "#32770"
            and window.title in {"另存为","Save As"}]


def start_recorded_simulation(session, target_path, out, timeout=300):
    """140→142→144→new Save As→new job→strict native completion.

    ``out`` may exist, but this function refuses its own previous evidence.
    A failed/timeout return never cancels or kills the user's AutoForm job.
    """
    target = Path(target_path).expanduser().resolve()
    output = Path(out).expanduser().resolve()
    if not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or timeout <= 0:
        return _fail("INVALID_SIMULATION_TIMEOUT")
    if target.suffix.casefold() != ".afd":
        return _fail("SIMULATION_TARGET_MUST_BE_AFD",path=str(target))
    if target.exists():
        return _fail("SIMULATION_TARGET_ALREADY_EXISTS",path=str(target))
    if getattr(session,"project",None) is not None:
        return _fail("SIMULATION_REQUIRES_UNSAVED_NEW_DESIGN",project=str(session.project))
    output.mkdir(parents=True,exist_ok=True)
    artifacts = ("simulation.json","gui-current.delta.log","native-solver.log","diagnostic-logs")
    if any((output/name).exists() for name in artifacts):
        return _fail("SIMULATION_EVIDENCE_ALREADY_EXISTS",out=str(output))
    state_path = output / "simulation.json"
    state_path.open("x",encoding="utf-8").close()
    started = time.monotonic()
    state = {"ok":False,"path":str(target),"out":str(output),"started":False,"saved":False,
             "job_bound":False,"completed":False,"evidence_events":[140,142,144],
             "gui_job_completion_verified":False,"native_solver_completion_verified":False}
    def finish(error=None, **extra):
        state.update(extra,elapsed_s=round(time.monotonic()-started,3))
        if error:
            state.update(ok=False,error=error)
        state_path.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding="utf-8")
        return state
    try:
        before_tabs = _design_tabs(session)
        design_rid = list(getattr(session, 'design_runtime_id', []) or [])
        if not design_rid or design_rid not in before_tabs:
            return finish('SIMULATION_OWNED_TAB_NOT_CONFIRMED')
        state['before_tabs'] = before_tabs
        log_root = _log_root()
        gui_path = log_root / ("log_AFFormingUI_%d.txt" % session.pid)
        baseline = _snapshot(gui_path)
        old_logs = {path.name for path in log_root.iterdir() if path.is_file()}
        state.update(gui_log_path=str(gui_path),gui_log_baseline=baseline,log_root=str(log_root))
        opened = pages.open_page(session,"simulation")
        state["stage"] = opened
        if not opened.get("ok"):
            return finish("SIMULATION_STAGE_NOT_REACHED")
        tab = _open_tab(session,"开始")
        state["start_tab"] = tab
        if not tab.get("ok"):
            return finish("SIMULATION_START_TAB_NOT_REACHED")
        status = _one(session.main_window,cls="AutoForm::StatusBarDialog")
        button = _one(status,name="开始",cls="AFButton",kind="ButtonControl")
        if button is None or not button.IsEnabled:
            return finish("SIMULATION_STATUS_START_NOT_UNIQUE_OR_ENABLED")
        main_hwnd = int(session.main_window.NativeWindowHandle)
        before_native = _probe_active_log(main_hwnd,session.pid,output / "log-probes")
        before_hash = hashlib.sha256(before_native["text"].encode("utf-8")).hexdigest() if before_native.get("ok") else None
        state["native_log_before"] = {k:v for k,v in before_native.items() if k != "text"}
        # Last check immediately before the action that can submit/save.
        if target.exists():
            return finish("SIMULATION_TARGET_ALREADY_EXISTS")
        action = common.click(session,button)
        state["start_input"] = action
        if not action.get("ok"):
            return finish("SIMULATION_START_INPUT_FAILED")
        state["started"] = True
        found = common.wait_for(lambda: (lambda items: items[0] if len(items)==1 else None)(_save_dialogs(session.pid)),
                                timeout=min(15,timeout))
        if found is None:
            return finish("SIMULATION_START_SAVE_DIALOG_NOT_UNIQUE")
        # Do not navigate or traverse the main UIA tree while Save As is modal.
        if (found.pid != session.pid or int(win32.user32.GetAncestor(found.hwnd,3) or 0) != main_hwnd
                or target.exists()):
            return finish("SIMULATION_SAVE_DIALOG_OR_TARGET_CHANGED")
        target.parent.mkdir(parents=True,exist_ok=True)
        state["save_dialog"] = dialogs.fill_save_dialog(found.hwnd,target)
        if not state['save_dialog'].get('ok'):
            return finish('SIMULATION_SAVE_DIALOG_PATH_NOT_CONFIRMED')
        def saved():
            conflicts = [w for w in win32.enum_top_windows(session.pid)
                         if w.class_name == "#32770" and ("确认另存为" in w.title or "Confirm Save As" in w.title)]
            if conflicts:
                return {"ok":True,"conflict":True}
            exists = target.exists() and target.stat().st_size > 0
            return {"ok":exists and not _save_dialogs(session.pid),"conflict":False,
                    "size":target.stat().st_size if exists else None}
        saved_state = common.wait_for(saved,timeout=min(30,timeout))
        state["save_file"] = saved_state
        if not saved_state or saved_state.get("conflict"):
            return finish("SIMULATION_NEW_SAVE_NOT_CONFIRMED")
        # A positive-size file only proves creation: R13 can still hold it with
        # incompatible sharing while writing simulation input/results.
        state['file_created'] = True
        session.project = target
        state['save_transaction'] = _save_transaction(session, target, before_tabs, state['save_dialog'])
        if not state['save_transaction']['identity_verified']:
            return finish('SIMULATION_SAVED_TAB_IDENTITY_NOT_CONFIRMED')
        (output/"diagnostic-logs").mkdir()
        last = {}
        job = None
        log_tab_opened = False
        deadline = started+timeout
        while time.monotonic() < deadline:
            delta = _gui_delta(gui_path,baseline)
            if not delta.get("ok"):
                return finish(delta["error"])
            (output/"gui-current.delta.log").write_bytes(delta["raw"])
            binding = current_job_evidence(delta["text"],target)
            state["job_binding"] = binding
            if binding.get("ok"):
                if job is not None and job != binding["job"]:
                    return finish("CURRENT_SIMULATION_JOB_CHANGED")
                job = binding["job"]
                state.update(job_bound=True,job=job)
                if not log_tab_opened:
                    state["log_tab"] = _open_tab(session,"日志")
                    log_tab_opened = True
                statuses = []
                log_meta = []
                for path in sorted(log_root.glob("log_%s_AFForming*.txt" % job["name"])):
                    if path.name in old_logs:
                        return finish("CURRENT_JOB_LOG_EXISTED_BEFORE_START",stale_log=str(path))
                    raw = _read_raw(path)
                    (output/"diagnostic-logs"/path.name).write_bytes(raw)
                    evidence = job_status_evidence(_decode(raw),job,target)
                    statuses.append(evidence)
                    log_meta.append({"path":str(path),"copy":str(output/"diagnostic-logs"/path.name),
                                     "sha256":hashlib.sha256(raw).hexdigest(),"size":len(raw)})
                state["job_logs"] = log_meta
                state["job_statuses"] = statuses
                if any(value.get("terminal_failure") for value in statuses):
                    return finish("CURRENT_GUI_JOB_FAILED_OR_CANCELLED")
                completed = any(value.get("completed") for value in statuses)
                state["gui_job_completion_verified"] = completed
                read = _probe_active_log(main_hwnd,session.pid,output / "log-probes",timeout=min(2,max(.1,deadline-time.monotonic())))
                last = {k:v for k,v in read.items() if k != "text"}
                if read.get("ok"):
                    native = bind_native_log(read["text"],job,target,before_hash)
                    last["binding"] = {k:v for k,v in native.items() if k != "text"}
                    if native.get("ok"):
                        (output/"native-solver.log").write_text(native["text"],encoding="utf-8")
                        state["native_log"] = {k:v for k,v in native.items() if k != "text"}
                        completion = native["completion"]
                        if completion.get("fatal_errors") or any(code != 0 for code in completion["program_end_codes"]):
                            return finish("CURRENT_NATIVE_SOLVER_FAILED",native_log_read=last)
                        if completed and completion.get("ok"):
                            codes = completion["program_end_codes"]
                            # GUI computation may update the file; bind the final bytes.
                            state['save_transaction'] = _save_transaction(session, target, before_tabs, state['save_dialog'],
                                completion_verified=True, timeout=max(0, min(20, deadline-time.monotonic())))
                            if not state['save_transaction']['ok']:
                                return finish('SIMULATION_FINAL_SAVE_NOT_CONFIRMED')
                            return finish(ok=True,error=None,saved=True,completed=True,native_solver_completion_verified=True,
                                          springback=True,program_end_codes=codes,program_end={"code":0},
                                          solver_completion_evidence=completion,native_log_read=last)
            time.sleep(min(.5,max(0,deadline-time.monotonic())))
        return finish("SIMULATION_COMPLETION_TIMEOUT",timed_out=True,native_log_read=last,
                      missing=[name for name,value in (("current_job",state["job_bound"]),
                               ("job_completed",state["gui_job_completion_verified"]),
                               ("ordered_native_springback_and_end",state["native_solver_completion_verified"])) if not value])
    except Exception as exc:
        return finish("SIMULATION_ACTION_EXCEPTION",detail=repr(exc))


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--read-log':
        _gui_log_worker(int(sys.argv[2]), int(sys.argv[3]))
    else:
        raise SystemExit('This private module only accepts --read-log HWND PID.')
