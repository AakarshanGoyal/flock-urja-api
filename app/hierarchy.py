"""Reconstruct the network hierarchy (Zone > Circle > Division > Subdivision >
Substation > Feeder > DT > Meter) from per-meter paths, repairing what we can.

What the data looks like (see PROTOCOL.md, "Hierarchy quirks"):

1. Every meter carries its *own* copy of the full path. Meters on the same DT
   almost always agree, but ~5% have one level with a blank code or a blank
   name.
2. Codes are only meaningful within their parent: e.g. ``SD-01`` appears under
   ``D-01``, ``D-05`` and ``D-09``; ``F-001`` under ``SS-01`` and ``SS-11``.
   Grouping by bare code would merge unrelated branches, so tree nodes are keyed
   by their full path (``Z-01/C-01/D-07``) and keep the raw code as a field.
3. DT-007 is called "Sanganer DT 7" by the transformer register but some meters
   (J100400-J100402, a later batch) still call it "Old Malviya Nagar Xfmr".

Repair strategy, per DT (the most reliable anchor: every meter has a dtCode
that matches the transformer register):

* For each level, take the most common non-blank code among that DT's meters.
  The feeder code is cross-checked against the register's ``feederCode``,
  which wins on disagreement.
* Names: most common non-blank name seen for that code on that DT. For DTs the
  register name wins; other names seen are kept as ``aliases``.
* Every fill-in is recorded as an issue so consumers can see what was inferred.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable

from .portal.parsing import HIERARCHY_LEVELS

PATH_LEVELS = HIERARCHY_LEVELS[:-1]  # zone..feeder; the DT is the anchor
LEVEL_LABELS = {
    "zone": "Zone",
    "circle": "Circle",
    "division": "Division",
    "subdivision": "Subdivision",
    "substation": "Substation",
    "feeder": "Feeder",
    "dt": "Distribution Transformer",
}


@dataclass
class HierarchyIssue:
    kind: str
    level: str
    detail: str
    meter_ids: list[str] = field(default_factory=list)
    dt_code: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "level": self.level,
            "dt_code": self.dt_code,
            "detail": self.detail,
            "meter_ids": self.meter_ids,
        }


@dataclass
class ResolvedHierarchy:
    # dt_code -> {level: {"code", "name"}} for zone..dt (repaired)
    dt_paths: dict[str, dict[str, dict[str, str]]]
    dt_aliases: dict[str, list[str]]
    issues: list[HierarchyIssue]

    def path_for_dt(self, dt_code: str | None) -> dict[str, dict[str, str]] | None:
        return self.dt_paths.get(dt_code or "")


def _most_common(values: Iterable[str]) -> str:
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else ""


def resolve(meters: list[dict[str, Any]], transformers: list[dict[str, Any]]) -> ResolvedHierarchy:
    """Work out one consistent path per DT and record every repair made."""
    register = {t["code"]: t for t in transformers}
    by_dt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    issues: list[HierarchyIssue] = []

    for m in meters:
        dt = m.get("dt_code") or m["hierarchy"]["dt"]["code"]
        if not dt:
            issues.append(HierarchyIssue("meter_without_dt", "dt", "meter has no DT code", [m["meter_id"]]))
            continue
        by_dt[dt].append(m)

    dt_paths: dict[str, dict[str, dict[str, str]]] = {}
    dt_aliases: dict[str, list[str]] = {}

    for dt_code in sorted(set(by_dt) | set(register)):
        group = by_dt.get(dt_code, [])
        path: dict[str, dict[str, str]] = {}
        for level in PATH_LEVELS:
            codes = [m["hierarchy"][level]["code"] for m in group]
            code = _most_common(codes)
            if level == "feeder" and dt_code in register:
                reg_code = register[dt_code].get("feederCode") or ""
                if reg_code and code and reg_code != code:
                    issues.append(
                        HierarchyIssue(
                            "feeder_conflict",
                            level,
                            f"meters say {code}, transformer register says {reg_code}; using register",
                            [m["meter_id"] for m in group],
                            dt_code,
                        )
                    )
                code = reg_code or code
            names = [m["hierarchy"][level]["name"] for m in group if m["hierarchy"][level]["code"] in (code, "")]
            name = _most_common(names)

            disagreeing = [m["meter_id"] for m in group if m["hierarchy"][level]["code"] not in ("", code)]
            if disagreeing:
                issues.append(
                    HierarchyIssue(
                        "conflicting_code",
                        level,
                        f"these meters report a different {level} than the rest of {dt_code} ({code})",
                        disagreeing,
                        dt_code,
                    )
                )
            blank_code = [m["meter_id"] for m in group if not m["hierarchy"][level]["code"]]
            if blank_code:
                issues.append(
                    HierarchyIssue(
                        "blank_code_filled",
                        level,
                        f"{level} code was blank; filled with {code or '?'} from other meters on {dt_code}",
                        blank_code,
                        dt_code,
                    )
                )
            blank_name = [m["meter_id"] for m in group if m["hierarchy"][level]["code"] and not m["hierarchy"][level]["name"]]
            if blank_name:
                issues.append(
                    HierarchyIssue(
                        "blank_name_filled",
                        level,
                        f"{level} name was blank; filled with {name or '?'!r} from other meters on {dt_code}",
                        blank_name,
                        dt_code,
                    )
                )
            path[level] = {"code": code, "name": name}

        # DT itself: the transformer register is authoritative for the name.
        seen_names = Counter(m["hierarchy"]["dt"]["name"] for m in group if m["hierarchy"]["dt"]["name"])
        reg = register.get(dt_code)
        dt_name = (reg or {}).get("name") or (seen_names.most_common(1)[0][0] if seen_names else "")
        aliases = sorted(n for n in seen_names if n != dt_name)
        if aliases:
            issues.append(
                HierarchyIssue(
                    "dt_name_mismatch",
                    "dt",
                    f"register calls {dt_code} {dt_name!r}; some meters say {', '.join(map(repr, aliases))}",
                    [m["meter_id"] for m in group if m["hierarchy"]["dt"]["name"] in aliases],
                    dt_code,
                )
            )
        if reg is None:
            issues.append(
                HierarchyIssue("dt_not_in_register", "dt", f"{dt_code} is used by meters but missing from /portal/dts", [m["meter_id"] for m in group], dt_code)
            )
        if not group:
            issues.append(HierarchyIssue("dt_without_meters", "dt", f"{dt_code} has no meters", [], dt_code))
        path["dt"] = {"code": dt_code, "name": dt_name}
        dt_paths[dt_code] = path
        dt_aliases[dt_code] = aliases

    issues.extend(_code_reuse_issues(dt_paths))
    return ResolvedHierarchy(dt_paths=dt_paths, dt_aliases=dt_aliases, issues=issues)


def _code_reuse_issues(dt_paths: dict[str, dict[str, dict[str, str]]]) -> list[HierarchyIssue]:
    """Flag codes that appear under more than one parent (why we key by path)."""
    out = []
    levels = HIERARCHY_LEVELS
    for i in range(1, len(levels) - 1):
        child, parent = levels[i], levels[i - 1]
        parents: dict[str, set[str]] = defaultdict(set)
        for path in dt_paths.values():
            if path[child]["code"] and path[parent]["code"]:
                parents[path[child]["code"]].add(path[parent]["code"])
        for code, ps in sorted(parents.items()):
            if len(ps) > 1:
                out.append(
                    HierarchyIssue(
                        "code_reused_across_parents",
                        child,
                        f"{child} {code} appears under {len(ps)} different {parent}s: {', '.join(sorted(ps))}",
                    )
                )
    return out


def node_id(path: dict[str, dict[str, str]], upto: str) -> str:
    """Path-qualified id, e.g. ``Z-01/C-01/D-07`` for a division."""
    parts = []
    for level in HIERARCHY_LEVELS:
        parts.append(path[level]["code"] or "?")
        if level == upto:
            break
    return "/".join(parts)


def build_tree(
    resolved: ResolvedHierarchy,
    meters_by_dt: dict[str, list[dict[str, Any]]],
    *,
    max_depth: str = "dt",
    include_meters: bool = False,
) -> list[dict[str, Any]]:
    """Nested tree of nodes with meter counts per status, down to ``max_depth``."""
    stop = HIERARCHY_LEVELS.index(max_depth)
    roots: dict[str, dict[str, Any]] = {}
    index: dict[str, dict[str, Any]] = {}

    for dt_code, path in sorted(resolved.dt_paths.items()):
        dt_meters = meters_by_dt.get(dt_code, [])
        status_counts = Counter(m.get("install_status") or "Unknown" for m in dt_meters)
        siblings = roots
        for depth, level in enumerate(HIERARCHY_LEVELS[: stop + 1]):
            nid = node_id(path, level)
            node = index.get(nid)
            if node is None:
                node = {
                    "id": nid,
                    "level": level,
                    "code": path[level]["code"],
                    "name": path[level]["name"],
                    "meter_count": 0,
                    "status_counts": {},
                    "children": [],
                }
                if level == "dt":
                    node["aliases"] = resolved.dt_aliases.get(dt_code, [])
                index[nid] = node
                if isinstance(siblings, dict):
                    siblings[nid] = node
                else:
                    siblings.append(node)
            node["meter_count"] += len(dt_meters)
            for status, n in status_counts.items():
                node["status_counts"][status] = node["status_counts"].get(status, 0) + n
            if level == "dt" and include_meters:
                node["meters"] = sorted(m["meter_id"] for m in dt_meters)
            siblings = node["children"]

    def finish(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nodes.sort(key=lambda n: n["id"])
        for n in nodes:
            finish(n["children"])
            if not n["children"]:
                n.pop("children")
        return nodes

    return finish(list(roots.values()))
