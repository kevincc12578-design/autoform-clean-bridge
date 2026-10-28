"""Discover or explicitly launch R13 without opening historical designs.

Only a newly launched session may apply its already selected license, and only
when the caller explicitly enables that option. No license selection is changed
and no application is terminated here.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

from . import native
from ..evidence import Evidence
from ..isolation import sanitized_environment
from ..models import BridgeError
from ..supervisor import Supervisor, WorkerError, WriterLease

_LICENSE_CLASS = 'AutoForm::LicensingManagerView'
_LICENSE_BUTTON = 'LicensingManagerView.m_mainFrame.buttonFrame.m_checkoutPB'
_WELCOME_CLASS = 'AutoForm::AFWelcomeDialog'
_WELCOME_BUTTON = 'main_window.AFWelcomeDialogClass.m_mainFrame.m_bottomFrame.m_closePB'


def _r13_processes():
    return [process for process in native.discover() if process['r13']]


def _identify(processes, expected=None):
    if len(processes) > 1:
        raise BridgeError('needs_user', 'Multiple R13 processes; choose a session before operating',
                          processes=processes)
    if not processes:
        return None
    process = processes[0]
    if expected is not None and (process['pid'], process['create_time']) != expected:
        raise BridgeError('needs_user', 'R13 process identity changed while connecting',
                          expected=list(expected), observed=process)
    return process


def _dump(supervisor, process, window):
    if window.get('pid') != process['pid'] or native.window_pid(window['hwnd']) != process['pid']:
        raise BridgeError('needs_user', 'Window ownership changed while connecting', window=window)
    try:
        response = supervisor.run_worker({'action': 'dump', 'hwnd': window['hwnd'],
                                          'pid': process['pid'], 'depth': 12}, timeout=12)
    except WorkerError as exc:
        raise BridgeError('needs_user', 'Cannot safely inspect the R13 startup window',
                          window=window, worker_error=exc.code, evidence=exc.evidence) from exc
    if not response.get('ok'):
        raise BridgeError('needs_user', 'Cannot read the R13 startup window',
                          window=window, response=response)
    return response


def _click_observed_button(supervisor, process, window, root_class, button_id, evidence):
    """Re-observe under the writer lock before the single allowed input."""
    expected = (process['pid'], process['create_time'])
    with WriterLease(process['pid']):
        current = _identify(_r13_processes(), expected)
        if current is None:
            raise BridgeError('needs_user', 'R13 exited before the startup action')
        windows = [item for item in current['windows'] if item['hwnd'] == window['hwnd']]
        if len(windows) != 1:
            raise BridgeError('needs_user', 'Startup window disappeared before input', window=window)
        observed = _dump(supervisor, current, windows[0])
        controls = observed.get('controls', [])
        if not controls or controls[0].get('class') != root_class:
            raise BridgeError('needs_user', 'Startup window type changed before input', window=window)
        buttons = [control for control in controls if control.get('aid') == button_id
                   and control.get('kind') == 'ButtonControl']
        if len(buttons) != 1 or not buttons[0].get('enabled') or buttons[0].get('offscreen'):
            raise BridgeError('needs_user', 'Startup button is not uniquely enabled and visible',
                              window=window, button_id=button_id, buttons=buttons)
        response = supervisor.run_worker({'action': 'click', 'hwnd': window['hwnd'],
                                          'pid': process['pid'], 'rid': buttons[0]['rid'],
                                          'depth': 12}, timeout=12)
        if not response.get('ok') or not response.get('input_sent'):
            raise BridgeError('needs_user', 'Startup action outcome is unknown', response=response)
        evidence.append('startup_input_sent', pid=process['pid'], create_time=process['create_time'],
                        window=window, button_id=button_id,
                        verification_scope='input_only; waiting for dialog disappearance')


def connect(evidence_dir: Path, launch: bool = False, license_server: str | None = None,
            *, allow_selected_license: bool = False) -> dict:
    evidence_dir = Path(evidence_dir).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence = Evidence(evidence_dir)
    process = _identify(_r13_processes())
    launched = False
    application = None
    initial_windows = process['windows'] if process else []
    expected = (process['pid'], process['create_time']) if process else None
    if process is None:
        if not launch:
            raise BridgeError('needs_user', 'No R13 session. Pass explicit launch=True to start it.')
        if not native.EXE.is_file():
            raise BridgeError('installation_missing', 'R13 executable was not found', path=str(native.EXE))
        command = [str(native.EXE)]
        env = sanitized_environment()
        if license_server is not None:
            env['AF_LICENSE_SERVER'] = license_server
        evidence.append('application_launch_requested', command=command, cwd=str(native.EXE.parent),
                        license_server=env.get('AF_LICENSE_SERVER'),
                        allow_selected_license=allow_selected_license)
        with (evidence_dir / 'application-stdout.log').open('xb') as output:
            application = subprocess.Popen(command, cwd=native.EXE.parent, env=env,
                                           stdout=output, stderr=subprocess.STDOUT,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
        launched = True
        try:
            created_at = native.psutil.Process(application.pid).create_time()
        except native.psutil.Error as exc:
            raise BridgeError('needs_user', 'Cannot confirm the newly launched R13 process identity',
                              pid=application.pid, application_terminated=False) from exc
        expected = (application.pid, created_at)
        launch_record = {'command': command, 'pid': application.pid, 'create_time': created_at,
                         'license_server': env.get('AF_LICENSE_SERVER'),
                         'allow_selected_license': allow_selected_license,
                         'policy': 'Application remains open; worker cleanup never terminates R13'}
        evidence.write_json('launch', launch_record)
        with (evidence_dir / 'launch.json').open('x', encoding='utf-8') as stream:
            json.dump(launch_record, stream, ensure_ascii=False, indent=2)

    deadline = time.monotonic() + 45
    acted_windows = {}
    license_applied = False
    welcome_closed = False
    supervisor = Supervisor(evidence_dir / 'connect-probes')
    try:
        while time.monotonic() < deadline:
            if application is not None and application.poll() is not None:
                raise BridgeError('needs_user', 'Launched R13 exited before connecting',
                                  pid=application.pid, exit_code=application.returncode)
            process = _identify(_r13_processes(), expected)
            if process is None:
                if not launched:
                    raise BridgeError('needs_user', 'Attached R13 exited while connecting', expected=list(expected))
                time.sleep(.3)
                continue
            windows = process['windows']
            main = [window for window in windows
                    if window['title'].startswith('AutoForm Forming R13') and not window['owner']]
            if len(main) > 1:
                raise BridgeError('needs_user', 'Multiple R13 main windows', windows=windows)
            others = [window for window in windows if not main or window['hwnd'] != main[0]['hwnd']]
            if not others and len(main) == 1:
                if not main[0].get('enabled'):
                    raise BridgeError('needs_user', 'Main window is disabled with no readable modal', windows=windows)
                result = {'pid': process['pid'], 'create_time': process['create_time'],
                          'hwnd': main[0]['hwnd'], 'launched': launched, 'window': main[0],
                          'baseline_windows': windows, 'initial_windows': initial_windows,
                          'selected_license_applied': license_applied, 'welcome_closed': welcome_closed}
                evidence.write_json('session', result)
                with (evidence_dir / 'session.json').open('x', encoding='utf-8') as stream:
                    json.dump(result, stream, ensure_ascii=False, indent=2)
                return result
            if len(others) > 1:
                for window in others:
                    _dump(supervisor, process, window)
                raise BridgeError('needs_user', 'Multiple R13 startup windows need review', windows=windows)
            if others:
                window = others[0]
                observed = _dump(supervisor, process, window)
                controls = observed.get('controls', [])
                root_class = controls[0].get('class') if controls else None
                dialog_identity = (window['hwnd'], root_class, tuple(controls[0].get('rid', [])) if controls else ())
                if dialog_identity in acted_windows:
                    if time.monotonic() - acted_windows[dialog_identity] > 10:
                        raise BridgeError('needs_user', 'Startup dialog did not disappear after input', windows=windows)
                    time.sleep(.3)
                    continue
                if root_class == _LICENSE_CLASS:
                    if not (launched and allow_selected_license) or license_applied:
                        raise BridgeError('needs_user', 'License selection needs the user; no selection was changed',
                                          windows=windows, launched=launched,
                                          allow_selected_license=allow_selected_license)
                    _click_observed_button(supervisor, process, window, _LICENSE_CLASS, _LICENSE_BUTTON, evidence)
                    license_applied = True
                    evidence.append('selected_license_input_sent', selection_changed=False,
                                    pid=process['pid'], window=window)
                elif root_class == _WELCOME_CLASS and len(main) == 1:
                    _click_observed_button(supervisor, process, window, _WELCOME_CLASS, _WELCOME_BUTTON, evidence)
                    welcome_closed = True
                else:
                    raise BridgeError('needs_user', 'Unknown R13 startup window; left untouched',
                                      windows=windows, observed_root_class=root_class)
                acted_windows[dialog_identity] = time.monotonic()
            time.sleep(.3)
        raise BridgeError('needs_user', 'R13 did not expose an enabled unique main window within 45 seconds',
                          expected=list(expected), processes=_r13_processes())
    except BaseException as exc:
        evidence.append('connect_failed', error_type=type(exc).__name__, error=str(exc),
                        details=getattr(exc, 'details', {}), application_terminated=False)
        raise
    finally:
        supervisor.shutdown()
