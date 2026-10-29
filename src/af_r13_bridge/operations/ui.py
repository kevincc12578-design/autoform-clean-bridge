"""Small business UI boundary using the new Bridge's native window guards.

This is a disposable worker view, not a second session/ownership implementation.
Every request receives the exact process, window and owned tab from WindowsAdapter.
"""
from __future__ import annotations
import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import time
from types import SimpleNamespace

import uiautomation as auto
from PIL import ImageGrab
from ..adapters import native


def rect(control):
    r = control.BoundingRectangle
    return (r.left, r.top, r.right, r.bottom)


def client_bounds(hwnd):
    box, origin = w.RECT(), w.POINT()
    native.u.GetClientRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
    native.u.ClientToScreen.argtypes = [w.HWND, c.POINTER(w.POINT)]
    if not native.u.GetClientRect(hwnd, c.byref(box)) or not native.u.ClientToScreen(hwnd, c.byref(origin)):
        raise OSError('Cannot read current client bounds')
    return origin.x, origin.y, origin.x + box.right, origin.y + box.bottom


def _intersects(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


class WindowScopedControl:
    def __init__(self, raw, clip):
        self.raw, self.clip = raw, clip

    def __getattr__(self, name):
        return getattr(self.raw, name)

    @property
    def IsOffscreen(self):
        return self.raw.IsOffscreen or not _intersects(rect(self.raw), self.clip)

    def GetChildren(self):
        children = []
        for child in self.raw.GetChildren():
            box = rect(child)
            if box[2] > box[0] and box[3] > box[1] and not _intersects(box, self.clip):
                continue
            children.append(WindowScopedControl(child, self.clip))
        return children

    def GetParentControl(self):
        parent = self.raw.GetParentControl()
        return WindowScopedControl(parent, self.clip) if parent else None


def walk(root, depth=0, max_depth=40):
    yield root, depth
    if depth < max_depth:
        for child in root.GetChildren():
            yield from walk(child, depth + 1, max_depth)


def collect(root):
    rows = []
    for control, depth in walk(root):
        value = None
        try:
            pattern = control.GetValuePattern()
            value = pattern.Value if pattern else None
        except Exception:
            pass
        rows.append({'type': control.ControlTypeName, 'class_name': control.ClassName,
                     'name': control.Name, 'value': value, 'rect': list(rect(control)),
                     'enabled': bool(control.IsEnabled), 'offscreen': bool(control.IsOffscreen),
                     'automation_id': control.AutomationId, 'rid': list(control.GetRuntimeId()), 'depth': depth})
    return rows


def find_all(root, **criteria):
    aliases = {'class_name': 'ClassName', 'name': 'Name', 'kind': 'ControlTypeName', 'aid': 'AutomationId'}
    return [item for item, _ in walk(root) if not item.IsOffscreen
            and all(getattr(item, aliases.get(key, key)) == value for key, value in criteria.items())]


def find_one(root, **criteria):
    found = find_all(root, **criteria)
    return found[0] if len(found) == 1 else None


def wait_for(predicate, timeout=20, interval=.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value.get('ok') if isinstance(value, dict) and 'ok' in value else bool(value):
            return value
        time.sleep(interval)
    return None


class WindowInfo(SimpleNamespace):
    def as_dict(self):
        return vars(self).copy()


def windows(pid=None, visible_only=True):
    return [WindowInfo(**item, visible=True) for item in native.windows(pid)]


def window_info(hwnd):
    found = [item for item in windows() if item.hwnd == hwnd]
    if len(found) != 1:
        raise RuntimeError('Window disappeared')
    return found[0]


def geometry(hwnd):
    item = window_info(hwnd)
    box = client_bounds(hwnd)
    native.u.GetDpiForWindow.argtypes = [w.HWND]
    native.u.IsZoomed.argtypes = [w.HWND]
    return {'rect': item.rect, 'client_size': [box[2]-box[0], box[3]-box[1]],
            'dpi': int(native.u.GetDpiForWindow(hwnd)), 'maximized': bool(native.u.IsZoomed(hwnd)),
            'minimized': bool(native.u.IsIconic(hwnd))}


def wait_stable_geometry(hwnd, timeout=3):
    previous, repeats = None, 0
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        current = geometry(hwnd)
        repeats = repeats+1 if current == previous else 0
        if repeats >= 2 and not current['minimized']:
            return current
        previous = current
        time.sleep(.1)
    return None


def _prepare(session, focus=True):
    if focus and not session.focus():
        return {'ok': False, 'error': 'WINDOW_FOCUS_FAILED'}
    actual = wait_stable_geometry(session.hwnd)
    if actual != session.window_profile:
        return {'ok': False, 'error': 'WINDOW_PROFILE_CHANGED', 'actual': actual, 'expected': session.window_profile}
    return {'ok': True, 'hwnd': session.hwnd, 'geometry': actual}


def _check_point(session, point, root_hwnd):
    x, y = map(int, point)
    fg = native.u.GetForegroundWindow()
    hit = native.u.WindowFromPoint(w.POINT(x, y))
    root = int(native.u.GetAncestor(hit, 2) or hit or 0)
    ok = native.window_pid(fg) == session.pid and root == int(root_hwnd) and native.window_pid(root) == session.pid
    return {'ok': ok, 'error': None if ok else 'POINT_WINDOW_MISMATCH', 'point': [x,y], 'hit_root': root, 'expected_root': int(root_hwnd)}


def click_screen(x, y):
    native.click_point(x,y,wait=.08)


def click(session, control, *, root_hwnd=None, focus=True, relative=None):
    prepared = _prepare(session, focus=focus)
    if not prepared['ok']:
        return prepared
    if not control.IsEnabled or control.IsOffscreen:
        return {'ok': False, 'error': 'CONTROL_NOT_ACTIONABLE'}
    box = rect(control)
    rx, ry = relative or (.5, .5)
    if not (0 <= rx < 1 and 0 <= ry < 1) or box[2] <= box[0] or box[3] <= box[1]:
        return {'ok': False, 'error': 'INVALID_CONTROL_RECT'}
    point = (int(box[0]+(box[2]-box[0])*rx), int(box[1]+(box[3]-box[1])*ry))
    checked = _check_point(session, point, root_hwnd or session.hwnd)
    if not checked['ok']:
        return checked
    click_screen(*point)
    return {'ok': True, 'input_sent': True, 'postcondition_verified': False, 'rect': list(box), **checked}


def double_click(session, control, *, relative=None):
    result = click(session, control, relative=relative)
    if not result['ok']:
        return result
    point = result['point']
    again = _check_point(session, point, session.hwnd)
    if not again['ok']:
        return again
    click_screen(*point)
    return {**result, 'method': 'guarded_double_click'}


def guarded_drag(session, start, end, duration=1):
    prepared = _prepare(session)
    if not prepared['ok']:
        return prepared
    for point in (start, end):
        checked = _check_point(session, point, session.hwnd)
        if not checked['ok']:
            return checked
    native.u.SetCursorPos(int(start[0]), int(start[1]))
    native.u.mouse_event(2, 0, 0, 0, None)
    try:
        count = max(12, int(duration*40))
        for i in range(1, count+1):
            if native.window_pid(native.u.GetForegroundWindow()) != session.pid:
                return {'ok': False, 'error': 'FOREGROUND_CHANGED_DURING_DRAG'}
            native.u.SetCursorPos(round(start[0]+(end[0]-start[0])*i/count), round(start[1]+(end[1]-start[1])*i/count))
            time.sleep(duration/count)
        return {'ok': True, 'input_sent': True, 'start': list(start), 'end': list(end)}
    finally:
        native.u.mouse_event(4, 0, 0, 0, None)


control_from_handle = auto.ControlFromHandle


def wait_file_dialog(pid, timeout=15):
    def locate():
        found = [item for item in windows(pid) if item.class_name == '#32770']
        if len(found) > 1:
            raise RuntimeError('Ambiguous file dialogs')
        return found[0] if found else None
    return wait_for(locate, timeout)


def _fill_dialog(hwnd, path, save):
    path = Path(path).resolve()
    if not save and not path.is_file():
        raise FileNotFoundError(path)
    info = window_info(hwnd)
    if info.class_name != '#32770':
        raise RuntimeError('Expected native file dialog')
    native.foreground(hwnd, info.pid)
    items = native.dialog_children(hwnd)
    edits = [item for item in items if item['class']=='Edit' and item['aid'] in ('1001','1148')]
    buttons = [item for item in items if item['class']=='Button' and item['aid']=='1']
    if len(edits)!=1 or len(buttons)!=1:
        raise RuntimeError('File dialog fields not unique')
    edit = control_from_handle(edits[0]['hwnd'])
    x,y = native.guard_point(hwnd, info.pid, rect(edit))
    auto.Click(x,y,waitTime=.1)
    edit.SetFocus()
    auto.SendKeys('{Ctrl}a',waitTime=.05)
    for char in str(path):
        if not edit.HasKeyboardFocus:
            raise RuntimeError('File path input focus changed')
        auto.SendUnicodeChar(char)
    if not wait_for(lambda: edit.GetValuePattern().Value == str(path),3,.05):
        raise RuntimeError('File dialog path readback mismatch')
    # The preview/currentChanged handler runs after the edit's value updates.
    # Observe an unchanged path over the historical verified settling interval.
    stable_until=time.monotonic()+.6
    while time.monotonic()<stable_until:
        if edit.GetValuePattern().Value!=str(path):
            raise RuntimeError('File path changed during preview settling')
        time.sleep(.1)
    button = control_from_handle(buttons[0]['hwnd'])
    if not button.IsEnabled:
        raise RuntimeError('File dialog submit disabled')
    x,y = native.guard_point(hwnd, info.pid, rect(button))
    native.click_point(x,y)
    if not wait_for(lambda: not any(item.hwnd==hwnd for item in windows(info.pid)),10,.1):
        raise RuntimeError('File dialog did not close after submit')
    return {'ok': True, 'path': str(path), 'path_readback': str(path), 'dialog': info.as_dict(),
            'edit_hwnd': edits[0]['hwnd'], 'open_button_hwnd': buttons[0]['hwnd']}


def fill_file_dialog(hwnd, path):
    return _fill_dialog(hwnd, path, False)


def fill_save_dialog(hwnd, path):
    return _fill_dialog(hwnd, path, True)


PAGES = {
    'part': ('AutoForm::PartSetupPageView',None),
    'part.import': ('AutoForm::PartImportPageView','导入'),
    'part.tip': ('AutoForm::TipPageBaseView','冲压方向'),
    'part.material': ('AutoForm::MaterialPageView','材料'),
    'part.formcheck': ('AutoForm::FeasibilityContainerView','成型检查'),
    'plan': ('AutoForm::ProcessLayoutStageView',None),
    'die_face': ('AutoForm::GeometryPageView',None),
    'blank': ('AutoForm::BlankPageView',None),
    'process': ('AutoForm::ProcessPageView',None),
    'simulation': ('AutoForm::SimulationPageView',None),
}


def open_page(session, key, timeout=20):
    if key not in PAGES:
        return {'ok': False, 'error': 'UNKNOWN_PAGE', 'page': key}
    stage = key.split('.')[0]
    stage_class = PAGES[stage][0]
    if find_one(session.main_window,class_name=stage_class) is None:
        bar = find_one(session.main_window,class_name='AFTaskBar')
        if bar is None:
            return {'ok': False, 'error': 'TASKBAR_NOT_UNIQUE'}
        points = json.loads((Path(__file__).parent/'calibration'/'stages.json').read_text(encoding='utf-8'))
        result = click(session,bar,relative=points[stage]['relative'])
        if not result['ok']:
            return result
        if not wait_for(lambda: find_one(session.main_window,class_name=stage_class),timeout):
            return {'ok': False, 'error': 'STAGE_NOT_CONFIRMED', 'page':key}
    presenter, activity = PAGES[key]
    if activity:
        button = find_one(session.main_window, class_name='AFFixedFontSizeToolButton',name=activity)
        if button is None:
            return {'ok': False,'error':'ACTIVITY_NOT_UNIQUE','activity':activity}
        result = click(session,button)
        if not result['ok']:
            return result
    found = wait_for(lambda: find_one(session.main_window,class_name=presenter),timeout)
    return {'ok': found is not None, 'page':key, 'error':None if found is not None else 'PRESENTER_NOT_CONFIRMED'}


class BusinessSession:
    def __init__(self, request):
        self.pid, self.hwnd = request['pid'], request['hwnd']
        self.directory = Path(request['directory'])
        self.project = Path(request['project']) if request.get('project') else None
        self.document_rid = request['document_rid']
        self.design_runtime_id = self.document_rid
        self.main_window = WindowScopedControl(control_from_handle(self.hwnd),client_bounds(self.hwnd))
        self.window_profile = wait_stable_geometry(self.hwnd)
        if self.window_profile is None:
            raise RuntimeError('Window geometry is not stable')
        self.verify_document()

    def verify_document(self):
        if native.window_pid(self.hwnd) != self.pid:
            raise RuntimeError('Business window process changed')
        tabs = [item for item,_ in walk(self.main_window) if item.ClassName=='AutoForm::DesignBarItem'
                and list(item.GetRuntimeId())==self.document_rid]
        if len(tabs)!=1:
            raise RuntimeError('Owned business document identity missing')
        buttons = [item for item in tabs[0].GetChildren() if item.ClassName=='AFDesignBarToolButton']
        if len(buttons)!=1:
            raise RuntimeError('Owned business document button ambiguous')
        from ..adapters.windows import active_title_matches
        if not active_title_matches(self.title(),buttons[0].Name):
            raise RuntimeError('Active document no longer matches owned tab')
        return True

    def focus(self):
        native.foreground(self.hwnd,self.pid)
        return True

    def title(self):
        return control_from_handle(self.hwnd).Name

    def screenshot(self,path):
        path=Path(path)
        path.parent.mkdir(parents=True,exist_ok=True)
        ImageGrab.grab(bbox=tuple(window_info(self.hwnd).rect)).save(path)
        return path

    def dump(self,path):
        rows=collect(self.main_window)
        Path(path).write_text(json.dumps({'pid':self.pid,'title':self.title(),'nodes':rows},ensure_ascii=False,indent=2),encoding='utf-8')
        return rows


uia = SimpleNamespace(walk=walk,control_rect=rect)
common = SimpleNamespace(click=click,double_click=double_click,guarded_drag=guarded_drag,
                         wait_for=wait_for,_prepare=_prepare,_check_point=_check_point)
win32 = SimpleNamespace(enum_top_windows=windows,window_info=window_info,wait_stable_geometry=wait_stable_geometry,
                        window_geometry=geometry,click_screen=click_screen,user32=native.u,
                        foreground_pid=lambda:native.window_pid(native.u.GetForegroundWindow()))
dialogs = SimpleNamespace(wait_file_dialog=wait_file_dialog,fill_file_dialog=fill_file_dialog,fill_save_dialog=fill_save_dialog)
pages = SimpleNamespace(open_page=open_page)
