"""Plain data classes shared by the library, the GUI and the command line."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from .design import Design


@dataclass
class Dep:
    """One row of the "common dependent artifacts" table."""
    pkg: str = ""
    artifact: str = ""
    system: str = ""          # artifact type, e.g. "Script Collection"
    source: str = ""
    include: bool = True
    pkg_id: str = ""
    manual: bool = False      # rows the user still has to complete (package / artifact dropdowns)

    def as_common(self) -> Dict[str, str]:
        return {"pkg": (self.pkg or "").strip() or "N/A", "artifact": self.artifact.strip(), "system": (self.system or "").strip()}


@dataclass
class DepResult:
    deps: List[Dep] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)   # references that match no artifact in the tenant
    notes: List[str] = field(default_factory=list)     # what the scan of the message mappings found


@dataclass
class CutoverData:
    iflow: Dict[str, str]                                   # Id, Name, Package, PackageId, Version
    param_groups: List[Dict[str, Any]]                      # {category, component, items:[(key, value)]}
    credentials: List[Dict[str, Any]]                       # {alias, rank, owner}
    warnings: List[str]
    design: Design
    cfg_map: Dict[str, Any]
    has_sender: bool = False
    timer: bool = False
    sender_system: str = ""
    sender_type: str = ""
    receiver_system: str = ""
    receiver_type: str = ""
    receiver_credential: str = ""
