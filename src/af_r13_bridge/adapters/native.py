"""Small, independently implemented Win32 boundary. No UIA calls here."""
from __future__ import annotations
import ctypes as c
from ctypes import wintypes as w
from pathlib import Path
import time
import psutil
import json
import sys
from contextlib import contextmanager
from .. import window_policy as policy

EXE = Path('D:/Program Files/AutoForm/AFplus/R13F/bin/AFFormingUI.exe')
u = c.WinDLL('user32', use_last_error=True)
u.GetForegroundWindow.restype = w.HWND
u.GetAncestor.argtypes = [w.HWND, w.UINT]
u.GetAncestor.restype = w.HWND
u.WindowFromPoint.argtypes = [w.POINT]
u.WindowFromPoint.restype = w.HWND
u.GetWindow.argtypes = [w.HWND, w.UINT]
u.GetWindow.restype = w.HWND
u.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
u.GetWindowRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
u.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
u.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
u.IsWindowVisible.argtypes = [w.HWND]
u.IsWindowEnabled.argtypes = [w.HWND]
u.SetForegroundWindow.argtypes = [w.HWND]
u.ShowWindow.argtypes = [w.HWND, c.c_int]
u.IsIconic.argtypes = [w.HWND]
u.GetDlgCtrlID.argtypes = [w.HWND]
try:
    u.SetProcessDPIAware()
except OSError:
    pass

def window_pid(hwnd):
    pid = w.DWORD()
    u.GetWindowThreadProcessId(hwnd, c.byref(pid))
    return pid.value

def windows(pid=None):
    found = []
    callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    @callback_type
    def callback(hwnd, _):
        if u.IsWindowVisible(hwnd) and (pid is None or window_pid(hwnd) == pid):
            title, cls, rect = c.create_unicode_buffer(2048), c.create_unicode_buffer(256), w.RECT()
            u.GetWindowTextW(hwnd, title, 2048)
            u.GetClassNameW(hwnd, cls, 256)
            u.GetWindowRect(hwnd, c.byref(rect))
            found.append(dict(hwnd=int(hwnd), pid=window_pid(hwnd), title=title.value,
                              class_name=cls.value, enabled=bool(u.IsWindowEnabled(hwnd)),
                              owner=int(u.GetWindow(hwnd, 4) or 0),
                              rect=[rect.left, rect.top, rect.right, rect.bottom]))
        return True
    u.EnumWindows(callback, 0)
    return found

def discover():
    result=[]
    for p in psutil.process_iter(['pid','name','exe','create_time']):
        if p.info['name'] and p.info['name'].lower() == 'afformingui.exe':
            result.append({**p.info, 'r13': bool(p.info['exe'] and Path(p.info['exe']).resolve()==EXE.resolve()),
                           'windows': windows(p.pid)})
    return result

def outside_desktop(rect):
    x,y,width,height=[u.GetSystemMetrics(index) for index in (76,77,78,79)]
    l,t,r,b=rect
    return r<=x or b<=y or l>=x+width or t>=y+height

def dialog_children(hwnd):
    """Native HWND/control IDs avoid traversing volatile Explorer file items."""
    result=[]
    callback_type=c.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
    @callback_type
    def callback(child,_):
        class_name=c.create_unicode_buffer(256)
        u.GetClassNameW(child,class_name,256)
        result.append({'hwnd':int(child),'class':class_name.value,
                       'aid':str(u.GetDlgCtrlID(child)),'pid':window_pid(child)})
        return True
    u.EnumChildWindows(hwnd,callback,0)
    return result

def foreground(hwnd, pid):
    if window_pid(hwnd) != pid:
        raise RuntimeError('Window PID changed')
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, 9)
        # DWM can report the final rectangle before restore animation ends.
        # Recheck foreground/hit guards only after that visual transition.
        time.sleep(.4)
    if u.GetForegroundWindow()!=hwnd:
        u.SetForegroundWindow(hwnd)
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        if u.GetForegroundWindow()==hwnd:
            return
        time.sleep(.05)
    raise RuntimeError('Foreground guard failed')

def guard_point(hwnd, pid, rect, *, popup_proof=None):
    guard_input(hwnd,pid,activate=True,popup_proof=popup_proof)
    outer=w.RECT()
    u.GetWindowRect(hwnd,c.byref(outer))
    l,t,r,b=rect
    if not (r>l and b>t and l>=outer.left and t>=outer.top and r<=outer.right and b<=outer.bottom):
        raise RuntimeError('Target rectangle outside current window')
    x,y=(l+r)//2,(t+b)//2
    hit=u.WindowFromPoint(w.POINT(x,y))
    if window_pid(hit)!=pid or u.GetAncestor(hit,2)!=hwnd:
        raise RuntimeError('Hit window guard failed')
    return x,y


class MONITORINFO(c.Structure):
    _fields_ = [('cbSize',w.DWORD),('rcMonitor',w.RECT),('rcWork',w.RECT),('dwFlags',w.DWORD)]


class GUITHREADINFO(c.Structure):
    _fields_ = [('cbSize',w.DWORD),('flags',w.DWORD),('hwndActive',w.HWND),
                ('hwndFocus',w.HWND),('hwndCapture',w.HWND),('hwndMenuOwner',w.HWND),
                ('hwndMoveSize',w.HWND),('hwndCaret',w.HWND),('rcCaret',w.RECT)]


u.GetClientRect.argtypes = [w.HWND,c.POINTER(w.RECT)]
u.ClientToScreen.argtypes = [w.HWND,c.POINTER(w.POINT)]
u.GetDpiForWindow.argtypes = [w.HWND]
u.IsZoomed.argtypes = [w.HWND]
u.IsWindow.argtypes = [w.HWND]
u.MonitorFromWindow.argtypes = [w.HWND,w.DWORD]
u.MonitorFromWindow.restype = w.HANDLE
u.GetMonitorInfoW.argtypes = [w.HANDLE,c.POINTER(MONITORINFO)]
u.SetWindowPos.argtypes = [w.HWND,w.HWND,c.c_int,c.c_int,c.c_int,c.c_int,w.UINT]
u.GetGUIThreadInfo.argtypes = [w.DWORD,c.POINTER(GUITHREADINFO)]
u.SetThreadDpiAwarenessContext.argtypes = [w.HANDLE]
u.SetThreadDpiAwarenessContext.restype = w.HANDLE
_input_profile = None
_material_preview = None


@contextmanager
def physical_pixels():
    previous = u.SetThreadDpiAwarenessContext(w.HANDLE(-4))
    if not previous:
        policy.reject('DPI_CONTEXT_FAILED', winerror=c.get_last_error())
    try:
        yield
    finally:
        u.SetThreadDpiAwarenessContext(previous)


def geometry(hwnd):
    with physical_pixels():
        outer, client, origin = w.RECT(), w.RECT(), w.POINT()
        if not (u.IsWindow(hwnd) and u.GetWindowRect(hwnd,c.byref(outer))
                and u.GetClientRect(hwnd,c.byref(client)) and u.ClientToScreen(hwnd,c.byref(origin))):
            policy.reject('WINDOW_GEOMETRY_UNAVAILABLE', hwnd=hwnd)
        dpi = int(u.GetDpiForWindow(hwnd))
        if not dpi:
            policy.reject('WINDOW_DPI_UNKNOWN', hwnd=hwnd)
        return dict(hwnd=int(hwnd),pid=window_pid(hwnd),rect=[outer.left,outer.top,outer.right,outer.bottom],
                    client_rect=[origin.x,origin.y,origin.x+client.right,origin.y+client.bottom],
                    dpi=dpi,minimized=bool(u.IsIconic(hwnd)),maximized=bool(u.IsZoomed(hwnd)),
                    enabled=bool(u.IsWindowEnabled(hwnd)))


def normalize_window(hwnd, pid, configuration=None):
    """Explicit session preparation; input guards never silently resize windows."""
    expected = policy.expected_profile(hwnd,pid,configuration)
    before = geometry(hwnd)
    if before['pid'] != pid or before['dpi'] != expected['dpi'] or not before['enabled']:
        policy.reject('WINDOW_PREPARATION_REFUSED', expected=expected, observed=before)
    requested = expected['client_rect']
    with physical_pixels():
        info = MONITORINFO(cbSize=c.sizeof(MONITORINFO))
        if not u.GetMonitorInfoW(u.MonitorFromWindow(hwnd,2),c.byref(info)):
            policy.reject('MONITOR_INFO_FAILED')
        work = info.rcWork
        if not (work.left <= requested[0] < requested[2] <= work.right
                and work.top <= requested[1] < requested[3] <= work.bottom):
            policy.reject('PROFILE_OUTSIDE_WORK_AREA', requested=requested)
        if before['minimized'] or before['maximized']:
            u.ShowWindow(hwnd,9)
            time.sleep(.4)
        for attempt in range(4):
            current = geometry(hwnd)
            if current['pid'] != pid or current['dpi'] != expected['dpi']:
                policy.reject('WINDOW_IDENTITY_CHANGED', observed=current)
            outer, client = current['rect'], current['client_rect']
            x, y = outer[0]+requested[0]-client[0], outer[1]+requested[1]-client[1]
            width = outer[2]-outer[0]+requested[2]-requested[0]-client[2]+client[0]
            height = outer[3]-outer[1]+requested[3]-requested[1]-client[3]+client[1]
            if not (work.left <= x and work.top <= y and x+width <= work.right and y+height <= work.bottom):
                policy.reject('FRAME_OUTSIDE_WORK_AREA', rect=[x,y,x+width,y+height])
            if not u.SetWindowPos(hwnd,None,x,y,width,height,0x4000|0x0010|0x0004):
                policy.reject('SET_WINDOW_POS_FAILED')
            stable, last, end = 0, None, time.monotonic()+1.5
            while time.monotonic() < end:
                current = geometry(hwnd)
                if (current['pid']==pid and current['dpi']==expected['dpi'] and current['client_rect']==requested
                        and not current['minimized'] and not current['maximized']):
                    stable = stable+1 if current==last else 1
                    if stable>=3:
                        return {**current,'work_rect':[work.left,work.top,work.right,work.bottom]}
                else:
                    stable=0
                last=current
                time.sleep(.06)
    policy.reject('WINDOW_NORMALIZE_FAILED', expected=expected, observed=current)


def configure_input(profile):
    global _input_profile, _material_preview
    _input_profile = dict(profile) if profile else None
    _material_preview = None


@contextmanager
def material_preview_context(dialog_hwnd, proof):
    global _material_preview
    if _material_preview is not None:
        policy.reject('MATERIAL_PREVIEW_CONTEXT_ALREADY_REGISTERED')
    _material_preview=(dialog_hwnd,dict(proof)) if proof else None
    try:
        yield
    finally:
        _material_preview=None


def gui_state(hwnd):
    pid = w.DWORD()
    thread = u.GetWindowThreadProcessId(hwnd,c.byref(pid))
    info = GUITHREADINFO(cbSize=c.sizeof(GUITHREADINFO))
    if not thread or not u.GetGUIThreadInfo(thread,c.byref(info)):
        policy.reject('GUI_STATE_UNAVAILABLE', hwnd=hwnd)
    return dict(thread_id=int(thread),pid=int(pid.value),focus=int(info.hwndFocus or 0),
                capture=int(info.hwndCapture or 0),active=int(info.hwndActive or 0))


def root_window(hwnd):
    return int(u.GetAncestor(hwnd,2) or hwnd or 0)


def work_area(hwnd):
    with physical_pixels():
        info=MONITORINFO(cbSize=c.sizeof(MONITORINFO))
        if not u.GetMonitorInfoW(u.MonitorFromWindow(hwnd,2),c.byref(info)):
            policy.reject('MONITOR_INFO_FAILED')
        return [info.rcWork.left,info.rcWork.top,info.rcWork.right,info.rcWork.bottom]


def _check_input_context(hwnd, pid, popup_proof=None):
    if window_pid(hwnd)!=pid or not u.IsWindowEnabled(hwnd) or u.IsIconic(hwnd):
        policy.reject('TARGET_NOT_INTERACTIVE', hwnd=hwnd)
    if _input_profile:
        main = _input_profile['hwnd']
        policy.assert_profile(geometry(main),_input_profile)
    else:
        # Startup license/welcome probes precede fixed-window preparation.
        main = int(u.GetAncestor(hwnd,3) or hwnd)
    visible = windows(pid)
    gui = gui_state(hwnd)
    if popup_proof:
        if gui['thread_id']!=gui_state(main)['thread_id'] or gui['active'] not in (main,hwnd):
            policy.reject('POPUP_THREAD_OR_ACTIVE_MISMATCH')
    suggestions = [item for item in visible if item['class_name']=='Auto-Suggest Dropdown']
    allowed = set()
    if _material_preview is not None and _material_preview[0]==hwnd:
        proof=_material_preview[1]
        if gui_state(proof['hwnd'])['thread_id']!=gui['thread_id']:
            policy.reject('MATERIAL_PREVIEW_THREAD_MISMATCH')
        allowed.update(policy.material_preview_allowance(hwnd,main,visible,proof))
    dialog = next((item for item in visible if item['hwnd']==hwnd),None)
    if suggestions and dialog and dialog['class_name']=='#32770':
        edits = [item for item in dialog_children(hwnd) if item['class']=='Edit' and item['aid'] in ('1001','1148')]
        if len(edits)!=1:
            policy.reject('FILENAME_CONTEXT_INVALID', edits=edits)
        edit = geometry(edits[0]['hwnd'])
        allowed.update(policy.filename_suggestions(dialog,edit,suggestions,gui,gui_state(edit['hwnd']),
                                               gui_state(suggestions[0]['hwnd']),root_window(edit['hwnd']),work_area(hwnd)))
    chain = policy.input_context(hwnd,main,visible,gui,root_window(gui['capture']),allowed,popup_proof)
    return dict(main=main,target=hwnd,owner_chain=chain,gui=gui)


def guard_input(hwnd, pid, *, activate=False, popup_proof=None):
    try:
        result = _check_input_context(hwnd,pid,popup_proof)
        if activate:
            foreground(hwnd,pid)
            result = _check_input_context(hwnd,pid,popup_proof)
        fg = u.GetForegroundWindow()
        if window_pid(fg)!=pid:
            policy.reject('FOREGROUND_MISMATCH', foreground=int(fg or 0))
        return result
    except policy.BridgeError as error:
        # Existing business helpers may retain only str(error). The supervisor
        # already captures stderr; keep the actual rejected windows there.
        print(json.dumps({'event':'input_guard_rejected','target':hwnd,'error':error.to_dict()},
                         ensure_ascii=True),file=sys.stderr,flush=True)
        raise


def click_point(x,y,wait=.15):
    """Let Qt process pointer movement before the down/up gesture."""
    u.SetCursorPos.argtypes=[c.c_int,c.c_int]
    u.SetCursorPos.restype=w.BOOL
    u.mouse_event.argtypes=[w.DWORD,w.DWORD,w.DWORD,w.DWORD,c.c_void_p]
    if not u.SetCursorPos(int(x),int(y)):
        raise OSError('SetCursorPos failed')
    time.sleep(.15)
    u.mouse_event(2,0,0,0,None)
    try:
        time.sleep(.05)
    finally:
        u.mouse_event(4,0,0,0,None)
    time.sleep(wait)
