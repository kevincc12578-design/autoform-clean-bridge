"""Index official R13 help mappings, without claiming UI availability."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re


DEFAULT_HELP = Path("D:/Program Files/AutoForm/AFplus/R13F/help/helpLinks.cfg")
VERIFICATION_SCOPE = "Help mappings only; UI, license, parameters and click execution unverified."
_MAPPING = re.compile(r"^(?P<presenter>.+?)\s+(?P<path>/user-interface/[^\s#]+)")
_FAMILIES = {
    "part-stage": "零件、方向与成型检查",
    "material-generator-material-editor-viewer": "材料与材料编辑",
    "plan-stage": "工艺规划",
    "die-face-stage": "模面与工具设计",
    "blank-stage": "料片与排样",
    "process-stage": "工艺、参考与测量",
    "simulation-stage": "模拟设置与运行",
    "evaluation-stage": "结果查看与评价",
    "geometry-generation": "几何生成与编辑",
    "application-toolbar": "视图与显示",
    "application-menu": "文件与导出",
    "reportmanager": "报告",
    "settings": "界面设置",
}


def _family(path: str) -> tuple[str, str]:
    key = path.split("/")[2]
    return key, _FAMILIES.get(key, key)


@dataclass(frozen=True)
class Catalog:
    source_path: str
    source_sha256: str
    entries: tuple[dict, ...]
    source_line_count: int

    def search(self, query: str = "") -> list[dict]:
        """Search IDs, paths, families and presenter names, not the live UI."""
        words = query.casefold().split()
        return [deepcopy(entry) for entry in self.entries if all(
            word in " ".join((entry["id"], entry["help_path"], entry["family"],
                              entry["family_label"], *entry["presenters"])).casefold()
            for word in words
        )]

    def as_dict(self) -> dict:
        return {
            "schema_version": 1,
            "source": {"path": self.source_path, "sha256": self.source_sha256,
                       "line_count": self.source_line_count,
                       "kind": "installed_official_help_mapping"},
            "entry_count": len(self.entries),
            "family_counts": dict(sorted(Counter(e["family"] for e in self.entries).items())),
            "verification_scope": VERIFICATION_SCOPE,
            "entries": self.search(),
        }


def load_catalog(path: str | Path = DEFAULT_HELP) -> Catalog:
    """Read a helpLinks.cfg. Missing or empty mapping files fail explicitly."""
    source = Path(path).resolve(strict=True)
    raw = source.read_bytes()
    lines = raw.decode("utf-8-sig").splitlines()
    grouped: dict[str, dict] = {}
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "//", "@", "$")):
            continue
        match = _MAPPING.match(stripped)
        if not match:
            continue
        help_path = match["path"].rstrip("/")
        if not help_path.startswith("/user-interface/") or len(help_path.split("/")) < 3:
            continue
        presenter = match["presenter"].strip()
        family, family_label = _family(help_path)
        entry = grouped.setdefault(help_path, {
            "id": "help." + help_path.removeprefix("/user-interface/").replace("/", "."),
            "help_path": help_path, "family": family, "family_label": family_label,
            "presenters": [], "source_lines": [], "status": "indexed",
            "local_availability": "unknown", "callable_verified": False,
        })
        if presenter not in entry["presenters"]:
            entry["presenters"].append(presenter)
        entry["source_lines"].append(number)
    if not grouped:
        raise ValueError(f"No user-interface mappings found in {source}")
    return Catalog(str(source), sha256(raw).hexdigest(),
                   tuple(grouped[key] for key in sorted(grouped)), len(lines))


def search(query: str, path: str | Path = DEFAULT_HELP) -> list[dict]:
    return load_catalog(path).search(query)


def as_dict(path: str | Path = DEFAULT_HELP) -> dict:
    return load_catalog(path).as_dict()


def query_catalog(query: str = "", source: str | Path | None = None) -> dict:
    """Return source evidence and filtered entries without attaching to AutoForm.

    entry_count and family_counts describe the complete source index;
    match_count describes only entries matching the query.
    """
    from .models import BridgeError

    if not isinstance(query, str):
        raise BridgeError("invalid_request", "Catalog query must be a string.")
    if source is not None and (not isinstance(source, (str, Path)) or not str(source)):
        raise BridgeError("invalid_request", "Catalog source must be a nonempty file path.")
    catalog = load_catalog(DEFAULT_HELP if source is None else source)
    result = catalog.as_dict()
    result["entries"] = catalog.search(query)
    result["query"] = query
    result["match_count"] = len(result["entries"])
    return result
