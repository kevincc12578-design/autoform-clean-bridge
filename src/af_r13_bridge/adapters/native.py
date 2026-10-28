"""Small, independently implemented Win32 boundary. No UIA calls here."""
from __future__ import annotations
import ctypes as c
from ctypes import wintypes as w
from pathlib import Path
import time
import psutil

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
    if u.GetForegroundWindow()!=hwnd:
        u.SetForegroundWindow(hwnd)
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        if u.GetForegroundWindow()==hwnd:
            return
        time.sleep(.05)
    raise RuntimeError('Foreground guard failed')

def guard_point(hwnd, pid, rect):
    foreground(hwnd,pid)
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
