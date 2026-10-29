"""Selected verified part operations, integrated with the new Bridge UI boundary.

Source and selection hashes are recorded in docs/provenance-part.json. No legacy
runtime imports, session creation, project saving or closing are performed here.
"""
from __future__ import annotations

import math
from pathlib import Path
import re
import time

from . import ui

TIP_PAGE = "AutoForm::TipPageBaseView"
FORMCHECK_BRANCH_PAGE = "AutoForm::FeasibilityPageView"


def _by_id(root, automation_id, *, suffix=False):
    matches = []
    for control, _ in ui.walk(root):
        try:
            actual = str(control.AutomationId or "")
            if _visible(control) and (actual.endswith(automation_id) if suffix else actual == automation_id):
                matches.append(control)
        except Exception:
            continue
    return matches[0] if len(matches) == 1 else None


def _click(session, control, **kwargs):
    return ui.click(session, control, **kwargs)


def _wait(predicate, timeout: float, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def _visible(control) -> bool:
    try:
        rect = ui.rect(control)
        return bool(not control.IsOffscreen and rect and rect[2] > rect[0] and rect[3] > rect[1])
    except Exception:
        return False


def _unique(root, *, name: str | None = None, class_name: str | None = None,
            types: set[str] | None = None):
    candidates = []
    if root is not None:
        for control, _ in ui.walk(root):
            try:
                if not _visible(control):
                    continue
                if name is not None and (control.Name or "").strip() != name:
                    continue
                if class_name is not None and control.ClassName != class_name:
                    continue
                if types is not None and control.ControlTypeName not in types:
                    continue
                candidates.append(control)
            except Exception:
                continue
    return candidates[0] if len(candidates) == 1 else None


def _page(session, class_name: str):
    return _unique(session.main_window, class_name=class_name)


def _rows(root) -> list[dict]:
    if root is None:
        return []
    # ui.collect intentionally reads values for engineering verification;
    # this is distinct from the privacy-limited recording observer.
    visible_rects = {tuple(ui.rect(control)) for control, _ in ui.walk(root) if _visible(control)}
    return [row for row in ui.collect(root) if tuple(row.get("rect") or ()) in visible_rects]


DEGREE_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(?:°|deg)\s*$", re.IGNORECASE)


def _degree(value) -> float | None:
    if not isinstance(value, str):
        return None
    match = DEGREE_RE.match(value.replace("−", "-"))
    return float(match.group(1)) if match else None


def _axis(name: str) -> str | None:
    name = str(name or "").strip().rstrip(":：").replace("′", "'").replace("″", "''").replace("’", "'")
    return {"X": "X", "Y": "Y'", "Y'": "Y'", "Z": "Z''", "Z''": "Z''"}.get(name)


def angles_from_rows(rows: list[dict]) -> dict:
    """Read degree-valued X/Y′/Z″ only; mm rotation points cannot qualify.

    UIA sometimes names the spinner itself and sometimes exposes only its label.
    The fallback pairs an axis label with the nearest degree field on that row.
    Conflicting duplicates are rejected instead of silently overwriting an axis.
    """
    values = {"X": [], "Y'": [], "Z''": []}
    sources = {key: [] for key in values}
    fields = [row for row in rows if row.get("type") in {"SpinnerControl", "EditControl", "TextControl"}
              and _degree(row.get("value") if row.get("value") is not None else row.get("name")) is not None]
    for row in fields:
        axis = _axis(row.get("name"))
        if axis:
            degree = _degree(row.get("value"))
            if degree is not None:
                values[axis].append(degree)
                sources[axis].append(row)
    for label in rows:
        axis = _axis(label.get("name"))
        bounds = label.get("rect")
        if not axis or not bounds or label.get("type") != "TextControl":
            continue
        ly = (bounds[1]+bounds[3])/2
        candidates = [row for row in fields if row.get("rect") and row["rect"][0] >= bounds[2]-3
                      and abs((row["rect"][1]+row["rect"][3])/2-ly) <= max(8, (bounds[3]-bounds[1])/2)]
        if candidates:
            field = min(candidates, key=lambda row: row["rect"][0])
            degree = _degree(field.get("value") if field.get("value") is not None else field.get("name"))
            values[axis].append(degree)
            sources[axis].append(field)
    angles = {}
    ambiguous = []
    for axis, candidates in values.items():
        if candidates and max(candidates)-min(candidates) <= 0.01:
            angles[axis] = candidates[0]
        elif candidates:
            ambiguous.append(axis)
    return {"ok": len(angles) == 3 and not ambiguous, "angles_deg": angles,
            "ambiguous": ambiguous, "sources": sources}


def read_direction_angles(session) -> dict:
    page = _page(session, TIP_PAGE)
    if page is None:
        return {"ok": False, "error": "TIP_PAGE_NOT_FOUND", "angles_deg": {}}
    result = angles_from_rows(_rows(page))
    selector = _direction_selector(page)
    result["direction_label"] = (selector.Name or "").strip() if selector is not None else None
    return result


def _direction_selector(page):
    label = _unique(page, name="方向", types={"TextControl"})
    if label is None:
        return None
    lr = ui.rect(label)
    candidates = []
    for control, _ in ui.walk(page):
        try:
            if control.ControlTypeName not in {"ButtonControl", "ComboBoxControl"} or not _visible(control):
                continue
            bounds = ui.rect(control)
            if bounds[0] >= lr[2]-3 and abs((bounds[1]+bounds[3]-lr[1]-lr[3])/2) <= max(8, (lr[3]-lr[1])/2):
                candidates.append((bounds[0], control))
        except Exception:
            continue
    return min(candidates, key=lambda pair: pair[0])[1] if candidates else None


def _find_reverse_menu(session):
    candidates = []
    for window in ui.windows(session.pid or 0):
        if not window.visible or window.pid != session.pid:
            continue
        root = ui.control_from_handle(window.hwnd)
        try:
            if root.ClassName != "AFMenu":
                continue
        except Exception:
            continue
        item = _unique(root, name="反向", class_name="QAction", types={"MenuItemControl"})
        if item is not None:
            candidates.append((window.hwnd, item))
    return candidates[0] if len(candidates) == 1 else None


def reversal_confirmed(before: dict, after: dict, *, tolerance: float = 0.1) -> bool:
    if not before.get("ok") or not after.get("ok") or after.get("direction_label") != "反向":
        return False
    a, b = before["angles_deg"], after["angles_deg"]
    delta = lambda x, y: (y-x+180) % 360-180
    return (abs(abs(delta(a["X"], b["X"]))-180) <= tolerance
            and abs(delta(a["Y'"], b["Y'"])) <= tolerance
            and abs(delta(a["Z''"], b["Z''"])) <= tolerance)


def apply_auto_tip_and_reverse(session, *, timeout: float = 30.0) -> dict:
    opened = ui.open_page(session, "part.tip", timeout=timeout)
    if not opened.get("ok"):
        return {"ok": False, "error": "TIP_PAGE_NOT_REACHED", "opened": opened}
    page = _page(session, TIP_PAGE)
    automatic = _unique(page, name="自动设置冲压方向", types={"ButtonControl"})
    if automatic is None:
        return {"ok": False, "error": "AUTO_TIP_ENTRY_NOT_FOUND"}
    auto_click = _click(session, automatic)
    if not auto_click.get("ok"):
        return {"ok": False, "error": "AUTO_TIP_CLICK_FAILED", "click": auto_click}
    angle_tab = _wait(lambda: _unique(_page(session, TIP_PAGE), name="冲压方向角度",
                                    types={"CheckBoxControl", "TabItemControl"}), timeout)
    if angle_tab is None:
        return {"ok": False, "error": "AUTO_TIP_EDITOR_NOT_CONFIRMED"}
    tab_click = _click(session, angle_tab)
    if not tab_click.get("ok"):
        return {"ok": False, "error": "TIP_ANGLE_TAB_CLICK_FAILED", "click": tab_click}
    before = _wait(lambda: (value if (value := read_direction_angles(session)).get("ok") else None), timeout)
    if before is None:
        return {"ok": False, "error": "TIP_ANGLES_UNREADABLE"}
    selector = _direction_selector(_page(session, TIP_PAGE))
    if selector is None:
        return {"ok": False, "error": "DIRECTION_SELECTOR_NOT_FOUND", "before": before}
    # The recording clicked the right arrow of the split QToolButton, not its
    # centre/default action. common.click rereads the rect after focusing.
    menu_click = _click(session, selector, relative=(0.9, 0.5))
    if not menu_click.get("ok"):
        return {"ok": False, "error": "DIRECTION_MENU_CLICK_FAILED", "click": menu_click}
    menu = _wait(lambda: _find_reverse_menu(session), min(timeout, 10))
    if menu is None:
        return {"ok": False, "error": "AFMENU_REVERSE_NOT_FOUND", "before": before}
    hwnd, item = menu
    chosen = _click(session, item, root_hwnd=hwnd, focus=False)
    if not chosen.get("ok"):
        return {"ok": False, "error": "REVERSE_MENU_CLICK_FAILED", "click": chosen, "before": before}
    last = {}
    def confirmed():
        nonlocal last
        last = read_direction_angles(session)
        return last if reversal_confirmed(before, last) and _find_reverse_menu(session) is None else None
    after = _wait(confirmed, timeout)
    return {"ok": after is not None, "error": None if after else "REVERSE_NOT_CONFIRMED",
            "before": before, "after": after or last,
            "auto_click": auto_click, "angle_tab_click": tab_click,
            "direction_menu_click": menu_click, "reverse_click": chosen,
            "evidence_events": [25, 27, 29, 31], "persisted": False}


def _status_apply(session):
    status = _unique(session.main_window, class_name="AutoForm::StatusBarDialog")
    return _unique(status, name="应用", types={"ButtonControl"})


def bending_result_evidence(rows: list[dict]) -> dict:
    """Read completed-result fields, not product quality, from one branch page.

    Exact units and unique AFLabels matter: a toolbar caption, unrelated value,
    zero, nonfinite value, missing unit or ambiguous duplicate is not a result.
    The result/plot controls also exist before calculation, so their presence
    corroborates the branch only and never proves that a new Apply finished.
    """
    fields = {}
    numeric = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
    for key, label, unit in (("weight_kg", "零件重量", "kg"), ("force_kn", "冲力", "kN")):
        candidates = [row for row in rows if row.get("type") == "TextControl"
                      and row.get("class_name") == "AFLabel"
                      and re.match(r"^\s*"+re.escape(label)+r"\s*[:：]", str(row.get("name") or ""))]
        entry = {"status": "missing" if not candidates else "invalid", "value": None,
                 "unit": unit, "candidates": len(candidates), "texts": [row["name"] for row in candidates]}
        if len(candidates) == 1:
            text = str(candidates[0]["name"]).replace("−", "-")
            prefix = r"\s*"+re.escape(label)+r"\s*[:：]\s*"
            if re.fullmatch(prefix+r"[-–—]\s*"+unit+r"\s*", text):
                entry["status"] = "placeholder"
            else:
                match = re.fullmatch(prefix+r"("+numeric+r")\s*"+unit+r"\s*", text)
                if match:
                    value = float(match.group(1))
                    if math.isfinite(value) and value > 0:
                        entry.update(status="valid", value=value)
        elif len(candidates) > 1:
            entry["status"] = "ambiguous"
        fields[key] = entry
    expected_controls = (("评估结果", "AFProgressiveDisclosureBtn"),
                         ("评估力", "AFProgressiveDisclosureBtn"),
                         ("展开的料片", "QToolButton"))
    controls = {name: sum(row.get("type") == "CheckBoxControl"
                          and row.get("class_name") == class_name and row.get("name") == name for row in rows)
                for name, class_name in expected_controls}
    bending_views = [row for row in rows if row.get("class_name") == "AutoForm::FeasibilityBendingView"]
    valid_numbers = all(entry["status"] == "valid" for entry in fields.values())
    placeholders = all(entry["status"] == "placeholder" for entry in fields.values())
    context = all(count == 1 for count in controls.values()) and len(bending_views) == 1
    return {"ok": bool(valid_numbers and context), "fields": fields,
            "numeric_results": {key: entry["value"] for key, entry in fields.items()},
            "both_placeholders": placeholders, "result_controls": controls,
            "bending_view_candidates": len(bending_views),
            "bending_view_rect": bending_views[0].get("rect") if len(bending_views) == 1 else None,
            "branch_context_verified": context, "engineering_quality_accepted": False}


def _bending_state(session) -> dict:
    page = _page(session, FORMCHECK_BRANCH_PAGE)
    rows = _rows(page)
    texts = [str(row.get("name") or "") for row in rows]
    busy = any(row.get("type") == "ProgressBarControl" for row in rows) or any("请等待" in text for text in texts)
    return {"branch_visible": page is not None, "page_rect": ui.rect(page) if page is not None else None,
            "busy": busy, "texts": texts, "values": [row for row in rows if row.get("value") is not None],
            "result_evidence": bending_result_evidence(rows),
            "scope": "current_unique_visible_FeasibilityPageView"}


def apply_bending_check(session, *, timeout: float = 180.0) -> dict:
    opened = ui.open_page(session, "part.formcheck")
    if not opened.get("ok"):
        return {"ok": False, "error": "FORMCHECK_PAGE_NOT_REACHED", "opened": opened}
    selected = _select_bending_branch(session, timeout=timeout)
    if not selected.get("ok"):
        return {"ok": False, "error": "BENDING_BRANCH_NOT_REACHED", "selected": selected}
    apply = _status_apply(session)
    if apply is None:
        return {"ok": False, "error": "FORMCHECK_APPLY_NOT_FOUND", "selected": selected}
    initial = {}
    def ready_before_apply():
        nonlocal initial
        initial = _bending_state(session)
        return initial if (initial.get("branch_visible")
                           and initial.get("result_evidence", {}).get("branch_context_verified")) else None
    before = _wait(ready_before_apply, min(timeout, 10.0), interval=.15)
    if before is None:
        return {"ok": False, "error": "BENDING_CURRENT_BRANCH_NOT_CONFIRMED", "selected": selected, "before": initial}
    if before.get("busy"):
        return {"ok": False, "error": "BENDING_ALREADY_BUSY_BEFORE_APPLY", "selected": selected, "before": before}
    clicked = _click(session, apply)
    if not clicked.get("ok"):
        return {"ok": False, "error": "FORMCHECK_APPLY_CLICK_FAILED", "click": clicked}
    busy_seen, stable_idle, last = False, 0, {}
    stable_key, stable_since, last_stable_at = None, None, None
    completion_mode = None
    placeholder_before = bool(before.get("result_evidence", {}).get("both_placeholders"))
    minimum_stable_s = .15
    def completed():
        nonlocal busy_seen, stable_idle, last, stable_key, stable_since, last_stable_at, completion_mode
        last = _bending_state(session)
        current = last.get("result_evidence", {})
        # Only samples after this Apply count toward a busy cycle. A fast
        # calculation may finish before the first poll; '-' -> valid labels is
        # an independent observed transition, and avoids requiring a lost pulse.
        busy_seen = busy_seen or bool(last.get("branch_visible") and last.get("busy"))
        mode = ("observed_busy_to_idle_with_valid_results" if busy_seen else
                "placeholders_to_finite_positive_results" if placeholder_before else None)
        valid_idle = bool(last.get("branch_visible") and not last.get("busy") and current.get("ok") and mode)
        if not valid_idle:
            stable_idle, stable_key, stable_since, last_stable_at, completion_mode = 0, None, None, None, None
            return None
        numbers = current["numeric_results"]
        key = (tuple(last.get("page_rect") or ()), tuple(current.get("bending_view_rect") or ()),
               numbers["weight_kg"], numbers["force_kn"])
        now = time.monotonic()
        if stable_key != key:
            stable_key, stable_since, stable_idle = key, now, 1
        else:
            stable_idle += 1
        last_stable_at, completion_mode = now, mode
        elapsed = now-stable_since
        return last if stable_idle >= 2 and elapsed+1e-9 >= minimum_stable_s else None
    result = _wait(completed, timeout, interval=0.15)
    return {"ok": result is not None, "error": None if result else "BENDING_COMPLETION_NOT_CONFIRMED",
            "selected": selected, "click": clicked, "before": before,
            "after": result or last, "busy_seen": busy_seen, "stable_idle_samples": stable_idle,
            "completion_mode": completion_mode if result is not None else None,
            "result_stability": {"required_samples": 2, "required_interval_s": minimum_stable_s,
                                 "observed_samples": stable_idle,
                                 "observed_interval_s": (last_stable_at-stable_since)
                                 if last_stable_at is not None and stable_since is not None else 0.0},
            "placeholder_before_apply": placeholder_before,
            "evidence_events": [51, 53, 55], "engineering_quality_accepted": False}


def material_matches(rows: list[dict], stem: str, thickness_mm: float) -> dict:
    observed = []
    for row in rows:
        for value in (row.get("name"), row.get("value")):
            if not isinstance(value, str) or stem not in value:
                continue
            match = re.search(re.escape(stem) + r"/\s*(\d+(?:\.\d+)?)\s*mm", value, re.IGNORECASE)
            if match:
                observed.append({"text": value, "thickness_mm": float(match.group(1))})
    return {"ok": any(math.isclose(item["thickness_mm"], thickness_mm, abs_tol=0.001) for item in observed),
            "material_stem": stem, "target_thickness_mm": thickness_mm, "observed": observed}


def _disclaimer(session):
    for window in ui.windows(session.pid or 0):
        if window.visible and window.pid == session.pid:
            try:
                root = ui.control_from_handle(window.hwnd)
                if root.ClassName == "AutoForm::MaterialDisclaimerView":
                    button = _by_id(root, "MaterialDisclaimerView.m_mainFrame.m_buttonBoxFrame.buttonBox.OkButton")
                    return (window.hwnd, button) if button is not None else None
            except Exception:
                continue
    return None


def _material_window_status(session) -> dict:
    """Read modal state independently of material text becoming available.

    Qt can show a nonblocking/open() dialog after the inline editor has already
    changed. https://doc.qt.io/qt-6/qdialog.html
    """
    try:
        main_hwnd = int(session.main_window.NativeWindowHandle)
        main_enabled = bool(session.main_window.IsEnabled and next(w.enabled for w in ui.windows(session.pid) if w.hwnd == main_hwnd))
    except Exception as exc:
        return {"main_enabled": False, "modal_hwnds": [], "error": repr(exc)}
    modals, unclassified = [], []
    for window in ui.windows(session.pid or 0):
        if not window.visible or window.pid != session.pid or window.hwnd == main_hwnd:
            continue
        try:
            control = ui.control_from_handle(window.hwnd)
            if bool(control.GetWindowPattern().IsModal):
                modals.append(window.hwnd)
        except Exception:
            unclassified.append(window.hwnd)
    return {"main_enabled": main_enabled, "modal_hwnds": modals,
            "unclassified_secondary_hwnds": unclassified}


def _apply_material(session, material_file: str | Path, *, thickness_mm: float = 4.0,
                    timeout: float = 30.0, context: dict) -> dict:
    """Video material import with editor-scoped confirmation and target readback."""
    path = Path(material_file).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() != ".mtb":
        return {"ok": False, "error": "MATERIAL_INPUT_INVALID", "path": str(path)}
    opened = ui.open_page(session, "part.material")
    if not opened.get("ok"):
        return {"ok": False, "error": "MATERIAL_PAGE_NOT_REACHED", "opened": opened}
    if _page(session, "AutoForm::AddMaterialEditorView") is not None:
        return {"ok": False, "error": "MATERIAL_EDITOR_ALREADY_OPEN"}
    existing = ui.windows(session.pid)
    context["initial_windows"] = [window.hwnd for window in existing]
    if any(window.hwnd != session.hwnd for window in existing):
        return {"ok": False, "error": "MATERIAL_SECONDARY_WINDOW_ALREADY_OPEN"}
    page = _page(session, "AutoForm::MaterialPageView")
    button = _unique(page, name="添加材料", types={"ButtonControl"})
    if button is None:
        button = _unique(page, name="替换...", types={"ButtonControl"})
    if button is None:
        return {"ok": False, "error": "MATERIAL_ENTRY_NOT_FOUND"}
    clicked = _click(session, button)
    if not clicked.get("ok"):
        return {"ok": False, "error": "MATERIAL_ENTRY_CLICK_FAILED", "click": clicked}
    context["entry_clicked"] = True
    dialog = ui.wait_file_dialog(session.pid, timeout=timeout)
    editor = _page(session, "AutoForm::AddMaterialEditorView")
    if editor is not None:
        context["editor_rid"] = list(editor.GetRuntimeId())
    if dialog is None:
        return {"ok": False, "error": "MATERIAL_FILE_DIALOG_NOT_FOUND"}
    if dialog.pid != session.pid or dialog.owner != session.hwnd or dialog.hwnd in context["initial_windows"]:
        return {"ok": False, "error": "MATERIAL_FILE_DIALOG_OWNERSHIP_UNVERIFIED"}
    context["picker"] = {"hwnd": dialog.hwnd, "title": dialog.title,
                         "class_name": dialog.class_name, "pid": dialog.pid, "owner": dialog.owner}
    context["path"] = str(path)
    filled = ui.fill_file_dialog(dialog.hwnd, path)
    if not filled.get("ok"):
        return {"ok": False, "error": "MATERIAL_FILE_DIALOG_FAILED", "file_dialog": filled}
    confirmed_editor = accepted_disclaimer = False
    disclaimer_seen = False
    clicked_disclaimers = set()
    acceptances = []
    stable_since = None
    last_stable_at = None
    stable_samples = 0
    stability_started = time.monotonic()
    stable_required_s = 1.0
    resets = []
    environment = {}
    completed_at = None
    last = {}
    def reset(reason):
        nonlocal stable_since, stable_samples, last_stable_at
        if stable_since is not None:
            resets.append({"elapsed_s": round(time.monotonic()-stability_started, 4), "reason": reason})
        stable_since, stable_samples, last_stable_at = None, 0, None
    def finished():
        nonlocal confirmed_editor, accepted_disclaimer, last, disclaimer_seen
        nonlocal stable_since, stable_samples, environment, completed_at, last_stable_at
        # The inline editor can remain visible behind the modal disclaimer.
        # Handle the modal first; never refocus the main window through it.
        disclaimer = _disclaimer(session)
        if disclaimer is not None:
            disclaimer_seen = True
            reset("disclaimer_visible")
            hwnd, confirm = disclaimer
            if hwnd not in clicked_disclaimers:
                result = _click(session, confirm, root_hwnd=hwnd, focus=False)
                acceptances.append({"hwnd": hwnd, "elapsed_s": round(time.monotonic()-stability_started, 4),
                                    "click": result})
                if result.get("ok"):
                    accepted_disclaimer = True
                    clicked_disclaimers.add(hwnd)
            return None
        editor = _page(session, "AutoForm::AddMaterialEditorView")
        if editor is not None:
            reset("material_editor_visible")
            confirm = _unique(editor, name="确定", types={"ButtonControl"})
            if confirm is not None and not confirmed_editor:
                result = _click(session, confirm)
                if result.get("ok"):
                    confirmed_editor = True
            return None
        last = material_matches(_rows(_page(session, "AutoForm::MaterialPageView")), path.stem, thickness_mm)
        environment = _material_window_status(session)
        if not last.get("ok"):
            reset("material_readback_mismatch")
            return None
        if not environment.get("main_enabled") or environment.get("modal_hwnds") or environment.get("unclassified_secondary_hwnds"):
            reset("main_window_disabled_or_modal_visible")
            return None
        now = time.monotonic()
        if stable_since is None:
            stable_since = now
        last_stable_at = now
        stable_samples += 1
        if now-stable_since < stable_required_s:
            return None
        # Close the observation interval with another disclaimer check; textual
        # readback alone never grants completion while an async modal is present.
        if _disclaimer(session) is not None:
            reset("disclaimer_appeared_at_stability_boundary")
            return None
        environment = _material_window_status(session)
        if not environment.get("main_enabled") or environment.get("modal_hwnds") or environment.get("unclassified_secondary_hwnds"):
            reset("modal_appeared_at_stability_boundary")
            return None
        completed_at = now
        return last
    after = _wait(finished, timeout)
    return {"ok": after is not None, "error": None if after else "MATERIAL_NOT_CONFIRMED",
            "path": str(path), "file_dialog": filled, "after": after or last,
            "editor_confirmed": confirmed_editor, "disclaimer_accepted": accepted_disclaimer,
            "disclaimer_seen": disclaimer_seen, "disclaimer_acceptances": acceptances,
            "disclaimer_closed": bool(after is not None and disclaimer_seen),
            "stability": {"required_s": stable_required_s,
                          "observed_s": round(last_stable_at-stable_since, 4) if stable_since is not None and last_stable_at is not None else 0.0,
                          "started_monotonic_s": stable_since,
                          "last_verified_monotonic_s": last_stable_at,
                          "completed_monotonic_s": completed_at,
                          "samples": stable_samples, "resets": resets,
                          "window_state": environment},
            "evidence_events": [33, 35, 42, 47, 49]}


def _cleanup_material_failure(session, context, timeout=8):
    """Cancel only the picker/editor created by this material attempt.

    Native picker cancellation is path-bound; inline cancellation is RuntimeId-
    bound and invoked inside the already supervised business worker. Engineering
    documents are neither saved nor closed here.
    """
    evidence = {"attempted": bool(context.get("entry_clicked")), "ok": False, "steps": [],
                "document_saved": False, "document_closed": False}
    if not context.get("entry_clicked"):
        return {**evidence, "ok": True, "scope": "no material input sent"}
    try:
        session.verify_document()
        windows = ui.windows(session.pid)
        picker = context.get("picker")
        current_picker = next((window for window in windows if picker and window.hwnd == picker["hwnd"]), None)
        if current_picker is not None:
            expected = (picker["pid"], picker["owner"], picker["title"], "#32770")
            actual = (current_picker.pid, current_picker.owner, current_picker.title, current_picker.class_name)
            if actual != expected:
                raise RuntimeError("Material picker identity changed")
            root = ui.control_from_handle(current_picker.hwnd)
            edit = _by_id(root, "1148")
            if edit is None:
                raise RuntimeError("Material picker path field unavailable")
            pattern = edit.GetValuePattern()
            actual_path = pattern.Value if pattern is not None else None
            if actual_path != context.get("path"):
                raise RuntimeError("Material picker path is not this attempt's exact input")
            cancel = _by_id(root, "2")
            if cancel is None or cancel.ControlTypeName != "ButtonControl" or not cancel.IsEnabled:
                raise RuntimeError("Material picker cancel is not uniquely enabled")
            clicked = _click(session, cancel, root_hwnd=current_picker.hwnd, focus=False)
            evidence["steps"].append({"action": "cancel_material_picker", "path": actual_path,
                                      "hwnd": current_picker.hwnd, "input": clicked})
            if not clicked.get("ok"):
                raise RuntimeError("Material picker cancellation input failed")
            gone = _wait(lambda: not any(window.hwnd != session.hwnd for window in ui.windows(session.pid)), timeout, .15)
            if not gone:
                raise RuntimeError("Material picker/preview disappearance unconfirmed; no other window cancelled")
            evidence["steps"].append({"action": "picker_and_preview_closed", "ok": True})
        elif any(window.hwnd != session.hwnd for window in windows):
            raise RuntimeError("Unrecognized material secondary window remains; left untouched")
        session.verify_document()
        editor = _page(session, "AutoForm::AddMaterialEditorView")
        if editor is not None:
            if context.get("editor_rid") is None or list(editor.GetRuntimeId()) != context["editor_rid"]:
                raise RuntimeError("Material editor identity differs from this attempt")
            cancel = _unique(editor, name="取消", types={"ButtonControl"})
            if cancel is None or not cancel.IsEnabled or not _visible(cancel):
                raise RuntimeError("Material editor cancellation control unavailable")
            prepared = ui._prepare(session)
            if not prepared.get("ok"):
                raise RuntimeError("Material editor window focus/profile unverified")
            bounds = ui.rect(cancel)
            point = ((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2)
            checked = ui._check_point(session, point, session.hwnd)
            if not checked.get("ok"):
                raise RuntimeError("Material editor cancellation point mismatch")
            pattern = cancel.GetInvokePattern()
            if pattern is None:
                raise RuntimeError("Material editor Cancel has no InvokePattern")
            pattern.Invoke()
            evidence["steps"].append({"action": "cancel_material_editor", "method": "InvokePattern",
                                      "editor_rid": context["editor_rid"], "input_sent": True})
            if not _wait(lambda: _page(session, "AutoForm::AddMaterialEditorView") is None, timeout, .15):
                raise RuntimeError("Material editor cancellation not confirmed")
        final = ui.windows(session.pid)
        main = [window for window in final if window.hwnd == session.hwnd]
        if len(main) != 1 or not main[0].enabled or any(window.hwnd != session.hwnd for window in final):
            raise RuntimeError("Material cleanup did not restore the enabled main window")
        evidence.update(ok=True, scope="this attempt's material picker/preview and inline editor only")
    except Exception as error:
        evidence["error"] = {"type": type(error).__name__, "message": str(error)}
    return evidence


def apply_material(session, material_file: str | Path, *, thickness_mm: float = 4.0, timeout: float = 30.0) -> dict:
    """Run the material operation, retaining its original failure during cleanup."""
    context = {}
    try:
        result = _apply_material(session, material_file, thickness_mm=thickness_mm, timeout=timeout, context=context)
    except Exception as error:
        result = {"ok": False, "error": "MATERIAL_OPERATION_EXCEPTION",
                  "exception": {"type": type(error).__name__, "message": str(error)}}
    if not result.get("ok"):
        result["material_cleanup"] = _cleanup_material_failure(session, context)
    return result


ACTION_LABELS = frozenset({"提取", "确认", "反向", "未使用的面片"})


def part_count_from_rows(rows, window_rect=None):
    """Require a unique explicit count; absence never means zero parts."""
    observed = []
    for row in rows:
        if row.get("type") != "TextControl" or row.get("offscreen") or row.get("enabled") is False:
            continue
        bounds = row.get("rect")
        if window_rect is not None:
            if not bounds:
                continue
            l, t, r, b = bounds
            wl, wt, wr, wb = window_rect
            if not (wl <= l < r <= wr and wt <= t < b <= wb):
                continue
        match = re.fullmatch(r"\s*(\d+)\s+(?:零件\(s\)|parts?\s*\(s\)|parts?)\s*", str(row.get("name") or ""), re.IGNORECASE)
        if match:
            observed.append(int(match.group(1)))
    return {"ok": len(observed) == 1, "count": observed[0] if len(observed) == 1 else None,
            "observations": observed, "error": None if len(observed) == 1 else "PART_COUNT_UNAVAILABLE"}


def _part_count(session):
    return part_count_from_rows(ui.collect(session.main_window), ui.rect(session.main_window))


def _tree_items(root):
    items = []
    if root is None:
        return items
    for control, _ in ui.walk(root):
        try:
            if _visible(control) and control.ControlTypeName == "TreeItemControl":
                name = str(control.Name or "").strip()
                if name:
                    items.append((name, control))
        except Exception:
            continue
    return items


def _import_confirmation(session, timeout=30):
    """Accept only the observed import-geometry confirmation in this process."""
    last_windows = []
    def locate():
        nonlocal last_windows
        last_windows = ui.windows(session.pid)
        found = []
        for window in last_windows:
            if not window.visible or window.pid != session.pid:
                continue
            root = ui.control_from_handle(window.hwnd)
            try:
                is_import = ("导入零件" in str(window.title or "") or "Import Part" in str(window.title or ""))
                if not is_import and root.ClassName != "AutoForm::AFMessageBox":
                    continue
                texts = [str(row.get("name") or "") for row in ui.collect(root)]
                if not is_import and not any("导入" in item or "import" in item.casefold() for item in texts):
                    continue
                yes = _by_id(root, "YesButton", suffix=True)
                if yes is not None:
                    found.append((window.hwnd, yes))
            except Exception:
                continue
        return found[0] if len(found) == 1 else None
    observed = _wait(locate, timeout)
    if observed is None:
        return {"ok": False, "error": "IMPORT_CONFIRMATION_NOT_FOUND"}
    hwnd, button = observed
    clicked = _click(session, button, root_hwnd=hwnd, focus=False)
    return {"ok": bool(clicked.get("ok")), "button": clicked, "hwnd": hwnd}


def step_part_identity(text):
    """Read named product/solid labels for this single-solid STEP route only.

    This does not parse or validate CAD topology. Part 21 X2/X4 strings are
    decoded per https://www.steptools.com/stds/step/IS_final_p21e3.html.
    """
    def decode(value):
        value = value.replace("''", "'")
        def replace(match):
            return bytes.fromhex(match.group(2)).decode("utf-16-be" if match.group(1) == "2" else "utf-32-be")
        return re.sub(r"\\X([24])\\([0-9A-Fa-f]+)\\X0\\", replace, value)
    def named(entity):
        pattern = r"#\d+\s*=\s*" + entity + r"\s*\(\s*'((?:[^']|'')*)'"
        values = [decode(value) for value in re.findall(pattern, text, re.IGNORECASE)]
        return [value for value in values if value and value.upper() != "NONE"]
    try:
        products, solids = named("PRODUCT"), named("MANIFOLD_SOLID_BREP")
    except (ValueError, UnicodeError) as error:
        return {"ok": False, "error": "STEP_NAME_ENCODING_UNREADABLE", "detail": str(error)}
    ok = len(products) == len(solids) == 1
    return {"ok": ok, "products": products, "solids": solids,
            "expected_part_names": [products[0] + "-" + solids[0]] if ok else [],
            "error": None if ok else "SINGLE_SOLID_STEP_IDENTITY_REQUIRED",
            "scope": "source product/solid names only; no geometry validation"}


def imported_part_evidence(rows, expected_names, *, main_enabled, secondary_windows=()):
    """Accept the actual named import and its extraction entry, never preview alone."""
    current = [row for row in rows if not row.get("offscreen") and row.get("enabled") is not False]
    count = part_count_from_rows(current)
    tree = [str(row.get("name") or "").strip() for row in current if row.get("type") == "TreeItemControl"]
    parts = [name for name in tree if name and name not in ACTION_LABELS]
    extract_count = tree.count("提取")
    names_match = len(parts) == 1 and parts[0] in expected_names
    ok = bool(count.get("count") == 1 and names_match and extract_count == 1
              and main_enabled and not secondary_windows)
    return {"ok": ok, "part_count": count, "part_names": parts,
            "expected_part_names": list(expected_names), "identity_matches": names_match,
            "extract_entries": extract_count, "main_enabled": bool(main_enabled),
            "secondary_windows": list(secondary_windows), "geometry_validity_verified": False}


def _wait_import_completion(session, expected_names, timeout):
    deadline = time.monotonic() + timeout
    confirmation = {"observed": False, "accepted": False}
    stable_key, stable_since, stable_samples = None, None, 0
    last = {}
    while time.monotonic() < deadline:
        windows = ui.windows(session.pid)
        extras = [window for window in windows if window.visible and window.hwnd != session.hwnd]
        if extras and not confirmation["observed"]:
            observed = _import_confirmation(session, timeout=0)
            if observed.get("ok"):
                confirmation = {"observed": True, "accepted": True, "response": observed}
                stable_key, stable_since, stable_samples = None, None, 0
                time.sleep(.2)
                continue
            if observed.get("error") != "IMPORT_CONFIRMATION_NOT_FOUND":
                return {"ok": False, "error": "IMPORT_CONFIRMATION_CLICK_FAILED", "confirmation": observed}
        main = [window for window in windows if window.hwnd == session.hwnd]
        rows = _rows(_page(session, "AutoForm::PartImportPageView")) if not extras else []
        last = imported_part_evidence(rows, expected_names,
                                      main_enabled=len(main) == 1 and main[0].enabled,
                                      secondary_windows=[window.hwnd for window in extras])
        if last["ok"]:
            key = tuple(last["part_names"])
            now = time.monotonic()
            if key != stable_key:
                stable_key, stable_since, stable_samples = key, now, 1
            else:
                stable_samples += 1
            if stable_samples >= 2 and now - stable_since >= .4:
                return {"ok": True, "after": last, "confirmation": confirmation,
                        "completion_mode": "confirmed_then_named_part" if confirmation["accepted"] else "named_part_without_confirmation",
                        "stable_samples": stable_samples, "stable_seconds": now - stable_since}
        else:
            stable_key, stable_since, stable_samples = None, None, 0
        time.sleep(.2)
    return {"ok": False, "error": "IMPORTED_PART_NOT_CONFIRMED", "after": last, "confirmation": confirmation}


def import_part(session, part_path, *, timeout=90):
    """Import the specified STEP into this session's already-owned empty design."""
    path = Path(part_path).expanduser().resolve()
    if not path.is_file() or path.suffix.casefold() not in {".step", ".stp"}:
        return {"ok": False, "error": "STEP_INPUT_INVALID", "path": str(path)}
    identity = step_part_identity(path.read_bytes().decode("latin-1"))
    if not identity.get("ok"):
        return {"ok": False, "error": "STEP_IDENTITY_UNVERIFIED", "identity": identity}
    opened = ui.open_page(session, "part")
    if not opened.get("ok"):
        return {"ok": False, "error": "PART_STAGE_NOT_REACHED", "opened": opened}
    before = _part_count(session)
    if before.get("count") != 0 or not before.get("ok"):
        return {"ok": False, "error": "EMPTY_PART_COUNT_REQUIRED", "before": before}
    buttons = [_unique(session.main_window, name=label, types={"ButtonControl"})
               for label in ("导入零件...", "导入零件", "Import Part...")]
    buttons = [button for button in buttons if button is not None]
    if len(buttons) != 1:
        return {"ok": False, "error": "IMPORT_BUTTON_NOT_UNIQUE", "count": len(buttons)}
    clicked = _click(session, buttons[0])
    if not clicked.get("ok"):
        return {"ok": False, "error": "IMPORT_BUTTON_CLICK_FAILED", "click": clicked}
    dialog = ui.wait_file_dialog(session.pid, timeout=30)
    if dialog is None:
        return {"ok": False, "error": "IMPORT_FILE_DIALOG_NOT_FOUND"}
    filled = ui.fill_file_dialog(dialog.hwnd, path)
    if not filled.get("ok"):
        return {"ok": False, "error": "IMPORT_FILE_DIALOG_FAILED", "file_dialog": filled}
    picker_gone = _wait(lambda: not any(w.visible and w.class_name == "#32770" for w in ui.windows(session.pid)), 10)
    if not picker_gone:
        return {"ok": False, "error": "IMPORT_PICKER_STILL_OPEN", "file_dialog": filled}
    time.sleep(1.5)
    completed = _wait_import_completion(session, identity["expected_part_names"], timeout)
    return {**completed,
            "part_path": str(path), "part_count_before": before["count"],
            "part_count_after": completed.get("after", {}).get("part_count", {}).get("count"),
            "file_dialog": filled, "input_identity": identity,
            "geometry_validity_verified": False}


def extract_part(session, *, timeout=60):
    """Extract the imported part, retaining the observed non-action part rows."""
    opened = ui.open_page(session, "part")
    if not opened.get("ok"):
        return {"ok": False, "error": "PART_STAGE_NOT_REACHED", "opened": opened}
    page = _page(session, "AutoForm::PartSetupPageView")
    before = _tree_items(page)
    buttons = [control for name, control in before if name == "提取"]
    if len(buttons) != 1:
        return {"ok": False, "error": "EXTRACT_ACTION_NOT_UNIQUE", "rows": [name for name, _ in before]}
    clicked = _click(session, buttons[0])
    if not clicked.get("ok"):
        return {"ok": False, "error": "EXTRACT_ACTION_CLICK_FAILED", "click": clicked}
    last = []
    def completed():
        nonlocal last
        last = [name for name, _ in _tree_items(_page(session, "AutoForm::PartSetupPageView"))]
        parts = [name for name in last if name not in ACTION_LABELS]
        count = _part_count(session)
        return {"rows": last, "part_rows": parts, "part_count": count} if "提取" not in last and parts and count.get("count") == 1 else None
    after = _wait(completed, timeout, interval=.5)
    return {"ok": after is not None, "error": None if after else "EXTRACTION_NOT_CONFIRMED",
            "action": "提取", "before": [name for name, _ in before], "after": after or {"rows": last},
            "action_row_gone": bool(after), "geometry_validity_verified": False, "click": clicked}


def _select_bending_branch(session, *, timeout=180):
    container = _page(session, "AutoForm::FeasibilityContainerView")
    button = _unique(container, name="弯曲零件", types={"ButtonControl"})
    if button is None:
        return {"ok": False, "error": "BENDING_BRANCH_ENTRY_NOT_FOUND"}
    clicked = _click(session, button)
    if not clicked.get("ok"):
        return {"ok": False, "error": "BENDING_BRANCH_CLICK_FAILED", "click": clicked}
    observed = _wait(lambda: _page(session, FORMCHECK_BRANCH_PAGE), timeout)
    return {"ok": observed is not None, "error": None if observed is not None else "BENDING_BRANCH_NOT_REACHED",
            "branch": "弯曲零件", "click": clicked}


def persisted_direction_matches(observed, target):
    axes = ("X", "Y'", "Z''")
    actual = observed.get("angles_deg", {})
    valid = bool(observed.get("ok") and isinstance(target, dict) and set(target) == set(axes) and set(actual) == set(axes))
    delta = {}
    if valid:
        for axis in axes:
            a, b = actual[axis], target[axis]
            if not all(isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) for value in (a, b)):
                valid = False
                break
            delta[axis] = (a-b+180) % 360-180
    return {"ok": bool(valid and all(abs(value) <= .1 for value in delta.values())), "delta_deg": delta,
            "dropdown_is_persisted_direction": False}


def read_part_state(session, material_stem, thickness_mm=4.0, expected_angles=None):
    """Read stable part semantics without creating or correcting any business value."""
    checks = {}
    # PartSetup retains the last activity (e.g. forming check). The observed
    # entity count and extracted-part tree belong to its Import presenter.
    checks["part_page"] = ui.open_page(session, "part.import")
    if not checks["part_page"].get("ok"):
        return {"ok": False, "error": "PART_READBACK_PAGE_FAILED", "values": {}, "checks": checks}
    checks["part_count"] = _part_count(session)
    part_rows = sorted(name for name, _ in _tree_items(_page(session, "AutoForm::PartImportPageView")) if name not in ACTION_LABELS)
    checks["part_rows"] = {"ok": bool(part_rows), "names": part_rows}
    checks["material_page"] = ui.open_page(session, "part.material")
    checks["material"] = material_matches(_rows(_page(session, "AutoForm::MaterialPageView")), material_stem, thickness_mm) if checks["material_page"].get("ok") else {"ok": False}
    checks["direction_page"] = ui.open_page(session, "part.tip")
    angle_tab = _unique(_page(session, TIP_PAGE), name="冲压方向角度", types={"CheckBoxControl", "TabItemControl"}) if checks["direction_page"].get("ok") else None
    checks["angle_tab"] = _click(session, angle_tab) if angle_tab is not None else {"ok": False}
    checks["direction"] = read_direction_angles(session) if checks["angle_tab"].get("ok") else {"ok": False, "angles_deg": {}}
    if expected_angles is not None:
        checks["direction_persistence"] = persisted_direction_matches(checks["direction"], expected_angles)
    observed_material = checks["material"].get("observed", [])
    thickness_values = sorted(set(item["thickness_mm"] for item in observed_material))
    values = {"part_count": checks["part_count"].get("count"), "part_names": part_rows,
              "material_stem": material_stem if checks["material"].get("ok") else None,
              "thickness_mm": thickness_values[0] if len(thickness_values) == 1 else None,
              "angles_deg": {axis: (value + 180) % 360 - 180
                             for axis, value in checks["direction"].get("angles_deg", {}).items()} or None}
    ok = checks["part_count"].get("count") == 1 and all(item.get("ok") for item in checks.values()) and values["thickness_mm"] is not None
    return {"ok": bool(ok), "values": values, "checks": checks,
            "error": None if ok else "PART_SEMANTIC_READBACK_FAILED"}
