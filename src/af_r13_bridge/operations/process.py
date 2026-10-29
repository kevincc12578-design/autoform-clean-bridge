"""Process actions derived from recording session-20261028-114837-d70207e2.

Selected business code is maintained locally with the new Bridge input layer.
Historical success is not inherited. Disk persistence is a separate lifecycle gate.
"""

from __future__ import annotations

from PIL import ImageGrab

from .ui import common, uia


def _click(session, control, **kwargs):
    click = common.click
    result = click(session, control, **kwargs)
    if isinstance(result, dict) and result.get("ok") is False:
        raise RuntimeError(result.get("error") or "CLICK_FAILED")
    return result


def _wait(predicate, timeout=8.0):
    wait_for = common.wait_for
    result = wait_for(predicate, timeout=timeout, interval=0.25)
    if not result:
        raise RuntimeError("POSTCONDITION_TIMEOUT")
    return result


def _visible_controls(root, *, name=None, class_name=None, control_type=None):
    if root is None:
        return []
    root_rect = uia.control_rect(root)
    found = []
    for ctrl, _ in uia.walk(root):
        try:
            if name is not None and (ctrl.Name or "").strip() != name:
                continue
            if class_name is not None and ctrl.ClassName != class_name:
                continue
            if control_type is not None and ctrl.ControlTypeName != control_type:
                continue
            if bool(ctrl.IsOffscreen):
                continue
            rect = uia.control_rect(ctrl)
            if not rect or rect[0] >= rect[2] or rect[1] >= rect[3]:
                continue
            if root_rect and (rect[2] <= root_rect[0] or rect[0] >= root_rect[2]
                              or rect[3] <= root_rect[1] or rect[1] >= root_rect[3]):
                continue
            found.append(ctrl)
        except Exception:
            continue
    return found


def _one(root, **query):
    found = _visible_controls(root, **query)
    if len(found) != 1:
        raise RuntimeError(f"CONTROL_NOT_UNIQUE: {query}, count={len(found)}")
    return found[0]


def _button(root, name):
    return _one(root, name=name, control_type="ButtonControl")


def _editor(session):
    found = _visible_controls(session.main_window, class_name="AutoForm::ToolDefinitionEditorView")
    return found[0] if len(found) == 1 else None


def _reference_view(session):
    found = _visible_controls(session.main_window, class_name="AutoForm::ReferenceGeometryView")
    return found[0] if len(found) == 1 else None


def _active_process_page(session):
    return _one(session.main_window, class_name="AutoForm::ProcessPageView")


def _require_operation(session):
    # The chain activates C-20 through its recorded operation tab beforehand.
    # Never fall back to the old page+(78,5/8/11/14) context-click sequence.
    page = _active_process_page(session)
    tabs = _visible_controls(page, class_name="AFFixedFontSizeToolButton", control_type="CheckBoxControl")
    names = {(ctrl.Name or "").strip() for ctrl in tabs}
    if not {"模具", "测量"}.issubset(names):
        raise RuntimeError("OPERATION_CONTEXT_NOT_ACTIVE")
    return page


def _process_tab(session, name):
    page = _active_process_page(session)
    found = _visible_controls(page, name=name, class_name="AFFixedFontSizeToolButton", control_type="CheckBoxControl")
    if not found:
        raise RuntimeError(f"PROCESS_TAB_NOT_FOUND: {name}")
    tab = min(found, key=lambda ctrl: uia.control_rect(ctrl)[1])
    _click(session, tab)
    _wait(lambda: _radio_selected(tab) is True)
    return {"ok": True, "name": name, "rect": uia.control_rect(tab)}


def _open_reference_tab(session):
    _require_operation(session)
    return _process_tab(session, "参考")


def inspect_model_colors(image) -> dict:
    """Report red validation text; color is corroborating evidence, not geometry."""
    pixels = list(image.convert("RGB").get_flattened_data())
    if not pixels:
        return {"ok": False, "error": "EMPTY_MODEL_CROP"}
    red = sum(r >= 150 and r - g >= 45 and r - b >= 30 for r, g, b in pixels)
    luminance = sorted((r + g + b) // 3 for r, g, b in pixels)
    contrast = luminance[int((len(luminance) - 1) * .95)] - luminance[int((len(luminance) - 1) * .05)]
    return {"ok": red < 3 and contrast >= 12, "red_pixels": red,
            "pixel_count": len(pixels), "contrast": contrast,
            "nonred": red < 3, "readable_crop": contrast >= 12}


def _model_colors(session) -> dict:
    if not session.focus():
        return {"ok": False, "error": "WINDOW_FOCUS_FAILED"}
    view = _reference_view(session)
    if view is None:
        return {"ok": False, "error": "REFERENCE_VIEW_NOT_FOUND"}
    geometry = _one(view, class_name="AutoForm::ToolGeometryView")
    rect = uia.control_rect(geometry)
    image = ImageGrab.grab(bbox=rect, all_screens=True)
    return {**inspect_model_colors(image), "rect": list(rect)}


def read_recorded_reference(session) -> dict:
    """Reopen the committed definition, inspect selected input, then cancel.

    Calling this on a newly reopened AFD supplies the workflow's persistence
    readback. The function itself never claims that a disk reopen happened.
    """
    evidence = {"ok": False, "persistence_verified": False}
    try:
        _open_reference_tab(session)
        view = _reference_view(session)
        if view is None:
            return {**evidence, "error": "REFERENCE_VIEW_NOT_FOUND"}
        evidence["model_colors"] = _model_colors(session)
        define = _button(view, "定义...")
        _click(session, define)
        panel = _wait(lambda: _editor(session))
        all_out = _button(panel, "全部输出")
        evidence["committed_input_present"] = bool(all_out.IsEnabled)
        _click(session, _button(panel, "取消"))
        _wait(lambda: _editor(session) is None)
        evidence["editor_closed"] = True
        evidence["ok"] = bool(evidence["model_colors"].get("ok") and evidence["committed_input_present"])
        evidence["error"] = None if evidence["ok"] else "REFERENCE_GEOMETRY_NOT_CONFIRMED"
    except Exception as exc:
        evidence["error"] = str(exc)
        # This readback must not leave a definition editor over subsequent steps.
        panel = _editor(session)
        if panel is not None:
            try:
                _click(session, _button(panel, "取消"))
            except Exception:
                evidence["editor_cleanup_failed"] = True
    return evidence


def add_recorded_reference(session, reverse: bool = False) -> dict:
    """参考 → 添加参考模型 → 定义 → 全部输入 → [反转] → 确定.

    The accepted recording does not reverse reference geometry. ``reverse`` is
    explicit so the previous unconditional action cannot silently return.
    """
    result = {"ok": False, "reverse_requested": reverse, "persistence_verified": False, "steps": []}
    if not isinstance(reverse, bool):
        return {**result, "error": "INVALID_REVERSE_VALUE"}
    if reverse:
        return {**result, "error": "REFERENCE_REVERSE_NOT_IN_VERIFIED_ROUTE"}
    try:
        _open_reference_tab(session)
        existing = _reference_view(session) is not None
        if existing:
            if reverse:
                return {**result, "error": "EXISTING_REFERENCE_REVERSE_STATE_UNKNOWN"}
            readback = read_recorded_reference(session)
            return {**result, "ok": bool(readback.get("ok")), "already_present": True,
                    "readback": readback, "error": readback.get("error")}
        _click(session, _button(session.main_window, "添加参考模型"))
        result["steps"].append("添加参考模型")
        view = _wait(lambda: _reference_view(session))
        _click(session, _button(view, "定义..."))
        panel = _wait(lambda: _editor(session))
        _click(session, _button(panel, "全部输入"))
        result["steps"].append("全部输入")
        # Enabled 全部输出 means the selection has input geometry, beyond the
        # mere presence of the reference row or an issued click.
        _wait(lambda: bool(_button(_editor(session), "全部输出").IsEnabled))
        if reverse:
            _click(session, _button(_editor(session), "反转"))
            result["steps"].append("反转")
        _click(session, _button(_editor(session), "确定"))
        result["steps"].append("确定")
        _wait(lambda: _editor(session) is None)
        result["readback"] = read_recorded_reference(session)
        result["ok"] = bool(result["readback"].get("ok"))
        result["error"] = result["readback"].get("error")
        # A successful normal reversal action still lacks a numeric normal
        # direction readback. Do not promote that unobserved mode to verified.
        if reverse:
            result.update(ok=False, error="REFERENCE_REVERSE_DIRECTION_NOT_VERIFIED")
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _measurement_plan_table(page):
    scenarios = _one(page, class_name="AutoForm::MeasurementScenariosView")
    return _one(scenarios, class_name="AFDragTreeWidget", control_type="TreeControl")


def _measurement_plan_rows(page):
    """Return named plan entities from the unique measurement table.

    R13 exposes both the name cell and the empty missing-status cell of a plan
    as TreeItemControl siblings. The empty cell is not a second plan. Restrict
    the lookup to MeasurementScenariosView, so other Process trees cannot count.
    """
    table = _measurement_plan_table(page)
    return [row for row in _visible_controls(table, control_type="TreeItemControl")
            if (row.Name or "").strip()]


def ensure_recorded_springback(session) -> dict:
    """Create at most one plan and require the exact configured-state text."""
    result = {"ok": False, "persistence_verified": False}
    try:
        _require_operation(session)
        _process_tab(session, "测量")
        page = _active_process_page(session)
        rows = _measurement_plan_rows(page)
        if len(rows) > 1:
            return {**result, "error": "AMBIGUOUS_MEASURE_PLAN", "plans": [r.Name for r in rows]}
        result["already_present"] = bool(rows)
        if not rows:
            buttons = _visible_controls(page, name="添加测量方案", control_type="ButtonControl")
            if len(buttons) == 1:
                _click(session, buttons[0])
            else:
                table = _measurement_plan_table(page)
                # Recorded event 134: (233,203) inside table (0,133,450,253).
                # Derive the current hit from that table, never screen pixels.
                _click(session, table, relative=(233 / 450, 70 / 120))
            def created_plan():
                current = _measurement_plan_rows(_active_process_page(session))
                if len(current) > 1:
                    raise RuntimeError("AMBIGUOUS_MEASURE_PLAN")
                return current if current else None
            rows = _wait(created_plan)
        _click(session, rows[0])
        _process_tab(session, "约束条件")
        state = _springback_state(session)
        if not state.get("ok"):
            _click(session, _button(_active_process_page(session), "自由回弹"))
            state = _wait(lambda: _springback_state(session))
        result.update(ok=True, readback=state, plans=[r.Name for r in rows])
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _springback_state(session):
    page = _active_process_page(session)
    texts = [(ctrl.Name or "").strip() for ctrl in _visible_controls(page, control_type="TextControl")]
    return {"ok": "执行了自由回弹" in texts, "kind": "自由回弹", "texts": texts}


def set_recorded_friction(session) -> dict:
    result = {"ok": False, "persistence_verified": False}
    try:
        context = select_press_line(session)
        if not context.get("ok"):
            return {**result, "error": "PRESS_LINE_CONTEXT_NOT_CONFIRMED", "context": context}
        _process_tab(session, "润滑")
        before = _friction_state(session)
        result["already_selected"] = bool(before.get("ok"))
        if not before.get("ok"):
            _click(session, _button(_active_process_page(session), "库仑摩擦"))
        state = _wait(lambda: _friction_state(session))
        if not isinstance(state, dict) or state.get("ok") is not True:
            return {**result, "error": "FRICTION_READBACK_NOT_CONFIRMED", "readback": state}
        result.update(ok=True, readback=state)
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _friction_state(session):
    page = _active_process_page(session)
    texts = [(ctrl.Name or "").strip() for ctrl in _visible_controls(page, control_type="TextControl")]
    confirmed = "摩擦系数" in texts or "增强库仑定律" in texts
    parameters = []
    for control in _visible_controls(page):
        if control.ControlTypeName not in ("SpinnerControl", "EditControl", "ComboBoxControl"):
            continue
        try:
            pattern = control.GetValuePattern()
            value = pattern.Value if pattern else None
        except Exception:
            value = None
        if value is not None:
            parameters.append({"name": (control.Name or "").strip(), "value": str(value),
                               "kind": control.ControlTypeName})
    parameters.sort(key=lambda item: (item["name"], item["kind"], item["value"]))
    return {"ok": confirmed, "selected": "库仑摩擦" if confirmed else None, "texts": texts,
            "parameters": parameters, "numeric_parameters_read": bool(parameters)}


def _radio_selected(control):
    """Read actual selection capability; focus alone does not prove selection."""
    if control is None:
        return None
    try:
        pattern = control.GetSelectionItemPattern()
        if pattern is not None:
            return bool(pattern.IsSelected)
    except Exception:
        pass
    try:
        pattern = control.GetTogglePattern()
        if pattern is not None:
            value = int(pattern.ToggleState)
            return bool(value) if value in (0, 1) else None
    except Exception:
        pass
    try:
        state = int(control.GetLegacyIAccessiblePattern().State)
        return bool(state & (0x2 | 0x10))
    except Exception:
        return None


def process_tab_names(session):
    try:
        page = _active_process_page(session)
    except RuntimeError:
        return []
    return [(control.Name or "").strip() for control in _visible_controls(
        page, class_name="AFFixedFontSizeToolButton", control_type="CheckBoxControl")
        if (control.Name or "").strip()]


def select_press_line(session):
    """Select the recorded press-line context, independently from C-20."""
    from . import plan
    from .ui import pages
    opened = pages.open_page(session, "process")
    if not opened.get("ok"):
        return opened
    bar = plan._one(session.main_window, class_name="AFAnimatedOPTabBar")
    if bar is None:
        return {"ok": False, "error": "PROCESS_CONTEXT_BAR_NOT_FOUND"}
    action = common.click(session, bar, relative=plan._relative(109))
    if not action.get("ok"):
        return action
    reached = common.wait_for(lambda: "润滑" in process_tab_names(session)
                             and "模具" not in process_tab_names(session), timeout=8)
    return {"ok": bool(reached), "error": None if reached else "PRESS_LINE_CONTEXT_NOT_CONFIRMED",
            "action": action}


def read_process_state(session):
    """Inspect persisted business semantics; never create or repair the model.

    Navigation and opening/cancelling the reference definition are required to
    inspect R13's properties. The stable values exclude screenshot paths,
    positions, timestamps and transient UI identity. Disk persistence belongs
    to the caller's same-file close/reopen lifecycle.
    """
    from . import plan
    checks = {}
    try:
        checks["plan"] = plan.read_recorded_plan_assignment(session)
        checks["blank"] = plan.read_recorded_blank_state(session)
        checks["tools"] = plan.read_recorded_automatic_tools(session)
        checks["line_context"] = select_press_line(session)
        if checks["line_context"].get("ok"):
            _process_tab(session, "润滑")
            checks["friction"] = _friction_state(session)
        checks["operation_context"] = plan._select_operation_context(session)
        if checks["operation_context"].get("ok"):
            checks["reference"] = read_recorded_reference(session)
            _process_tab(session, "测量")
            rows = _measurement_plan_rows(_active_process_page(session))
            names = [(row.Name or "").strip() for row in rows]
            checks["measurement"] = {"ok": len(rows) == 1, "plans": names}
            if len(rows) == 1:
                _click(session, rows[0])
                _process_tab(session, "约束条件")
                checks["springback"] = _springback_state(session)
        required = ("plan", "blank", "tools", "line_context", "friction", "operation_context",
                    "reference", "measurement", "springback")
        failed = [name for name in required if checks.get(name, {}).get("ok") is not True]
        tool_values = [{"tool": item.get("tool"), "model_mode": item.get("model_mode"),
                        "automatic_contact": item.get("automatic_contact")}
                       for item in checks.get("tools", {}).get("tools", [])]
        values = {
            "operation": checks.get("plan", {}).get("operation"),
            "draw_region_assignment": checks.get("plan", {}).get("operation_assignment_verified"),
            "blank_contour_committed": checks.get("blank", {}).get("submission_verified"),
            "blank_source_identity": checks.get("blank", {}).get("source_names") or None,
            "automatic_tools": tool_values,
            "friction": {"kind": checks.get("friction", {}).get("selected"),
                         "parameters": checks.get("friction", {}).get("parameters", [])},
            "reference_input_present": checks.get("reference", {}).get("committed_input_present"),
            "reference_orientation": None,
            "measurement_plans": checks.get("measurement", {}).get("plans", []),
            "free_springback": checks.get("springback", {}).get("ok"),
        }
        return {"ok": not failed, "values": values, "checks": checks, "read_only": True,
                "persistent_verified": False, "missing": failed,
                "error": "BUSINESS_READBACK_INCOMPLETE" if failed else None}
    except Exception as exc:
        return {"ok": False, "values": {}, "checks": checks, "error": str(exc),
                "read_only": True, "persistent_verified": False}

