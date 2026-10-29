"""Actions independently wired to the correct 2026-10-28 recording.

Selected verified business actions are maintained in this package.
Navigation and guarded inputs use the new Bridge adapter; no historical package
is imported or executed. Current-run persistence is verified by the lifecycle.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import uuid

import uiautomation as auto
from PIL import Image, ImageGrab

from .ui import common, pages, uia, win32
from . import process
from ..isolation import sanitized_environment

CALIBRATION = Path(__file__).resolve().parent / "calibration"
RECORDING = CALIBRATION
U_GLYPH_REFERENCE = CALIBRATION / "recorded-c20-draw-u.png"


def _fail(error, **evidence):
    return {"ok": False, "error": error, **evidence}


def _controls(root, *, name=None, class_name=None, type_name=None):
    if root is None:
        return []
    found = []
    for control, _ in uia.walk(root):
        try:
            if bool(control.IsOffscreen):
                continue
            if name is not None and (control.Name or "").strip() != name:
                continue
            if class_name is not None and control.ClassName != class_name:
                continue
            if type_name is not None and control.ControlTypeName != type_name:
                continue
            rect = uia.control_rect(control)
            if rect and rect[2] > rect[0] and rect[3] > rect[1]:
                found.append(control)
        except Exception:
            continue
    return found


def _one(root, **criteria):
    matches = _controls(root, **criteria)
    return matches[0] if len(matches) == 1 else None


def _names(root):
    names = []
    for control, _ in uia.walk(root):
        try:
            if not control.IsOffscreen and control.Name:
                names.append(control.Name.strip())
        except Exception:
            continue
    return names


def _selected(control):
    if control is None:
        return None
    try:
        return bool(control.GetSelectionItemPattern().IsSelected)
    except Exception:
        pass
    try:
        value = control.GetTogglePattern().ToggleState
        return bool(int(value)) if int(value) in (0, 1) else None
    except Exception:
        pass
    try:
        state = int(control.GetLegacyIAccessiblePattern().State)
        return bool(state & (0x2 | 0x10))  # selected or checked
    except Exception:
        return None


@lru_cache(maxsize=1)
def _recorded():
    rows = json.loads((RECORDING / "steps.json").read_text(encoding="utf-8-sig"))
    return {row["event_id"]: row for row in rows}


def _descriptor(event_id, parent_class=None):
    record = _recorded()[event_id]
    target = record["target_before"]
    if parent_class:
        matches = [parent for parent in target["parents"] if parent["class_name"] == parent_class]
        if len(matches) != 1:
            raise ValueError(f"recording lacks unique {parent_class} at event {event_id}")
        return matches[0]
    return target["control"]


def _relative(event_id, *, parent_class=None):
    record = _recorded()[event_id]
    left, top, right, bottom = _descriptor(event_id, parent_class)["rect"]
    return ((record["x"]-left)/(right-left), (record["y"]-top)/(bottom-top))


def _point(control, relative):
    left, top, right, bottom = uia.control_rect(control)
    return (round(left+(right-left)*relative[0]), round(top+(bottom-top)*relative[1]))


def _page(session, class_name):
    return _one(session.main_window, class_name=class_name)


def _tab(session, name, *, within=None):
    root = within or session.main_window
    candidates = _controls(root, name=name, class_name="AFFixedFontSizeToolButton")
    if len(candidates) != 1:
        return _fail("TAB_NOT_UNIQUE", tab=name, candidates=len(candidates))
    action = common.click(session, candidates[0])
    if not action.get("ok"):
        return action
    if not common.wait_for(lambda: _selected(candidates[0]) is True, timeout=4):
        return _fail("TAB_SELECTION_NOT_CONFIRMED", tab=name, click=action)
    return {"ok": True, "tab": name, "click": action}


def _library_icon(library):
    """Match the recorded unnamed U icon within its current library layout."""
    reference = _descriptor(61)
    parent = _descriptor(61, "AutoForm::ProductionLineLibraryView")
    recorded_rect, parent_rect = reference["rect"], parent["rect"]
    expected = (((recorded_rect[0]+recorded_rect[2])/2-parent_rect[0])/(parent_rect[2]-parent_rect[0]),
                ((recorded_rect[1]+recorded_rect[3])/2-parent_rect[1])/(parent_rect[3]-parent_rect[1]))
    current = uia.control_rect(library)
    width, height = current[2]-current[0], current[3]-current[1]
    matches = []
    for icon in _controls(library, class_name="AutoForm::OperationIcon"):
        rect = uia.control_rect(icon)
        center = (((rect[0]+rect[2])/2-current[0])/width,
                  ((rect[1]+rect[3])/2-current[1])/height)
        distance = math.hypot(center[0]-expected[0], center[1]-expected[1])
        size_x = (rect[2]-rect[0])/width / ((recorded_rect[2]-recorded_rect[0])/(parent_rect[2]-parent_rect[0]))
        size_y = (rect[3]-rect[1])/height / ((recorded_rect[3]-recorded_rect[1])/(parent_rect[3]-parent_rect[1]))
        if distance < .045 and .7 <= size_x <= 1.3 and .7 <= size_y <= 1.3:
            matches.append((icon, {"rect": list(rect), "layout_distance": distance,
                                   "normalized_size_ratio": [size_x, size_y]}))
    return matches[0] if len(matches) == 1 else (None, {"candidates": len(matches)})


def _bay_state(session):
    bay = _page(session, "AutoForm::ProductionLineDnDBayView")
    if bay is None:
        return {"ok": False, "error": "PRODUCTION_LINE_BAY_NOT_FOUND"}
    names = _names(bay)
    icons = _controls(bay, class_name="AutoForm::OperationIcon")
    return {"ok": "C-20" in names and bool(icons), "operation": "C-20" if "C-20" in names else None,
            "names": names, "operation_icons": len(icons), "rect": list(uia.control_rect(bay))}


def _plan_tree(session):
    """Select only a unique tree or the observed nested frozen-column pair.

    Width is not a general disambiguator. Every candidate must belong to the
    same unique ProcessLayoutView and share its left/top/bottom with all other
    candidates. Equal-width duplicates or unrelated trees remain errors.
    """
    views = _controls(session.main_window, class_name="AutoForm::ProcessLayoutView")
    if len(views) != 1:
        return None, None, _fail("PROCESS_LAYOUT_VIEW_NOT_UNIQUE", candidates=len(views))
    view = views[0]
    trees = _controls(view, class_name="AutoForm::ProcessLayoutTreeView")
    rectangles = [uia.control_rect(tree) for tree in trees]
    evidence = {"view_rect": list(uia.control_rect(view)), "tree_rects": [list(rect) for rect in rectangles]}
    if not trees:
        return None, view, _fail("PROCESS_LAYOUT_TREE_MISSING", **evidence)
    outer = uia.control_rect(view)
    if any(not (outer[0] <= rect[0] < rect[2] <= outer[2] and
                outer[1] <= rect[1] < rect[3] <= outer[3]) for rect in rectangles):
        return None, view, _fail("PROCESS_LAYOUT_TREE_OUTSIDE_VIEW", **evidence)
    if len(trees) == 1:
        return trees[0], view, {"ok": True, "method": "unique_tree", **evidence}
    edges = {(rect[0], rect[1], rect[3]) for rect in rectangles}
    widths = [rect[2]-rect[0] for rect in rectangles]
    if len(edges) != 1 or len(set(widths)) != len(widths):
        return None, view, _fail("PROCESS_LAYOUT_TREE_AMBIGUOUS", **evidence)
    selected = trees[widths.index(max(widths))]
    return selected, view, {"ok": True, "method": "nested_frozen_columns", **evidence}


def _normalized_name(value):
    return "".join(str(value or "").split()).casefold()


DRAW_REGION_NAMES = {_normalized_name(value) for value in ("拉延区域 1", "Draw Region 1")}
OUTER_TRIM_NAMES = {_normalized_name(value) for value in ("外部修边", "Outer Trim")}


def _recorded_cell(tree, view=None):
    """Locate the C-20 × draw-region cell from current headers and row labels."""
    if tree is None:
        return _fail("PROCESS_LAYOUT_TREE_MISSING")
    rect = uia.control_rect(tree)
    headers = _controls(view or tree, name="C-20", type_name="HeaderControl")
    header_boxes = {uia.control_rect(header) for header in headers
                    if rect[0] <= uia.control_rect(header)[0] < uia.control_rect(header)[2] <= rect[2]
                    and rect[1] <= uia.control_rect(header)[1] < uia.control_rect(header)[3] <= rect[3]}
    rows = _controls(tree, type_name="TreeItemControl")
    draw_rows = [row for row in rows if _normalized_name(row.Name) in DRAW_REGION_NAMES]
    trim_rows = [row for row in rows if _normalized_name(row.Name) in OUTER_TRIM_NAMES]
    names = _names(tree)
    if len(header_boxes) != 1 or len(draw_rows) != 1 or len(trim_rows) != 1:
        return _fail("PLAN_CELL_LABELS_NOT_VERIFIED", names=names, headers=[list(box) for box in header_boxes],
                     draw_rows=len(draw_rows), outer_trim_rows=len(trim_rows))
    header = next(iter(header_boxes))
    row = uia.control_rect(draw_rows[0])
    cell = (header[0], row[1], header[2], row[3])
    if not (rect[0] <= cell[0] < cell[2] <= rect[2] and rect[1] <= cell[1] < cell[3] <= rect[3]):
        return _fail("PLAN_CELL_OUTSIDE_TREE", cell=list(cell), tree_rect=list(rect))
    center = ((cell[0]+cell[2])/2, (cell[1]+cell[3])/2)
    return {"ok": True, "operation": "C-20", "feature": draw_rows[0].Name,
            "outer_trim": trim_rows[0].Name, "cell_rect": list(cell), "header_rect": list(header),
            "row_rect": list(row), "point": list(center),
            "relative": [(center[0]-rect[0])/(rect[2]-rect[0]), (center[1]-rect[1])/(rect[3]-rect[1])],
            "names": names}


def _glyph_ink(image, *, reference=False):
    """Extract the observed pale-gold U stroke, excluding white frame/cursor.

    The captured U is gold; the bright white outlines are the focus frame and
    cursor. The blue selection tint weakens the U's color but keeps positive
    red/green-versus-blue contrast. Background matching is never used.
    """
    rgb = image.convert("RGB")
    points = set()
    for y in range(3, rgb.height-3):
        for x in range(3, rgb.width-3):
            red, green, blue = rgb.getpixel((x, y))
            selected = red-blue >= 25 and green-blue >= 25 if reference else (
                red-blue >= 8 and green-blue >= 10 and min(red, green) >= 105)
            if selected:
                points.add((x, y))
    return points


def _dilate(points, radius=2):
    return {(x+dx, y+dy) for x, y in points
            for dx in range(-radius, radius+1) for dy in range(-radius, radius+1)}


def assignment_glyph_evidence(image):
    """Pure image check against the recorded cell; never accepts background alone."""
    with Image.open(U_GLYPH_REFERENCE) as stored:
        template = stored.convert("RGB")
    width, height = image.size
    if (width < 19 or height < 12 or width > 114 or height > 69
            or abs((width/height)/(template.width/template.height)-1) > .18):
        return _fail("ASSIGNMENT_CELL_SCALE_OR_ASPECT_MISMATCH", size=[width,height])
    normalized = image.convert("RGB").resize(template.size, Image.Resampling.LANCZOS)
    reference = _glyph_ink(template, reference=True)
    ink = _glyph_ink(normalized)
    nearby_reference, nearby_ink = _dilate(reference), _dilate(ink)
    precision = len(ink & nearby_reference)/len(ink) if ink else 0.0
    coverage = len(reference & nearby_ink)/len(reference) if reference else 0.0
    left = min(x for x,y in reference)
    right = max(x for x,y in reference)
    top = min(y for x,y in reference)
    bottom = max(y for x,y in reference)
    regions = {"left_stroke": {point for point in reference if point[0] <= left+4},
               "bottom_stroke": {point for point in reference if point[1] >= bottom-3},
               "right_upper_stroke": {point for point in reference if point[0] >= right-2 and point[1] <= top+3}}
    coverage_regions = {name: len(points & nearby_ink)/len(points) if points else 0.0
                        for name, points in regions.items()}
    top_gap_ink = sum(left+4 <= x <= right-4 and y <= top+3 for x,y in ink)
    matched = (len(ink) >= 18 and precision >= .85 and coverage >= .75
               and coverage_regions["left_stroke"] >= .75
               and coverage_regions["bottom_stroke"] >= .65
               and coverage_regions["right_upper_stroke"] >= .75 and top_gap_ink <= 4)
    return {"ok": matched, "mode": "visual_calibrated_u_glyph", "foreground_pixels": len(ink),
            "reference_foreground_pixels": len(reference), "precision": round(precision,4),
            "coverage": round(coverage,4), "region_coverage": coverage_regions,
            "top_gap_ink": top_gap_ink, "original_cell_size": [width,height],
            "normalized_size": list(template.size), "reference": str(U_GLYPH_REFERENCE),
            "reference_sha256": hashlib.sha256(U_GLYPH_REFERENCE.read_bytes()).hexdigest(),
            "background_and_focus_border_used_as_proof": False,
            "geometry_validated": False, "requires_geometry_preflight": True,
            "error": None if matched else "RECORDED_U_GLYPH_NOT_CONFIRMED"}


def _capture_assignment_cell(session, rect):
    """Read exactly the current screen cell; no focus or mouse input is sent."""
    prepared = common._prepare(session, focus=False)
    if not prepared.get("ok"):
        return prepared
    center = (round((rect[0]+rect[2])/2), round((rect[1]+rect[3])/2))
    checked = common._check_point(session, center, prepared["hwnd"])
    if not checked.get("ok"):
        return checked
    try:
        image = ImageGrab.grab(bbox=tuple(rect), all_screens=True).convert("RGB")
        folder = Path(session.directory) / "assignment-glyph-readbacks"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"cell-{uuid.uuid4().hex}.png"
        image.save(path)
        return {"ok": True, "image": image, "path": str(path), "screen_rect": list(rect),
                "rgb_sha256": hashlib.sha256(image.tobytes()).hexdigest(), "guard": checked}
    except Exception as exc:
        return _fail("ASSIGNMENT_CELL_CAPTURE_FAILED", detail=repr(exc))


def _public_cell_state(tree, cell_rect):
    """Keep public UIA/MSAA fields as evidence; empty fields are not assignments."""
    candidates = [control for control in _controls(tree, type_name="TreeItemControl")
                  if list(uia.control_rect(control)) == cell_rect]
    if len(candidates) != 1:
        return {"candidates": len(candidates), "assignment_semantics_verified": False}
    control = candidates[0]
    values = {}
    for name in ("Name", "HelpText", "ItemStatus", "ItemType"):
        try:
            values[name] = str(getattr(control,name) or "")
        except Exception:
            values[name] = None
    try:
        legacy = control.GetLegacyIAccessiblePattern()
        for name in ("Value", "Description", "State"):
            try:
                values[f"legacy_{name}"] = getattr(legacy,name)
            except Exception:
                values[f"legacy_{name}"] = None
    except Exception:
        pass
    return {"candidates": 1, "properties": values, "assignment_semantics_verified": False}


def _assignment_state(tree, view=None, session=None):
    """Inspect an existing icon in the semantic target cell, without assigning."""
    cell = _recorded_cell(tree, view)
    if not cell.get("ok"):
        return cell
    icons = _controls(tree, class_name="AutoForm::OperationIcon")
    left, top, right, bottom = cell["cell_rect"]
    matched = []
    for icon in icons:
        box = uia.control_rect(icon)
        center = ((box[0]+box[2])/2, (box[1]+box[3])/2)
        if left <= center[0] < right and top <= center[1] < bottom:
            matched.append(list(box))
    verified = len(matched) == 1
    evidence = {**cell, "ok": verified, "labels_verified": True, "assigned_icons": len(icons),
                "target_cell_icons": matched, "verification_mode": "uia_operation_icon" if verified else None,
                "geometry_validated": False, "requires_geometry_preflight": True,
                "error": None if verified else "EXISTING_PLAN_ASSIGNMENT_NOT_CONFIRMED"}
    if verified or session is None:
        return evidence
    evidence["cell_public_properties"] = _public_cell_state(tree, cell["cell_rect"])
    captured = _capture_assignment_cell(session, cell["cell_rect"])
    if not captured.get("ok"):
        return {**evidence, "visual_capture": captured}
    visual = assignment_glyph_evidence(captured["image"])
    evidence.update(ok=bool(visual.get("ok")), visual_glyph=visual,
                    visual_capture={key:value for key,value in captured.items() if key != "image"},
                    verification_mode="visual_calibrated_u_glyph",
                    error=None if visual.get("ok") else "EXISTING_PLAN_ASSIGNMENT_NOT_CONFIRMED")
    return evidence


def create_recorded_plan(session):
    """Create an empty plan, then drag the recorded U operation into its bay."""
    opened = pages.open_page(session, "plan")
    if not opened.get("ok"):
        return _fail("PLAN_PAGE_NOT_REACHED", opened=opened)
    before = _bay_state(session)
    if before.get("ok"):
        return {**before, "already_present": True, "persistent_verified": False}
    button = _one(session.main_window, name="创建空的工序规划", class_name="QCommandLinkButton")
    entry = None
    if button is not None:
        entry = common.click(session, button)
        if not entry.get("ok"):
            return _fail("CREATE_EMPTY_PLAN_INPUT_FAILED", action=entry)
    library = common.wait_for(lambda: _page(session, "AutoForm::ProductionLineLibraryView"), timeout=12)
    bay = _page(session, "AutoForm::ProductionLineDnDBayView")
    if library is None or bay is None:
        return _fail("PRODUCTION_LINE_LIBRARY_OR_BAY_MISSING", opened=opened, entry=entry)
    # Refresh all coordinates after focus/layout stabilization, before the drag.
    prepared = common._prepare(session)
    if not prepared.get("ok"):
        return prepared
    library = _page(session, "AutoForm::ProductionLineLibraryView")
    bay = _page(session, "AutoForm::ProductionLineDnDBayView")
    if library is None or bay is None:
        return _fail("PRODUCTION_LINE_LAYOUT_DISAPPEARED_AFTER_FOCUS")
    icon, matching = _library_icon(library)
    if icon is None:
        return _fail("RECORDED_OPERATION_ICON_LAYOUT_MISMATCH", matching=matching)
    action = common.guarded_drag(session, _point(icon, _relative(61)),
                                 _point(bay, _relative(86, parent_class="AutoForm::ProductionLineDnDBayView")),
                                 duration=1.436)
    if not action.get("ok"):
        return _fail("OPERATION_DRAG_INPUT_FAILED", action=action)
    after = common.wait_for(lambda: _bay_state(session), timeout=10)
    if not after:
        return _fail("OPERATION_DROP_NOT_CONFIRMED", action=action, after=_bay_state(session))
    return {"ok": True, "operation": "C-20", "entry": entry, "drag": action, "matching": matching,
            "after": after, "persistent_verified": False, "calibration_source": str(RECORDING / "steps.json")}


def activate_recorded_operation(session):
    """Assign the existing draw-region feature to the existing C-20 cell."""
    opened = pages.open_page(session, "plan")
    if not opened.get("ok"):
        return _fail("PLAN_PAGE_NOT_REACHED", opened=opened)
    tab = _tab(session, "工艺规划")
    if not tab.get("ok"):
        return tab
    tree, view, selector = _plan_tree(session)
    if tree is None:
        return {**selector, "tab": tab}
    cell = _recorded_cell(tree, view)
    if not cell.get("ok"):
        return {**cell, "selector": selector}
    action = common.double_click(session, tree, relative=cell["relative"])
    if not action.get("ok"):
        return _fail("PLAN_CELL_DOUBLE_CLICK_FAILED", action=action)
    def assigned():
        current_tree, current_view, current_selector = _plan_tree(session)
        return _assignment_state(current_tree, current_view, session=session) if current_tree is not None else current_selector
    after = common.wait_for(assigned, timeout=8)
    if not after:
        return _fail("PLAN_CELL_ASSIGNMENT_NOT_CONFIRMED", action=action, after=assigned())
    return {"ok": True, "operation": "C-20", "feature": "拉延区域 1", "action": action,
            "after": after, "selector": selector, "persistent_verified": False}


def read_recorded_plan_assignment(session):
    """Read the existing bay and feature assignment; never drag or double-click.

    View navigation is the only input.  Callers know whether this read follows
    an actual disk reopen; this function does not manufacture that claim.
    """
    opened = pages.open_page(session, "plan")
    if not opened.get("ok"):
        return _fail("PLAN_PAGE_NOT_REACHED", read_only=True, opened=opened)
    line_tab = _tab(session, "生产线")
    if not line_tab.get("ok"):
        return _fail("PRODUCTION_LINE_TAB_NOT_CONFIRMED", read_only=True, tab=line_tab)
    bay = _bay_state(session)
    if not bay.get("ok"):
        return _fail("EXISTING_PRODUCTION_LINE_NOT_CONFIRMED", read_only=True, bay=bay)
    plan_tab = _tab(session, "工艺规划")
    if not plan_tab.get("ok"):
        return _fail("PLAN_TAB_NOT_CONFIRMED", read_only=True, tab=plan_tab, bay=bay)
    tree, view, selector = _plan_tree(session)
    assignment = _assignment_state(tree, view, session=session) if tree is not None else selector
    verified = bool(assignment.get("ok"))
    return {"ok": verified, "read_only": True, "operation": "C-20", "bay": bay,
            "assignment": assignment, "selector": selector, "operation_assignment_verified": verified,
            "persistent_verified": False, "requires_geometry_preflight": True,
            "error": None if verified else "EXISTING_PLAN_ASSIGNMENT_NOT_CONFIRMED"}


def read_recorded_blank_state(session):
    """Read an already submitted contour without opening a geometry editor.

    A curve input showing 编辑... proves a committed curve object is present.
    It does not prove that object is closed or geometrically correct.  The
    source name is reported only if the current read exposes it; otherwise it
    remains pending alongside native geometry preflight.
    """
    opened = pages.open_page(session, "blank")
    if not opened.get("ok"):
        return _fail("BLANK_PAGE_NOT_REACHED", read_only=True, opened=opened)
    page = _page(session, "AutoForm::BlankPageView")
    if page is None:
        return _fail("BLANK_PAGE_NOT_FOUND", read_only=True)
    if _page(session, "AutoForm::PickLinesEditorView") is not None:
        return _fail("BLANK_GEOMETRY_EDITOR_STILL_ACTIVE", read_only=True)
    outline = _one(page, class_name="AutoForm::BlankLinesPageView")
    curve = _one(outline or page, name="编辑...", class_name="AFCurveInputControl")
    read = read_blank(session)
    names = _names(page)
    sources = sorted({name for name in names if "料片轮廓线" in name and "成型检查部分" in name})
    plots = []
    for control in _controls(page):
        class_name = control.ClassName or ""
        if any(token in class_name.casefold() for token in ("plot", "chart")):
            plots.append({"class_name": class_name, "name": control.Name or "",
                          "rect": list(uia.control_rect(control))})
    committed = outline is not None and curve is not None and bool(read.get("ok"))
    pending = ["closed_contour", "native_geometry_validation"]
    if len(sources) != 1:
        pending.append("source_identity")
    return {"ok": committed, "read_only": True, "submission_verified": committed,
            "contour_object_present": curve is not None,
            "outline_page_present": outline is not None,
            "curve_input": "编辑..." if curve is not None else None,
            "source_names": sources, "source_identity_verified": len(sources) == 1,
            "graphics_controls": plots, "graphics_geometry_verified": False,
            "closed_contour_verified": None, "geometry_pending": pending,
            "persistent_verified": False, "requires_geometry_preflight": True,
            "read": read, "error": None if committed else "EXISTING_BLANK_CONTOUR_NOT_CONFIRMED"}


def open_recorded_die_face(session):
    """Open the C-20 editor. This does not claim to generate new surfaces."""
    opened = pages.open_page(session, "die_face")
    if not opened.get("ok"):
        return _fail("DIE_FACE_STAGE_NOT_REACHED", opened=opened)
    button = _one(session.main_window, name="模面设计", class_name="QCommandLinkButton")
    action = None
    if button is not None:
        action = common.click(session, button)
        if not action.get("ok"):
            return action
    def editor():
        geometry = _page(session, "AutoForm::GeometryPageView")
        groups = _controls(geometry, class_name="AutoForm::GeomSetView")
        modify = _controls(geometry, class_name="AutoForm::ModifyGroupPageView")
        tabs = {control.Name.strip() for control in _controls(geometry, class_name="AFFixedFontSizeToolButton")}
        start = _controls(geometry, name="模面设计", class_name="QCommandLinkButton")
        return {"ok": geometry is not None and len(groups) == 1 and len(modify) == 1
                       and {"冲压方向", "修改"}.issubset(tabs) and not start,
                "geometry_page_present": geometry is not None, "geom_sets": len(groups),
                "modify_pages": len(modify), "tabs": sorted(tabs), "start_entry_present": bool(start),
                "operation_label_read_directly": False}
    after = common.wait_for(editor, timeout=12)
    if not after:
        return _fail("DIE_FACE_EDITOR_NOT_CONFIRMED", opened=opened, action=action, after=editor())
    left = pages.open_page(session, "blank")
    editor_left = bool(left.get("ok") and _page(session, "AutoForm::GeometryPageView") is None)
    return {"ok": editor_left, "scope": "editor_opened_and_left", "geometry_generated": False,
            "action": action, "after": after, "leave": left, "editor_left": editor_left,
            "error": None if editor_left else "DIE_FACE_EDITOR_LEAVE_NOT_CONFIRMED"}


NATIVE_MENU_CLASS = "Qt5QWindowPopupDropShadowSaveBits"
MENU_PROBE_TIMEOUT_S = 2.0
MENU_REFERENCES = (
    "https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading",
    "https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomation2-get_connectiontimeout",
)


def _native_menu_windows(pid):
    """Never request a UIA element while discovering the independent popup."""
    return [window for window in win32.enum_top_windows(pid)
            if window.pid == pid and window.visible and window.enabled
            and window.class_name == NATIVE_MENU_CLASS
            and window.rect[2] > window.rect[0] and window.rect[3] > window.rect[1]]


def _native_popup_visible(hwnd,pid):
    """A disabled-but-visible menu has not disappeared yet."""
    try:
        window = win32.window_info(hwnd)
        return bool(window.pid == pid and window.visible and window.class_name == NATIVE_MENU_CLASS)
    except Exception:
        return False


def _popup_items_readonly(hwnd, pid):
    """Worker-only UIA lookup: one known popup, one direct GetChildren call."""
    window = win32.window_info(hwnd)
    if (window.pid != pid or not window.visible or not window.enabled
            or window.class_name != NATIVE_MENU_CLASS):
        return _fail("NATIVE_MENU_IDENTITY_CHANGED")
    root = auto.ControlFromHandle(hwnd)
    if root.ClassName != "AFMenu":
        return _fail("UIA_POPUP_IS_NOT_AFMENU")
    children = root.GetChildren()
    if len(children) > 32:
        return _fail("AFMENU_CHILD_COUNT_UNEXPECTED", count=len(children))
    items = []
    for child in children:
        if (child.ControlTypeName != "MenuItemControl" or child.ClassName != "QAction"
                or child.IsOffscreen or not child.IsEnabled):
            continue
        rect = uia.control_rect(child)
        if rect and rect[2] > rect[0] and rect[3] > rect[1]:
            items.append({"name": (child.Name or "").strip(), "rect": list(rect),
                          "class_name": "QAction", "control_type": "MenuItemControl"})
    chosen = [item for item in items if item["name"] == "复制自..."]
    return {"ok": len(chosen) == 1, "hwnd": hwnd, "pid": pid, "root_class": "AFMenu",
            "native_class": NATIVE_MENU_CLASS, "popup_rect": list(window.rect), "items": items,
            "item": chosen[0] if len(chosen) == 1 else None,
            "error": None if len(chosen) == 1 else "COPY_MENU_ITEM_NOT_UNIQUE"}


def _menu_probe_worker(hwnd, pid):
    """Read only; this subprocess is disposable if a provider call hangs."""
    try:
        result = _popup_items_readonly(int(hwnd), int(pid))
    except Exception as exc:
        result = _fail("POPUP_UIA_PROBE_FAILED", detail=repr(exc))
    # ASCII JSON avoids a console-codepage round trip for Chinese menu names.
    print(json.dumps(result, ensure_ascii=True), flush=True)


def _probe_popup_bounded(hwnd, pid, timeout=MENU_PROBE_TIMEOUT_S, *, directory):
    command = [sys.executable, "-I", "-m", "af_r13_bridge.operations.plan",
               "--popup-probe", str(hwnd), str(pid)]
    folder = Path(directory) / "popup-probes"
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / uuid.uuid4().hex
    request_path = stem.with_suffix(".request.json")
    request_path.write_text(json.dumps({"command": command, "pid": pid, "hwnd": hwnd,
                                       "timeout_s": timeout}, ensure_ascii=False, indent=2), encoding="utf-8")
    started = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=str(directory), env=sanitized_environment(), stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=timeout,
                                   creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    except subprocess.TimeoutExpired:
        # subprocess.run terminates and reaps only the helper it created.
        return _fail("POPUP_UIA_PROBE_TIMEOUT", hwnd=hwnd, pid=pid, timeout_s=timeout,
                     owned_probe_only=True, references=list(MENU_REFERENCES))
    except Exception as exc:
        return _fail("POPUP_UIA_PROBE_START_FAILED", detail=repr(exc))
    stem.with_suffix(".stdout.log").write_text(completed.stdout, encoding="utf-8")
    stem.with_suffix(".stderr.log").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        return _fail("POPUP_UIA_PROBE_EXITED", exit_code=completed.returncode, stderr=completed.stderr[-2000:])
    try:
        result = json.loads(completed.stdout)
    except (ValueError, TypeError):
        return _fail("POPUP_UIA_PROBE_INVALID_JSON", stdout=completed.stdout[-2000:], stderr=completed.stderr[-2000:])
    if not isinstance(result,dict) or result.get("hwnd",hwnd) != hwnd or result.get("pid",pid) != pid:
        return _fail("POPUP_UIA_PROBE_IDENTITY_MISMATCH")
    return {**result, "probe_elapsed_s": round(time.monotonic()-started,3),
            "method": "bounded_popup_only_uia", "main_uia_tree_traversed": False,
            "request_evidence": str(request_path)}


def _popup_copy_item(session, timeout=5.0):
    """Bounded popup lookup without touching the main window's COM object."""
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        windows = _native_menu_windows(session.pid)
        if len(windows) > 1:
            return _fail("NATIVE_AFMENU_POPUP_NOT_UNIQUE", candidates=[w.hwnd for w in windows])
        if windows:
            return _probe_popup_bounded(windows[0].hwnd, session.pid,
                                         timeout=min(MENU_PROBE_TIMEOUT_S,max(.1,deadline-time.monotonic())), directory=session.directory)
        time.sleep(min(.1,max(0,deadline-time.monotonic())))
    return _fail("NATIVE_AFMENU_POPUP_NOT_FOUND", timeout_s=timeout)


def _click_copy_popup(session, popup, *, main_hwnd, expected_geometry):
    """Click a fresh data snapshot with native checks; never call common._prepare.

    Accessing main_window.NativeWindowHandle here would be another COM query
    while the menu provider is active. Both main HWND and geometry were cached
    before opening the menu instead.
    """
    if not popup.get("ok") or popup.get("pid") != session.pid:
        return _fail("COPY_POPUP_NOT_VERIFIED", popup=popup)
    item = popup.get("item") or {}
    rect = item.get("rect")
    if (popup.get("root_class") != "AFMenu" or popup.get("native_class") != NATIVE_MENU_CLASS
            or item.get("name") != "复制自..." or not rect or len(rect) != 4):
        return _fail("COPY_POPUP_ITEM_INVALID", popup=popup)
    current = win32.window_info(popup["hwnd"])
    if (current.pid != session.pid or not current.visible or not current.enabled
            or current.class_name != NATIVE_MENU_CLASS or list(current.rect) != popup.get("popup_rect")):
        return _fail("COPY_POPUP_SNAPSHOT_STALE")
    if not (current.rect[0] <= rect[0] < rect[2] <= current.rect[2]
            and current.rect[1] <= rect[1] < rect[3] <= current.rect[3]):
        return _fail("COPY_POPUP_ITEM_OUTSIDE_WINDOW")
    geometry = win32.wait_stable_geometry(main_hwnd)
    if geometry is None or geometry != expected_geometry:
        return _fail("MAIN_WINDOW_CHANGED_DURING_POPUP", expected=expected_geometry, actual=geometry)
    point = (round((rect[0]+rect[2])/2),round((rect[1]+rect[3])/2))
    proof={**current.as_dict(),'root_class':popup['root_class']}
    checked = common._check_point(session,point,popup["hwnd"],popup_proof=proof)
    if not checked.get("ok"):
        return checked
    win32.click_screen(*point)
    return {"ok":True,"method":"guarded_native_popup_snapshot","point":list(point),
            "item_rect":rect,"popup_hwnd":popup["hwnd"],"postcondition_verified":False,
            "main_uia_accessed":False,"guard":checked}


def _source_focus_state(control):
    keyboard_focus = None
    legacy_state = None
    try:
        keyboard_focus = bool(control.HasKeyboardFocus)
    except Exception:
        pass
    try:
        legacy_state = int(control.GetLegacyIAccessiblePattern().State)
    except Exception:
        pass
    return {"keyboard_focus":keyboard_focus,"legacy_state":legacy_state,
            "focused":keyboard_focus is True or legacy_state is not None and bool(legacy_state & 0x4)}


def _source_preselection_state(editor):
    """Read the recorded default source without inventing a selection interface.

    A supported selection API's false is authoritative. Qt's current/focused
    tree index is a separate property. When no selection capability exists,
    exact unique source + default focus + enabled confirmation is only the
    recorded submission precondition, never direct geometry-selection proof.
    """
    expected = _normalized_name("料片轮廓线 - 成型检查部分")
    rows = _controls(editor,type_name="TreeItemControl")
    sources = [row for row in rows if _normalized_name(row.Name) == expected]
    base = {"ok":False,"source_candidates":len(sources),"selection_read_directly":False,
            "geometry_selection_verified":False,"additional_source_click":False}
    if len(sources) != 1:
        return {**base,"error":"SOURCE_CURVE_NOT_UNIQUE","names":[row.Name for row in rows]}
    source = sources[0]
    confirm = _one(editor,name="确定",type_name="ButtonControl")
    try:
        confirm_enabled = confirm is not None and bool(confirm.IsEnabled)
        source_enabled = bool(source.IsEnabled)
    except Exception:
        confirm_enabled = source_enabled = False
    base.update(source=source.Name,source_enabled=source_enabled,confirmation_enabled=confirm_enabled)
    selection = None
    mode = None
    interfaces = {}
    for method,attribute,label in (("GetSelectionItemPattern","IsSelected","selection_item_pattern"),
                                   ("GetTogglePattern","ToggleState","toggle_pattern")):
        try:
            raw = getattr(getattr(source,method)(),attribute)
            interfaces[label] = {"supported":True,"value":raw}
            if raw in (False,True,0,1):
                selection = bool(raw)
            mode = label
            break
        except Exception as exc:
            interfaces[label] = {"supported":False,"error":type(exc).__name__}
    focus = _source_focus_state(source)
    legacy = focus["legacy_state"]
    if mode is None and legacy is not None and legacy & (0x2 | 0x10 | 0x200000):
        # MSAA selected/checked/selectable provides real selection capability.
        selection = bool(legacy & (0x2 | 0x10))
        mode = "legacy_selection_flags"
    if mode is not None:
        verified = selection is True and confirm_enabled and source_enabled
        return {**base,"ok":verified,"mode":mode,"selection_read_directly":True,
                "selected":selection,"focus":focus,"interfaces":interfaces,
                "error":None if verified else "SOURCE_CURVE_SELECTION_NOT_CONFIRMED"}
    focused_rows = [row.Name for row in rows if _source_focus_state(row)["focused"]]
    focused_source_only = focus["focused"] and focused_rows == [source.Name]
    ready = bool(focused_source_only and source_enabled and confirm_enabled)
    return {**base,"ok":ready,"mode":"focused_source_and_enabled_confirmation",
            "selection_read_directly":False,"selected":None,"focus":focus,
            "focused_rows":focused_rows,"interfaces":interfaces,
            "error":None if ready else "DEFAULT_SOURCE_SUBMISSION_PRECONDITION_NOT_CONFIRMED"}


def copy_recorded_blank(session):
    """Create a flat blank and copy the preselected formcheck contour."""
    opened = create_flat_blank(session)
    if not opened.get("ok"):
        return _fail("FLAT_BLANK_PAGE_NOT_REACHED", opened=opened)
    page = _page(session, "AutoForm::BlankPageView")
    curve = _one(page, name="矩形...", class_name="AFCurveInputControl")
    if curve is None:
        return _fail("RECORDED_BLANK_CURVE_INPUT_MISSING", names=_names(page) if page else [])
    # Cache before opening AFMenu. No main-window COM properties are read while
    # that popup remains open, even when selecting it without focus changes.
    main_hwnd = int(session.main_window.NativeWindowHandle)
    expected_geometry = win32.wait_stable_geometry(main_hwnd)
    if expected_geometry is None:
        return _fail("MAIN_WINDOW_GEOMETRY_UNSTABLE_BEFORE_POPUP")
    arrow = common.click(session, curve, relative=_relative(101))
    if not arrow.get("ok"):
        return arrow
    popup = _popup_copy_item(session,timeout=5)
    if not popup.get("ok"):
        return _fail("COPY_FROM_POPUP_ITEM_MISSING", arrow=arrow, popup=popup)
    choice = _click_copy_popup(session,popup,main_hwnd=main_hwnd,expected_geometry=expected_geometry)
    if not choice.get("ok"):
        return choice
    closed = common.wait_for(lambda: not _native_popup_visible(popup["hwnd"],session.pid),timeout=5)
    if not closed:
        return _fail("COPY_POPUP_DID_NOT_CLOSE", popup=popup, choice=choice)
    confirmed = confirm_recorded_blank_source(session)
    return {**confirmed,"popup_probe":popup,"popup_choice":choice,"evidence_events":[101,103,105]}


def confirm_recorded_blank_source(session):
    """Confirm an already open recorded PickLines editor; never create a blank.

    This local continuation performs no source click and opens no new menu.
    Full-chain acceptance still requires the later save/reopen/native checks.
    """
    editor = common.wait_for(lambda: _page(session, "AutoForm::PickLinesEditorView"), timeout=8)
    if editor is None:
        return _fail("PICK_LINES_EDITOR_MISSING")
    preselection = _source_preselection_state(editor)
    if not preselection.get("ok"):
        return _fail("SOURCE_CURVE_SELECTION_NOT_CONFIRMED", source_preselection=preselection,
                     names=_names(editor))
    source = preselection["source"]
    confirm = _one(editor, name="确定", type_name="ButtonControl")
    if confirm is None:
        return _fail("PICK_LINES_CONFIRM_MISSING", source=source)
    confirmed = common.click(session, confirm)
    if not confirmed.get("ok"):
        return confirmed
    def submitted():
        current = _page(session, "AutoForm::BlankPageView")
        edited = _one(current, name="编辑...", class_name="AFCurveInputControl")
        return {"ok": _page(session, "AutoForm::PickLinesEditorView") is None and edited is not None,
                "curve_input": "编辑..." if edited is not None else None}
    after = common.wait_for(submitted, timeout=8)
    if not after:
        return _fail("COPIED_BLANK_SUBMISSION_NOT_CONFIRMED", after=submitted())
    read = read_blank(session)
    return {"ok": True, "source": source, "after": after, "read": read,
            "submission_verified": True, "closed_contour_verified": False, "persistent_verified": False,
            "requires_geometry_preflight": True,"source_preselection":preselection,
            "scope":"existing_picklines_confirmation","additional_source_click":False,
            "evidence_events":[105]}


def _select_operation_context(session):
    opened = pages.open_page(session, "process")
    if not opened.get("ok"):
        return opened
    bar = _one(session.main_window, class_name="AFAnimatedOPTabBar")
    if bar is None:
        return _fail("PROCESS_OPERATION_CONTEXT_BAR_MISSING")
    action = common.click(session, bar, relative=_relative(115))
    if not action.get("ok"):
        return action
    tabs = common.wait_for(lambda: {"ok": {"模具", "测量"}.issubset(set(process.process_tab_names(session))),
                                    "tabs": process.process_tab_names(session)}, timeout=8)
    return {"ok": True, "action": action, **tabs} if tabs else _fail("C20_OPERATION_CONTEXT_NOT_CONFIRMED", action=action)


def _tool_context_state(root, name):
    """Identify the current tool without equating focus with selection.

    This Qt tree exposes only a focused current index on the observed build.
    When an actual selection capability exists its false remains decisive.
    Otherwise the unique focused row must agree with the right-hand parameter
    panel's unique title. Automatic model/contact are checked separately below.
    """
    matches = _controls(root, name=name, type_name="TreeItemControl")
    base = {"ok":False,"tool":name,"tool_candidates":len(matches),
            "selection_read_directly":False,"selected":None}
    if len(matches) != 1:
        return {**base,"error":"TOOL_CONTEXT_ROW_NOT_UNIQUE"}
    tool = matches[0]
    try:
        enabled = bool(tool.IsEnabled)
    except Exception:
        enabled = False
    focus = _source_focus_state(tool)
    base.update(tool_enabled=enabled,focus=focus)
    interfaces = {}
    selection = mode = None
    for method,attribute,label in (("GetSelectionItemPattern","IsSelected","selection_item_pattern"),
                                   ("GetTogglePattern","ToggleState","toggle_pattern")):
        try:
            raw = getattr(getattr(tool,method)(),attribute)
            interfaces[label] = {"supported":True,"value":raw}
            selection = bool(raw) if raw in (False,True,0,1) else None
            mode = label
            break
        except Exception as exc:
            interfaces[label] = {"supported":False,"error":type(exc).__name__}
    legacy = focus["legacy_state"]
    if mode is None and legacy is not None and legacy & (0x2 | 0x10 | 0x200000):
        selection = bool(legacy & (0x2 | 0x10))
        mode = "legacy_selection_flags"
    if mode is not None:
        verified = selection is True and enabled
        return {**base,"ok":verified,"mode":mode,"selection_read_directly":True,
                "selected":selection,"interfaces":interfaces,
                "error":None if verified else "TOOL_SELECTION_NOT_CONFIRMED"}

    tool_rect = uia.control_rect(tool)
    # Scope focus to the actual tool tree. A different focused tree elsewhere
    # in ToolsPageView cannot establish this parameter pane's current context.
    trees = [tree for tree in _controls(root,class_name="AFDragTreeWidget")
             if any(uia.control_rect(row) == tool_rect for row in
                    _controls(tree,name=name,type_name="TreeItemControl"))]
    evidence = {**base,"mode":"context_by_focus_and_panel_title","interfaces":interfaces,
                "tree_candidates":len(trees)}
    if len(trees) != 1:
        return {**evidence,"error":"TOOL_CONTEXT_TREE_NOT_UNIQUE"}
    tree = trees[0]
    rows = _controls(tree,type_name="TreeItemControl")
    focused_rows = [row.Name for row in rows if _source_focus_state(row)["focused"]]
    headers = _controls(tree,name="模具",type_name="HeaderControl")
    titles = _controls(root,name=name,class_name="QLabel",type_name="TextControl")
    evidence.update(focused_rows=focused_rows,header_candidates=len(headers),
                    panel_title_candidates=len(titles),tree_rect=list(uia.control_rect(tree)))
    panel_aligned = False
    if len(headers) == 1 and len(titles) == 1:
        header_rect = uia.control_rect(headers[0])
        title_rect = uia.control_rect(titles[0])
        tree_rect = uia.control_rect(tree)
        header_height = header_rect[3]-header_rect[1]
        title_height = title_rect[3]-title_rect[1]
        # Relative to the current tree/header, not recorded screen coordinates.
        # The observed pane title and header top differ by one pixel.
        panel_aligned = (title_rect[0] >= tree_rect[2]
                         and abs(title_rect[1]-header_rect[1]) <= max(3,header_height*.35)
                         and abs((title_rect[1]+title_rect[3])-(header_rect[1]+header_rect[3]))
                             <= max(header_height,title_height))
        evidence.update(header_rect=list(header_rect),panel_title_rect=list(title_rect))
    verified = bool(enabled and focus["focused"] and focused_rows == [name] and panel_aligned)
    return {**evidence,"ok":verified,"panel_title_aligned":panel_aligned,
            "error":None if verified else "TOOL_FOCUS_AND_PANEL_CONTEXT_NOT_CONFIRMED"}


def _automatic_tool_state(root, name):
    context = _tool_context_state(root,name)
    models = _controls(root,name="自动",class_name="AFCurveInputControl")
    auto_contacts = []
    for ctrl in _controls(root,name="自动",type_name="CheckBoxControl"):
        try:
            if "手动" in _names(ctrl.GetParentControl()):
                auto_contacts.append(ctrl)
        except Exception:
            continue
    contact = _selected(auto_contacts[0]) if len(auto_contacts) == 1 else None
    return {"ok":context.get("ok") is True and len(models) == 1 and contact is True,
            "tool":name,"tool_selected":context.get("selected"),"tool_context":context,
            "selection_read_directly":context.get("selection_read_directly",False),
            "context_mode":context.get("mode"),"model_mode":"自动" if len(models) == 1 else None,
            "automatic_contact":contact,"model_candidates":len(models),
            "contact_candidates":len(auto_contacts)}


def verify_automatic_tools(session, *, show_tools=False):
    """Read C-20's existing automatic convex/concave tools; never define faces."""
    context = _select_operation_context(session)
    if not context.get("ok"):
        return context
    tab = _tab(session, "模具", within=_page(session, "AutoForm::OperationPageView"))
    if not tab.get("ok"):
        return tab
    visibility = None
    if show_tools:
        from .visibility import show_recorded_tools
        visibility = show_recorded_tools(session, _page(session, "AutoForm::ToolsPageView"))
        if not visibility.get("ok"):
            return _fail("RECORDED_TOOL_VISIBILITY_NOT_CONFIRMED", visibility=visibility)
    states = []
    for name in ("凸模", "凹模"):
        tools = _page(session, "AutoForm::ToolsPageView")
        tool = _one(tools, name=name, type_name="TreeItemControl")
        if tool is None:
            return _fail("RECORDED_TOOL_MISSING", tool=name, names=_names(tools) if tools else [])
        action = common.click(session, tool)
        if not action.get("ok"):
            return _fail("TOOL_SELECTION_INPUT_FAILED", tool=name, action=action)
        def state():
            root = _page(session, "AutoForm::ToolsPageView")
            return _automatic_tool_state(root,name)
        after = common.wait_for(state, timeout=5)
        if not after:
            return _fail("AUTOMATIC_TOOL_STATE_NOT_CONFIRMED", tool=name, after=state(), previous=states)
        states.append(after)
    return {"ok": True, "operation": "C-20", "tools": states, "mode_only_verified": True,
            "recorded_visibility": visibility, "visibility_requested": show_tools,
            "geometry_validated": False, "persistent_verified": False, "requires_geometry_preflight": True}


def read_recorded_automatic_tools(session):
    """Read existing automatic modes using only tab/tool selection, no setters."""
    result = verify_automatic_tools(session)
    return {**result, "read_only": True, "automatic_tool_modes_verified": bool(result.get("ok")),
            "geometry_validated": False, "geometry_pending": ["native_tool_geometry_validation"],
            "persistent_verified": False, "requires_geometry_preflight": True}


def read_blank(session):
    """Read the current blank page without setting fields or creating geometry."""
    page = _page(session, "AutoForm::BlankPageView")
    if page is None:
        return _fail("BLANK_PAGE_NOT_FOUND")
    fields = []
    for control in _controls(page):
        try:
            pattern = control.GetValuePattern()
            value = pattern.Value if pattern else None
            if value is not None:
                fields.append({"name": control.Name or "", "value": value,
                               "kind": control.ControlTypeName})
        except Exception:
            continue
    return {"ok": True, "texts": _names(page), "values": fields,
            "geometry_validated": False}


def create_flat_blank(session, *, timeout=30.0):
    """Create only the recorded flat blank; no rectangle or contour is drawn."""
    opened = pages.open_page(session, "blank")
    if not opened.get("ok"):
        return _fail("BLANK_PAGE_NOT_REACHED", opened=opened)
    page = _page(session, "AutoForm::BlankPageView")
    if page is None:
        return _fail("BLANK_PAGE_NOT_FOUND")
    if _one(page, class_name="AutoForm::BlankLinesPageView") is not None:
        return {"ok": True, "outline_page": True, "already_present": True}
    button = _one(page, name="创建平板料片", type_name="ButtonControl")
    if button is None:
        return _fail("FLAT_BLANK_ENTRY_NOT_UNIQUE", names=_names(page))
    action = common.click(session, button)
    if not action.get("ok"):
        return action
    reached = common.wait_for(lambda: _page(session, "AutoForm::BlankLinesPageView"), timeout=timeout)
    return {"ok": reached is not None, "outline_page": reached is not None,
            "action": action, "error": None if reached is not None else "FLAT_BLANK_OUTLINE_NOT_REACHED"}


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--popup-probe":
        _menu_probe_worker(sys.argv[2], sys.argv[3])
    else:
        raise SystemExit("Only the read-only popup probe is supported here.")
