"""Replay event 119's display-all eye only after a local visual state read.

The eye is a display control.  It is not the tool's enable flag and does not
prove that its automatic geometry exists.  All input uses common's native
foreground, ownership, occlusion and stable-window guards.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from pathlib import Path
from statistics import mean, median, pstdev
from types import SimpleNamespace
import time
import uuid

from PIL import Image, ImageGrab

from .ui import common, uia


ASSETS = Path(__file__).resolve().parent / "calibration"
PROVENANCE = ASSETS / "recorded-tool-eyes.provenance.json"
ROW_NAMES = {"slide": "滑块(上模)", "convex": "凸模", "bed": "台面(下模)", "concave": "凹模"}
ROLES = ("header", *ROW_NAMES)


def _fail(error, **details):
    return {"ok": False, "error": error, **details}


def _name(control):
    return "".join(str(control.Name or "").split()).replace("（", "(").replace("）", ")")


def _visible(root, *, class_name=None, type_name=None):
    result = []
    for control, _ in uia.walk(root):
        try:
            rect = uia.control_rect(control)
            if control.IsOffscreen or not rect or rect[2] <= rect[0] or rect[3] <= rect[1]:
                continue
            if class_name is not None and control.ClassName != class_name:
                continue
            if type_name is not None and control.ControlTypeName != type_name:
                continue
            result.append(control)
        except Exception:
            continue
    return result


def _inside(rect, parent):
    return bool(rect and parent and parent[0] <= rect[0] < rect[2] <= parent[2]
                and parent[1] <= rect[1] < rect[3] <= parent[3])


def _layout(tools_page):
    """Use the current header/rows, never a whole-window or tree-height ratio."""
    try:
        if tools_page.ClassName != "AutoForm::ToolsPageView" or tools_page.IsOffscreen:
            return _fail("TOOLS_PAGE_NOT_VISIBLE")
        page_rect = uia.control_rect(tools_page)
        trees = _visible(tools_page, class_name="AFDragTreeWidget", type_name="TreeControl")
        if len(trees) != 1:
            return _fail("TOOLS_TREE_NOT_UNIQUE", candidates=len(trees))
        tree = trees[0]
        tree_rect = uia.control_rect(tree)
        if not _inside(tree_rect, page_rect):
            return _fail("TOOLS_TREE_OUTSIDE_PAGE")
        headers = [c for c in _visible(tree, type_name="HeaderControl") if _name(c) == "模具"]
        if len(headers) != 1:
            return _fail("TOOLS_HEADER_NOT_UNIQUE", candidates=len(headers))
        items = _visible(tree, type_name="TreeItemControl")
        rows = {role: [c for c in items if _name(c) == name] for role, name in ROW_NAMES.items()}
        if len(items) != 4 or any(len(value) != 1 for value in rows.values()):
            return _fail("RECORDED_TOOL_ROWS_NOT_UNIQUE", names=[_name(c) for c in items],
                         candidates={role: len(value) for role, value in rows.items()})
        controls = {"header": headers[0], **{role: value[0] for role, value in rows.items()}}
        rects = {role: uia.control_rect(control) for role, control in controls.items()}
        if any(not _inside(rect, tree_rect) for rect in rects.values()):
            return _fail("TOOL_ROW_OUTSIDE_TREE", rects=rects)
        heights = [rects[role][3]-rects[role][1] for role in ROW_NAMES]
        row_height = float(median(heights))
        if not 16 <= row_height <= 40:
            return _fail("TOOL_ROW_SCALE_UNCALIBRATED", row_heights=heights)
        scale = row_height / 20.0
        tolerance = max(1.0, 1.5*scale)
        header = rects["header"]
        valid = (max(heights)-min(heights) <= tolerance
                 and abs((header[3]-header[1])-1.15*row_height) <= tolerance
                 and abs(header[0]-tree_rect[0]) <= tolerance
                 and abs(header[1]-tree_rect[1]-scale) <= tolerance
                 and abs(tree_rect[2]-header[2]-scale) <= tolerance
                 and header[2]-header[0] >= 3.5*row_height)
        previous_bottom = header[3]
        for role in ROW_NAMES:
            rect = rects[role]
            expected_left = tree_rect[0] + (0.6*row_height if role in ("convex", "concave") else 0)
            valid = valid and (abs(rect[1]-previous_bottom) <= tolerance
                               and abs(rect[2]-header[2]) <= tolerance
                               and abs(rect[0]-expected_left) <= tolerance)
            previous_bottom = rect[3]
        if not valid:
            return _fail("TOOLS_HEADER_ROW_LAYOUT_UNCALIBRATED", tree_rect=tree_rect,
                         rects=rects, row_heights=heights)
        boxes = {}
        for role, rect in rects.items():
            # Actual event 119: x111:126, header y385:400; body y407:422 +20n.
            # Qt header is 23 px and body rows 20 px at the recorded scale.
            top = rect[1] + (0.15 if role == "header" else 0.10)*row_height
            box = (round(rect[2]-0.90*row_height), round(top),
                   round(rect[2]-0.15*row_height), round(top+0.75*row_height))
            if not _inside(box, rect):
                return _fail("TOOL_EYE_OUTSIDE_ROW", role=role, box=box, row_rect=rect)
            boxes[role] = box
        evidence = {"tree_rect": list(tree_rect), "rects": {k: list(v) for k, v in rects.items()},
                    "eye_boxes": {k: list(v) for k, v in boxes.items()}, "row_height": row_height,
                    "scale": scale, "method": "current_header_and_four_uia_rows"}
        return {"ok": True, "tree": tree, "header": headers[0], "boxes": boxes,
                "fingerprint": (tuple(tree_rect), *(tuple(rects[role]) for role in ROLES)),
                "evidence": evidence}
    except Exception as exc:
        return _fail("TOOLS_LAYOUT_READ_FAILED", detail=repr(exc))


@lru_cache(maxsize=1)
def _references():
    metadata = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    templates = {}
    for state, source in metadata["states"].items():
        path = ASSETS / source["asset"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != source["asset_sha256"]:
            raise ValueError("TOOL_EYE_REFERENCE_HASH_MISMATCH")
        with Image.open(path) as image:
            if image.size != (15, 75):
                raise ValueError("TOOL_EYE_REFERENCE_SIZE_MISMATCH")
            templates[state] = {role: image.convert("RGB").crop((0, i*15, 15, (i+1)*15))
                                for i, role in enumerate(ROLES)}
    return templates, metadata


def _features(image):
    image = image.convert("RGB").resize((15, 15), Image.Resampling.LANCZOS)
    color = image.load()
    gray = image.convert("L").load()
    edge = [gray[x, y] for y in range(15) for x in range(15) if x in (0, 14) or y in (0, 14)]
    rim = [gray[x, y] for y in range(1, 14) for x in range(1, 14) if x in (1, 13) or y in (1, 13)]
    core = [gray[x, y] for y in range(2, 13) for x in range(2, 13)]
    rim_level = float(median(rim))
    saturation = sorted(max(color[x, y])-min(color[x, y]) for y in range(15) for x in range(15))
    return {"core": [value-rim_level for value in core], "core_std": pstdev(core),
            "rim_level": rim_level, "edge_level": float(median(edge)),
            "frame_contrast": rim_level-median(edge), "saturation_p95": saturation[int(.95*(len(saturation)-1))],
            "dark_pixels": sum(value < rim_level-90 for value in core)}


def classify_eye(image, role):
    """Pure calibrated glyph classification; background alone cannot be hidden."""
    try:
        templates, _ = _references()
        if role not in ROLES:
            return _fail("UNKNOWN_TOOL_EYE_ROLE", state="unknown")
        width, height = image.size
        if not (12 <= width <= 30 and 12 <= height <= 30 and abs(width-height) <= 1):
            return _fail("TOOL_EYE_CROP_SIZE_UNCALIBRATED", state="unknown", size=list(image.size))
        observed = _features(image)
        distances = {}
        reference_features = {}
        for state in ("hidden", "visible"):
            # Render the reference at the same measured DPI before normalizing
            # both crops. Otherwise downsampling only the observation changes
            # its one-pixel rim and introduces a false brightness difference.
            rendered = templates[state][role].resize(image.size, Image.Resampling.LANCZOS)
            reference = _features(rendered)
            reference_features[state] = reference
            delta = [a-b for a, b in zip(observed["core"], reference["core"])]
            distances[state] = {"mae": mean(abs(value) for value in delta),
                                "rmse": mean(value*value for value in delta)**.5}
        best = min(distances, key=lambda state: distances[state]["mae"])
        margin = distances["visible" if best == "hidden" else "hidden"]["mae"]-distances[best]["mae"]
        frame_ok = (observed["frame_contrast"] >= 80 and 140 <= observed["rim_level"] <= 255
                    and observed["edge_level"] <= 140 and observed["saturation_p95"] <= 20)
        shape_ok = (distances[best]["mae"] <= 16 and distances[best]["rmse"] <= 27 and margin >= 18)
        if best == "hidden":
            # At 80% DPI the rim bleeds into the core even for a real empty
            # square. Bound that variation against its same-scale reference.
            std_limit = max(16, reference_features["hidden"]["core_std"]+6)
            shape_ok = shape_ok and observed["dark_pixels"] <= 3 and observed["core_std"] <= std_limit
        else:
            shape_ok = shape_ok and 20 <= observed["dark_pixels"] <= 65
        known = bool(frame_ok and shape_ok)
        return {"ok": known, "state": best if known else "unknown", "role": role,
                "distances": {state: {k: round(v, 3) for k, v in values.items()} for state, values in distances.items()},
                "margin": round(margin, 3), "frame_verified": bool(frame_ok),
                "reference_render_size": list(image.size),
                "features": {key: round(value, 3) for key, value in observed.items() if key != "core"},
                "error": None if known else "RECORDED_TOOL_EYE_NOT_CONFIRMED"}
    except Exception as exc:
        return _fail("TOOL_EYE_CLASSIFICATION_FAILED", state="unknown", detail=repr(exc))


def _capture_tree(session, layout):
    prepared = common._prepare(session, focus=False)
    if not prepared.get("ok"):
        return prepared
    points = [((box[0]+box[2])//2, (box[1]+box[3])//2) for box in layout["boxes"].values()]
    for point in points:
        guarded = common._check_point(session, point, prepared["hwnd"])
        if not guarded.get("ok"):
            return guarded
    try:
        rect = layout["evidence"]["tree_rect"]
        started = time.perf_counter_ns()
        image = ImageGrab.grab(bbox=tuple(rect), all_screens=True).convert("RGB")
        finished = time.perf_counter_ns()
        if image.size != (rect[2]-rect[0], rect[3]-rect[1]):
            return _fail("TOOL_TREE_CAPTURE_SIZE_MISMATCH", size=list(image.size))
        for point in points:
            guarded = common._check_point(session, point, prepared["hwnd"])
            if not guarded.get("ok"):
                return guarded
        folder = Path(session.directory) / "tool-visibility-readbacks"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"tool-eyes-{uuid.uuid4().hex}.png"
        image.save(path)
        return {"ok": True, "image": image, "path": str(path),
                "rgb_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
                "capture_started_perf_ns": started, "capture_completed_perf_ns": finished}
    except Exception as exc:
        return _fail("TOOL_TREE_CAPTURE_FAILED", detail=repr(exc))


def _observe(session, tools_page):
    layout = _layout(tools_page)
    if not layout.get("ok"):
        return layout, None
    capture = _capture_tree(session, layout)
    if not capture.get("ok"):
        return capture, layout
    image = capture.pop("image")
    left, top, _, _ = layout["evidence"]["tree_rect"]
    icons = {role: classify_eye(image.crop((box[0]-left, box[1]-top, box[2]-left, box[3]-top)), role)
             for role, box in layout["boxes"].items()}
    values = {value["state"] for value in icons.values()}
    state = next(iter(values)) if len(values) == 1 else ("unknown" if "unknown" in values else "mixed")
    return {"ok": state in ("visible", "hidden", "mixed"), "visibility": state,
            "icons": icons, "capture": capture, "layout": layout["evidence"],
            "error": None if state != "unknown" else "TOOLS_VISIBILITY_UNKNOWN"}, layout


class _HeaderEye:
    """Fresh rect adapter so common.click rechecks the calibrated layout."""
    def __init__(self, tools_page, fingerprint):
        self.tools_page = tools_page
        self.fingerprint = fingerprint

    @property
    def IsEnabled(self):
        return bool(self.tools_page.IsEnabled)

    @property
    def IsOffscreen(self):
        return bool(self.tools_page.IsOffscreen)

    @property
    def BoundingRectangle(self):
        current = _layout(self.tools_page)
        if not current.get("ok") or current["fingerprint"] != self.fingerprint:
            raise ValueError("TOOLS_LAYOUT_CHANGED_BEFORE_INPUT")
        left, top, right, bottom = current["boxes"]["header"]
        return SimpleNamespace(left=left, top=top, right=right, bottom=bottom)


def show_recorded_tools(session, tools_page, *, timeout=5.0, interval=0.15):
    """Read all five eyes; send at most one display-all toggle, then verify.

    Already visible is an idempotent success. Mixed/unknown is refused before
    input. A failed postcondition never triggers a second toggle.
    """
    result = {"ok": False, "calibration_event": 119, "visibility_only": True,
              "enabled_state_verified": False, "geometry_validated": False,
              "persistent_verified": False, "visibility_verified": False,
              "changed": False, "toggle_sent": False}
    try:
        _, metadata = _references()
        result["visual_reference"] = {"provenance": str(PROVENANCE), "event_id": metadata["event_id"],
                                      "assets": metadata["states"], "mouse_highlight_used": False}
        prepared = common._prepare(session)
        if not prepared.get("ok"):
            return {**result, "error": "TOOLS_VISIBILITY_PREPARE_FAILED", "guard": prepared}
        before, layout = _observe(session, tools_page)
        result["before"] = before
        state = before.get("visibility")
        if state == "visible":
            return {**result, "ok": True, "visibility_verified": True, "after": before, "error": None}
        if state != "hidden":
            return {**result, "error": "TOOLS_VISIBILITY_NOT_SAFELY_HIDDEN"}
        pre_input, fresh = _observe(session, tools_page)
        result["pre_input"] = pre_input
        if pre_input.get("visibility") == "visible":
            return {**result, "ok": True, "visibility_verified": True, "after": pre_input, "error": None}
        if pre_input.get("visibility") != "hidden":
            return {**result, "error": "TOOLS_VISIBILITY_CHANGED_BEFORE_INPUT"}
        if fresh["fingerprint"] != layout["fingerprint"]:
            return {**result, "error": "TOOLS_LAYOUT_CHANGED_BEFORE_INPUT"}
        action = common.click(session, _HeaderEye(tools_page, fresh["fingerprint"]), focus=False)
        result["input"] = action
        if not action.get("ok"):
            return {**result, "error": "TOOLS_DISPLAY_ALL_INPUT_FAILED"}
        result["toggle_sent"] = True
        result["changed"] = None  # Input alone does not prove that the icons changed.
        deadline = time.monotonic()+max(0.0, timeout)
        stable = 0
        polls = []
        after = pre_input
        while time.monotonic() < deadline:
            after, current = _observe(session, tools_page)
            polls.append({"visibility": after.get("visibility"), "error": after.get("error"),
                          "capture": after.get("capture")})
            result["after"] = after
            result["polls"] = polls
            if current is None or current.get("fingerprint") != fresh["fingerprint"]:
                return {**result, "error": "TOOLS_LAYOUT_CHANGED_AFTER_INPUT"}
            if "visibility" not in after:
                return {**result, "error": "TOOLS_VISIBILITY_READBACK_FAILED"}
            stable = stable+1 if after.get("visibility") == "visible" else 0
            if stable >= 2:
                return {**result, "ok": True, "changed": True, "visibility_verified": True,
                        "visible_samples": stable, "error": None}
            time.sleep(max(0.01, interval))
        return {**result, "error": "TOOLS_DISPLAY_ALL_NOT_CONFIRMED", "visible_samples": stable}
    except Exception as exc:
        return {**result, "error": "TOOLS_VISIBILITY_ACTION_FAILED", "detail": repr(exc)}
