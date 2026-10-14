"""Tenant context: packages, cached lists of iFlows / artifacts."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .api import ApiError, CpiClient, get_token, lit

# (OData entity, label shown to the user). Lists a tenant does not offer are simply empty.
ART_TYPES = [
    ("ScriptCollectionDesigntimeArtifacts", "Script Collection"),
    ("ValueMappingDesigntimeArtifacts", "Value Mapping"),
    ("MessageMappingDesigntimeArtifacts", "Message Mapping"),
    ("IntegrationDesigntimeArtifacts", "Integration Flow"),
    ("MessageTypeDesigntimeArtifacts", "Message Type"),
    ("DataTypeDesigntimeArtifacts", "Data Type"),
]
TYPE_LABELS = [label for _, label in ART_TYPES]
ENTITY_OF = {label: entity for entity, label in ART_TYPES}
SHARED_PACKAGE = re.compile(r"global|common|generic|shared", re.I)

Progress = Optional[Callable[[str], None]]


@dataclass
class SelectedFlow:
    pkg_id: str
    pkg_name: str
    id: str
    name: str
    version: str = ""
    interface_id: str = ""

    @property
    def key(self) -> str:
        return f"{self.pkg_id}||{self.id}"

    def as_flow(self) -> Dict[str, str]:
        return {"Id": self.id, "Name": self.name, "Version": self.version, "reqVersion": "",
                "pkg": {"Id": self.pkg_id, "Name": self.pkg_name}}


class Tenant:
    def __init__(self, client: CpiClient, packages: List[Dict[str, str]]):
        self.client = client
        self.packages = packages
        self.pkg_name = {p["Id"]: p["Name"] for p in packages}
        self.flows_by_pkg: Dict[str, List[Dict[str, str]]] = {}
        self.list_cache: Dict[str, Optional[List[Dict[str, str]]]] = {}
        self.pd_cache: Dict[str, Optional[dict]] = {}
        self.design_cache: Dict[str, List[str]] = {}

    @classmethod
    def connect(cls, base_url: str, token_url: str, client_id: str, client_secret: str, session=None) -> "Tenant":
        token = get_token(token_url, client_id, client_secret, session=session)
        client = CpiClient(base_url, token, session=session)
        rows = client.get_all_paged("/IntegrationPackages")
        packages = sorted(({"Id": r["Id"], "Name": r.get("Name") or r["Id"]} for r in rows), key=lambda p: p["Name"].lower())
        if not packages:
            raise ApiError("The connection works, but no packages were returned.")
        return cls(client, packages)

    # ---- iFlows ------------------------------------------------------------------------
    def list_flows(self, pkg_id: str) -> List[Dict[str, str]]:
        if pkg_id not in self.flows_by_pkg:
            rows = self.client.get_all_paged(f"/IntegrationPackages({lit(pkg_id)})/IntegrationDesigntimeArtifacts")
            self.flows_by_pkg[pkg_id] = sorted(
                ({"Id": r["Id"], "Name": r.get("Name") or r["Id"], "Version": r.get("Version") or ""} for r in rows),
                key=lambda f: f["Name"].lower())
        return self.flows_by_pkg[pkg_id]

    def load_flows(self, pkgs: List[Dict[str, str]], progress: Progress = None) -> None:
        todo = [p for p in pkgs if p["Id"] not in self.flows_by_pkg]
        done = [0]

        def work(p):
            try:
                self.list_flows(p["Id"])
            except ApiError as exc:
                if exc.status is None:
                    raise
                self.flows_by_pkg[p["Id"]] = []
            done[0] += 1
            if progress:
                progress(f"Reading package {done[0]}/{len(todo)}...")

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(work, todo))

    def resolve_iflow_by_id(self, iflow_id: str, progress: Progress = None) -> SelectedFlow:
        """Exact iFlow ID: read directly (active version); falls back to looking through the packages."""
        raw = (iflow_id or "").strip()
        if not raw:
            raise ApiError("Empty iFlow ID.")
        try:
            a = self.client.read_entity("IntegrationDesigntimeArtifacts", raw, "active")
            if a and a.get("Id"):
                pid = a.get("PackageId") or ""
                return SelectedFlow(pid, self.pkg_name.get(pid, pid), a["Id"], a.get("Name") or a["Id"], a.get("Version") or "")
        except ApiError as exc:
            if exc.status in (401, 403):
                raise ApiError(f"HTTP {exc.status} while reading '{raw}'. Check the service key and its roles.", exc.status)
            if exc.status is None:
                raise
        self.load_flows(self.packages, progress)
        for p in self.packages:
            for f in self.flows_by_pkg.get(p["Id"], []):
                if f["Id"].lower() == raw.lower():
                    return SelectedFlow(p["Id"], p["Name"], f["Id"], f["Name"], f["Version"])
        raise ApiError(f"iFlow '{raw}' was not found.")

    # ---- other artifact types ----------------------------------------------------------
    def list_pkg_artifacts(self, pkg_id: str, entity: str) -> Optional[List[Dict[str, str]]]:
        """Artifacts of one type inside a package: [{Id, Name, Version}], or None when that list is not available."""
        key = f"{pkg_id}|{entity}"
        if key not in self.list_cache:
            rows: Optional[List[Dict[str, str]]] = None
            try:
                raw = self.client.get_all_paged(f"/IntegrationPackages({lit(pkg_id)})/{entity}")
                rows = sorted(({"Id": r["Id"], "Name": r.get("Name") or r["Id"], "Version": r.get("Version") or ""} for r in raw),
                              key=lambda r: r["Name"].lower())
            except ApiError as exc:
                if exc.status is None:
                    raise
            self.list_cache[key] = rows
        return self.list_cache[key]

    def package_contents(self, pkg_id: str) -> List[dict]:
        """Everything in a package grouped by type: [{label, items:[{Id,Name}]}] (unavailable lists are empty)."""
        return [{"label": label, "items": self.list_pkg_artifacts(pkg_id, entity) or []} for entity, label in ART_TYPES]
