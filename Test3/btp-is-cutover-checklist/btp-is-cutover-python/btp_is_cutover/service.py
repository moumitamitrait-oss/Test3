"""Orchestration used by the GUI and the command line: analyse the selected iFlows, then write the workbooks."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .api import ApiError, NetworkError
from .cutover import cutover_data_for
from .detect import detect_dependencies, with_defaults
from .models import CutoverData, Dep, DepResult
from .tenant import SelectedFlow, Tenant
from .workbook import build_workbook, save_workbook

Progress = Optional[Callable[[str], None]]


@dataclass
class Analysis:
    data: Optional[CutoverData] = None
    result: DepResult = field(default_factory=DepResult)
    error: str = ""


def analyze(tenant: Tenant, selected: List[SelectedFlow], progress: Progress = None, workers: int = 2) -> Dict[str, Analysis]:
    """Read every selected iFlow and detect its common dependent artifacts (the default value mapping is added)."""
    out: Dict[str, Analysis] = {}
    done = [0]

    def work(flow: SelectedFlow):
        try:
            data = cutover_data_for(tenant, flow.as_flow())
            res = detect_dependencies(tenant, data, lambda m: progress and progress(f"{flow.name}: {m}"))
            res.deps = with_defaults(res.deps)
            out[flow.key] = Analysis(data=data, result=res)
        except NetworkError:
            raise
        except (ApiError, ValueError) as exc:
            out[flow.key] = Analysis(error=str(exc))
        done[0] += 1
        if progress:
            progress(f"{done[0]} of {len(selected)} iFlows analysed")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, selected))
    return out


def validate_details(selected: List[SelectedFlow], poc: str) -> List[str]:
    """Interface ID (every iFlow) and Point of contact are mandatory."""
    problems = []
    if any(not (f.interface_id or "").strip() for f in selected):
        problems.append("an Interface ID for every iFlow")
    if not (poc or "").strip():
        problems.append("the Point of contact")
    return problems


def generate(analyses: Dict[str, Analysis], selected: List[SelectedFlow], options: Dict[str, Any], out_dir: str) -> Tuple[List[str], List[str]]:
    """Write one workbook per iFlow; returns (paths, notes)."""
    paths: List[str] = []
    notes: List[str] = []
    for flow in selected:
        a = analyses.get(flow.key)
        if not a:
            continue
        if not a.data:
            notes.append(f"{flow.name}: skipped ({a.error})")
            continue
        notes.extend(f"{flow.name}: {w}" for w in a.data.warnings)
        common = [d.as_common() for d in a.result.deps if d.include and (d.artifact or "").strip()]
        wb, filename = build_workbook(a.data, {**options, "interface_id": flow.interface_id, "common": common})
        paths.append(save_workbook(wb, out_dir, filename))
        notes.extend(f"{flow.name}: not included: {d.system} {d.artifact}".rstrip() for d in a.result.deps
                     if not d.include and not d.artifact and d.system)
    return paths, notes
