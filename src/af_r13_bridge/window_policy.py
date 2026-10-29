"""Fixed-window and input-context rules, independent of Windows and UIA.

Selected rules adapted from the isolated click experiment; the existing Bridge
continues to own sessions, transport, workers and document lifecycle.
"""
from .models import BridgeError

DEFAULT_WINDOW = dict(client_width=1800, client_height=960, client_x=30, client_y=50, dpi=96)
# Qt registers separate tooltip classes for shadow/no-shadow flags. Both were
# observed in R13; do not broaden this to all Qt popup/tool window classes.
DECORATION_CLASSES = frozenset({'SysShadow','Qt5QWindowToolTipDropShadowSaveBits',
                               'Qt5QWindowToolTipSaveBits','tooltips_class32'})


def is_decoration(window):
    return window.get('class_name') in DECORATION_CLASSES


def reject(code, **details):
    raise BridgeError(code, code, **details)


def rectangle(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(isinstance(v, bool) or not isinstance(v, int) for v in value)):
        reject('INVALID_RECT', rect=value)
    if value[2] <= value[0] or value[3] <= value[1]:
        reject('EMPTY_RECT', rect=value)
    return tuple(value)


def assert_profile(actual, expected):
    differences = {}
    for key in ('hwnd', 'pid', 'client_rect', 'dpi', 'maximized'):
        a, b = actual.get(key), expected.get(key)
        if key == 'client_rect':
            a, b = rectangle(a), rectangle(b)
        if a != b:
            differences[key] = {'expected': b, 'observed': a}
    if actual.get('minimized'):
        differences['minimized'] = {'expected': False, 'observed': True}
    if differences:
        reject('WINDOW_PROFILE_CHANGED', differences=differences)
    return actual


def expected_profile(hwnd, pid, configuration=None):
    cfg = dict(DEFAULT_WINDOW if configuration is None else configuration)
    if set(cfg) != set(DEFAULT_WINDOW) or any(isinstance(v, bool) or not isinstance(v, int) for v in cfg.values()):
        reject('INVALID_WINDOW_PROFILE')
    x, y, width, height = (cfg[k] for k in ('client_x', 'client_y', 'client_width', 'client_height'))
    if min(width, height, cfg['dpi']) <= 0:
        reject('INVALID_WINDOW_PROFILE')
    return dict(hwnd=int(hwnd), pid=int(pid), client_rect=[x,y,x+width,y+height], dpi=cfg['dpi'], maximized=False)


def owner_chain(target, main, windows):
    index = {item['hwnd']: item for item in windows}
    if main not in index:
        reject('MAIN_WINDOW_MISSING', hwnd=main)
    chain, current = [], target
    while current:
        item = index.get(current)
        if current in chain or item is None or item['pid'] != index[main]['pid']:
            reject('WINDOW_NOT_OWNED', target=target, chain=chain)
        chain.append(current)
        if current == main:
            return chain
        current = item.get('owner', 0)
    reject('WINDOW_NOT_OWNED', target=target, chain=chain)


def filename_suggestions(dialog, edit, popups, gui, edit_gui, popup_gui, edit_root, work_rect=None):
    """Allow only one currently focused, geometrically attached filename list."""
    if not popups:
        return set()
    if len(popups) != 1:
        reject('FILENAME_SUGGESTIONS_AMBIGUOUS')
    popup = popups[0]
    if (dialog['class_name'] != '#32770' or not dialog['enabled']
            or gui['focus'] != edit['hwnd'] or edit_root != dialog['hwnd']
            or len({dialog['pid'], edit['pid'], popup['pid']}) != 1
            or len({gui['thread_id'], edit_gui['thread_id'], popup_gui['thread_id']}) != 1):
        reject('FILENAME_SUGGESTION_CONTEXT_INVALID')
    dl, dt, dr, db = rectangle(dialog['rect'])
    left, top, right, bottom = rectangle(popup['rect'])
    el, et, er, eb = rectangle(edit['rect'])
    wl, wt, wr, wb = rectangle(work_rect if work_rect is not None else dialog['rect'])
    if not (dl <= el < er <= dr and dt <= et < eb <= db
            and wl <= left < right <= wr and wt <= top < bottom <= wb
            and left == el and right == er and top == eb):
        reject('FILENAME_SUGGESTION_CONTEXT_INVALID', reason='dropdown_not_attached_to_filename_edit',
               dialog_rect=dialog['rect'],edit_rect=edit['rect'],popup_rect=popup['rect'],work_rect=work_rect)
    return {popup['hwnd']}


def input_context(target, main, windows, gui, capture_root, allowed_suggestions=(), popup_proof=None):
    if popup_proof:
        item=next((w for w in windows if w['hwnd']==target),None)
        principal=next((w for w in windows if w['hwnd']==main),None)
        native_classes={'AFMenu':'Qt5QWindowPopupDropShadowSaveBits',
                        'AutoForm::MaterialDisclaimerView':'Qt5QWindow'}
        if (not item or not principal or target==main or item.get('owner')!=0
                or item['pid']!=principal['pid']
                or item['class_name']!=native_classes.get(popup_proof.get('root_class'))
                or any(popup_proof.get(k)!=item.get(k) for k in ('hwnd','pid','class_name','rect'))):
            reject('POPUP_PROOF_INVALID')
        # Qt AFMenu may have no native owner. Keep that fact; this explicit
        # target is allowed by a fresh bounded UIA observation, not a fake owner.
        chain=[target,main]
    else:
        chain = owner_chain(target, main, windows)
    expected = set(chain) | set(allowed_suggestions)
    main_record = next(item for item in windows if item['hwnd'] == main)
    blockers = [item for item in windows if item['pid'] == main_record['pid']
                and item.get('visible', True) and item['hwnd'] not in expected
                and not is_decoration(item)]
    if blockers:
        reject('UNEXPECTED_OWNED_WINDOW', windows=blockers)
    if gui['capture'] and capture_root != target:
        reject('MOUSE_CAPTURE_REDIRECTED', target=target, gui=gui, capture_root=capture_root)
    return chain


def material_preview_allowance(target, main, windows, proof):
    """One observed material preview is a sibling of its native file picker."""
    index={item['hwnd']:item for item in windows}
    dialog,preview,principal=index.get(target),index.get(proof.get('hwnd')),index.get(main)
    if (not dialog or not preview or not principal or target==main
            or dialog['class_name']!='#32770' or dialog.get('owner')!=main
            or preview['class_name']!='Qt5QWindow' or preview.get('owner')!=main
            or proof.get('root_class')!='AutoForm::MaterialPreviewerView'
            or len({dialog['pid'],preview['pid'],principal['pid']})!=1
            or any(proof.get(k)!=preview.get(k) for k in ('hwnd','pid','owner','class_name','rect','title'))):
        reject('MATERIAL_PREVIEW_CONTEXT_INVALID')
    return {preview['hwnd']}
