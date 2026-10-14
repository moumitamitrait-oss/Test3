"""Common dependent artifacts of an iFlow: references to script collections / mappings / value mappings / message types,
what the message mappings themselves use, and the iFlows called through ProcessDirect."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Set

from .api import ApiError, NetworkError
from .design import parse_design, parse_manifest
from .models import CutoverData, Dep, DepResult
from .tenant import ART_TYPES, ENTITY_OF, SHARED_PACKAGE, Tenant

Progress = Optional[Callable[[str], None]]
SKIP_KEYS = re.compile(r"mappingreference|mappingtype|mappingsource|mappingmode|mappingselect", re.I)
SKIP_VALS = re.compile(r"^(static|dynamic|none|default|empty|null|undefined|yes|no|true|false|\d+)$", re.I)

# Always part of the common dependent artifacts (can be unticked or removed)
DEFAULT_DEPS = [("P66_Global_GenericHandlers", "VM_Global_IFlow_to_EMailRecipients", "Value Mapping")]


def hint_for(key: str) -> Optional[str]:
    """The property name already tells the artifact type (scriptBundleId = script collection ...)."""
    if re.search(r"message ?type|msg ?type", key, re.I):
        return "Message Type"
    if re.search(r"data ?type", key, re.I):
        return "Data Type"
    if re.search(r"script", key, re.I):
        return "Script Collection"
    if re.search(r"value ?map", key, re.I):
        return "Value Mapping"
    if re.search(r"mapping", key, re.I):
        return "Message Mapping"
    return None


def with_defaults(deps: List[Dep]) -> List[Dep]:
    for pkg, artifact, system in reversed(DEFAULT_DEPS):
        hit = next((d for d in deps if d.artifact == artifact and d.system == system), None)
        if hit:
            hit.include = True
        else:
            deps.insert(0, Dep(pkg=pkg, artifact=artifact, system=system, source="Default (always included)"))
    return deps


def _priority(p: Dict[str, str]) -> bool:
    return bool(SHARED_PACKAGE.search(p["Name"] + " " + p["Id"]))


# ---------------------------------------------------------------------------------------------------
def scan_packages_for(tenant: Tenant, art_id: str, label: str) -> str:
    """Look through the packages (shared ones first) for an artifact of a known type; returns the package ID."""
    entity = ENTITY_OF[label]
    for group in ([p for p in tenant.packages if _priority(p)], [p for p in tenant.packages if not _priority(p)]):
        def has(p):
            rows = tenant.list_pkg_artifacts(p["Id"], entity)
            return p["Id"] if rows and any(r["Id"] == art_id for r in rows) else ""
        with ThreadPoolExecutor(max_workers=6) as pool:
            for found in pool.map(has, group):
                if found:
                    return found
    return ""


def resolve_artifact(tenant: Tenant, art_id: str, hint: Optional[str]) -> Optional[Dict[str, str]]:
    """Which package does this artifact belong to, and what type is it?
    Type: from the property name (hint); without a hint it is confirmed from the package contents.
    Package: a direct read answers with the PackageId (whatever the endpoint), otherwise the packages are searched."""
    pkg_id = ""
    order = ([(ENTITY_OF[hint], hint)] + [t for t in ART_TYPES if t[1] != hint]) if hint else list(ART_TYPES)
    for entity, _ in order:
        try:
            a = tenant.client.read_entity(entity, art_id, "active")
            if a and a.get("Id"):
                pkg_id = a.get("PackageId") or ""
                break
        except NetworkError:
            raise
        except ApiError:
            continue
    typ = hint
    if not typ and pkg_id:
        for entity, label in ART_TYPES:
            rows = tenant.list_pkg_artifacts(pkg_id, entity)
            if rows and any(r["Id"] == art_id for r in rows):
                typ = label
                break
    if not pkg_id and hint:
        pkg_id = scan_packages_for(tenant, art_id, hint)
    if not pkg_id and not typ:
        return None
    return {"id": art_id, "pkg_id": pkg_id, "type": typ or ""}


# ---- ProcessDirect ----------------------------------------------------------------------------------
def _norm_addr(a: str) -> str:
    return re.sub(r"^/+", "", (a or "").strip().lower())


def _pd_sender_addresses(tenant: Tenant, f: Dict[str, str], pkg: Dict[str, str]) -> List[str]:
    if f["Id"] in tenant.design_cache:
        return tenant.design_cache[f["Id"]]
    addrs: List[str] = []
    try:
        flow = {"Id": f["Id"], "Name": f["Name"], "Version": f.get("Version"), "reqVersion": "", "pkg": pkg}
        xml, manifest = tenant.client.get_iflow_files(flow, f.get("Version") or "active")
        design = parse_design(xml, manifest)
        cfg_map: Optional[Dict[str, Any]] = None
        for ch in design.channels:
            if ch["direction"] != "Sender" or not re.search(r"processdirect", ch["type"], re.I) or not ch["address"]:
                continue
            addr = ch["address"]
            if "{{" in addr:
                if cfg_map is None:
                    try:
                        cfg, _ = tenant.client.get_configurations(flow)
                        cfg_map = {c["ParameterKey"]: c.get("ParameterValue") for c in cfg}
                    except ApiError:
                        cfg_map = {}
                addr = re.sub(r"\{\{([^{}]+)\}\}", lambda m: str(cfg_map.get(m.group(1).strip(), m.group(0))), addr)
            addrs.append(_norm_addr(addr))
    except (ApiError, ValueError):
        pass
    tenant.design_cache[f["Id"]] = addrs
    return addrs


def find_pd_target(tenant: Tenant, address: str, self_flow: Dict[str, str], progress: Progress = None) -> Optional[dict]:
    """The iFlow with a ProcessDirect SENDER on this address. Candidates are ranked by how well their name matches the
    address and verified by reading their design. Same package and shared packages first."""
    target = _norm_addr(address)
    if not target:
        return None
    if target in tenant.pd_cache:
        return tenant.pd_cache[target]
    tokens = [t.lower() for t in re.split(r"[^A-Za-z0-9]+|(?<=[a-z])(?=[A-Z])", address) if len(t) >= 3]
    need = max(1, -(-len(tokens) // 2))

    def one_pass(pkgs):
        tenant.load_flows(pkgs, progress)
        cands = []
        for p in pkgs:
            for f in tenant.flows_by_pkg.get(p["Id"], []):
                if f["Id"] == self_flow["Id"]:
                    continue
                hay = (f["Name"] + " " + f["Id"]).lower()
                score = sum(1 for t in tokens if t in hay)
                if score >= need:
                    cands.append((score, f, p))
        cands.sort(key=lambda c: -c[0])
        for _, f, p in cands[:6]:
            if progress:
                progress(f"Checking {f['Name']} for ProcessDirect {address}...")
            if target in _pd_sender_addresses(tenant, f, {"Id": p["Id"], "Name": p["Name"]}):
                return {"Id": f["Id"], "Name": f["Name"], "pkg_id": p["Id"], "pkg_name": p["Name"]}
        return None

    own = self_flow.get("PackageId")
    prio = [p for p in tenant.packages if p["Id"] == own or _priority(p)]
    result = one_pass(prio) or one_pass([p for p in tenant.packages if p not in prio])
    tenant.pd_cache[target] = result
    return result


# ---- message mappings ------------------------------------------------------------------------------
def shared_index(tenant: Tenant, own_pkg_id: str, progress: Progress = None):
    """Artifacts of the iFlow's own package and of shared packages: (by id, by lower-case name)."""
    pkgs = [p for p in tenant.packages if p["Id"] == own_pkg_id or _priority(p)]
    done = [0]

    def work(p):
        for entity, _ in ART_TYPES:
            tenant.list_pkg_artifacts(p["Id"], entity)
        done[0] += 1
        if progress:
            progress(f"Reading shared packages {done[0]}/{len(pkgs)}...")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(work, pkgs))
    by_id: Dict[str, dict] = {}
    by_name: Dict[str, dict] = {}
    for p in pkgs:
        for entity, label in ART_TYPES:
            for r in tenant.list_cache.get(f"{p['Id']}|{entity}") or []:
                e = {"pkg_name": p["Name"], "pkg_id": p["Id"], "label": label, "id": r["Id"]}
                by_id.setdefault(r["Id"], e)
                by_name.setdefault(r["Name"].lower(), e)
    return by_id, by_name


def extract_type_refs(texts: Dict[str, str]) -> Dict[str, str]:
    """Message / data type references written into a mapping (elements or attributes called messageType / dataType,
    "messageType = Name" style settings). Returns name -> "Message Type" | "Data Type"."""
    import xml.etree.ElementTree as ET

    refs: Dict[str, str] = {}

    def add(name, label):
        n = (name or "").strip()
        if re.fullmatch(r"[A-Za-z_][\w.\-]{2,150}", n) and n not in refs:
            refs[n] = label

    def kind(name: str) -> Optional[str]:
        if re.search(r"message ?type|msg ?type", name, re.I):
            return "Message Type"
        return "Data Type" if re.search(r"data ?type", name, re.I) else None

    for fname, txt in texts.items():
        if re.search(r"\.(xsd|wsdl|edmx)$", fname, re.I):
            continue
        for m in re.finditer(r'(message|data) ?type[^=:"<>\n]{0,30}[=:]\s*"?([A-Za-z_][\w.\-]{2,100})', txt, re.I):
            add(m.group(2), "Message Type" if m.group(1).lower().startswith("m") else "Data Type")
        if not re.search(r"\.(mmap|xml|prop|properties|json|project)$", fname, re.I):
            continue
        try:
            root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", txt).strip())
        except ET.ParseError:
            continue
        for el in root.iter():
            name = el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""
            k = kind(name)
            if k:
                for an, av in el.attrib.items():
                    if re.fullmatch(r"(name|ref|id|uri|artifactid|resourceid)", an.rsplit("}", 1)[-1], re.I):
                        add(av, k)
                if len(el) == 0:
                    add("".join(el.itertext()), k)
            for an, av in el.attrib.items():
                k2 = kind(an.rsplit("}", 1)[-1])
                if k2:
                    add(av, k2)
    return refs


def scan_mapping(tenant: Tenant, dep: Dep, own_pkg_id: str, exclude: Set[str], progress: Progress = None) -> dict:
    """Open a message mapping and look for references to other artifacts (manifest, every identifier in the mapping
    files, message / data types and the schema files of the mapping)."""
    info: Dict[str, Any] = {"found": [], "note": "", "files": []}
    rows = tenant.list_cache.get(f"{dep.pkg_id}|MessageMappingDesigntimeArtifacts") or []
    listed = next((r.get("Version") for r in rows if r["Id"] == dep.artifact), None)
    texts = None
    for version in [v for v in ("active", listed) if v]:
        try:
            texts = tenant.client.get_bundle_texts("MessageMappingDesigntimeArtifacts", dep.artifact, version)
            info["note"] = ""
            break
        except NetworkError:
            raise
        except ApiError as exc:
            info["note"] = f"could not be opened ({exc})"
    if texts is None:
        return info
    by_id, by_name = shared_index(tenant, own_pkg_id, progress)
    src = "Message Mapping " + dep.artifact
    have: Set[str] = set()

    def push(hit: dict, source: str):
        if hit["id"] not in have and hit["id"] not in exclude:
            have.add(hit["id"])
            info["found"].append(Dep(pkg=hit["pkg_name"], pkg_id=hit["pkg_id"], artifact=hit["id"], system=hit["label"], source=source))

    mf = next((n for n in texts if n.upper().endswith("MANIFEST.MF")), None)
    tokens: Set[str] = set()
    for txt in texts.values():
        tokens.update(re.findall(r"[A-Za-z][A-Za-z0-9_.\-]{5,99}", txt))
    if mf:
        tokens.update(parse_manifest(texts[mf]))
    for tok in tokens:
        if tok in by_id:
            push(by_id[tok], src)

    type_refs = extract_type_refs(texts)
    for name, label in type_refs.items():
        hit = by_id.get(name) or by_name.get(name.lower())
        if hit:
            push(hit, f"{src} ({label} reference)")
        elif name not in have:
            have.add(name)
            info["found"].append(Dep(pkg="", artifact=name, system=label, source=f"{src} ({label} reference, package not found)"))
    for fname in texts:
        if re.search(r"\.(xsd|wsdl|edmx)$", fname, re.I):
            base = re.sub(r"\.[^.]+$", "", fname.split("/")[-1])
            hit = by_id.get(base) or by_name.get(base.lower())
            if hit and hit["label"] in ("Message Type", "Data Type"):
                push(hit, f"{src} (schema file {base})")
    info["files"] = [n.split("/")[-1] for n in texts]
    return info


# ---- everything together --------------------------------------------------------------------------
def detect_dependencies(tenant: Tenant, data: CutoverData, progress: Progress = None) -> DepResult:
    me = data.iflow
    result = DepResult()
    seen: Set[str] = set()

    def add(d: Dep):
        key = f"{d.artifact or d.source}|{d.system.split(' (')[0]}"
        if key not in seen:
            seen.add(key)
            result.deps.append(d)

    def resolve_val(v: str) -> str:
        return re.sub(r"\{\{([^{}]+)\}\}", lambda m: str(data.cfg_map.get(m.group(1).strip(), m.group(0))), str(v))

    # 1) references to other artifacts (iFlow properties and MANIFEST Require-Bundle)
    cands: Dict[str, dict] = {}
    for r in data.design.ref_props:
        if SKIP_KEYS.search(r["key"]):
            continue
        v = resolve_val(r["value"])
        if re.fullmatch(r"[\w.\-]{3,100}", v) and not SKIP_VALS.match(v) and v != me["Id"] and v not in cands:
            cands[v] = {"source": "iFlow property " + r["key"], "hint": hint_for(r["key"])}
    for art_id in data.design.manifest_refs:
        if art_id != me["Id"] and not SKIP_VALS.match(art_id) and art_id not in cands:
            cands[art_id] = {"source": "Manifest Require-Bundle", "hint": None}
    for art_id, c in list(cands.items())[:15]:
        if progress:
            progress(f"Resolving reference {art_id}...")
        r = resolve_artifact(tenant, art_id, c["hint"])
        if not r or not r["pkg_id"]:
            result.skipped.append(art_id)
            continue
        pkg = tenant.pkg_name.get(r["pkg_id"], r["pkg_id"])
        if r["type"]:
            add(Dep(pkg=pkg, pkg_id=r["pkg_id"], artifact=r["id"], system=r["type"], source=c["source"]))
        else:
            add(Dep(pkg=pkg, pkg_id=r["pkg_id"], artifact=art_id, system="Referenced artifact (type to confirm)",
                    source=c["source"], include=False))

    # 1b) what do the message mappings themselves reference (global resources)?
    for mm in [d for d in result.deps if d.system == "Message Mapping" and d.pkg_id]:
        if progress:
            progress(f"Scanning message mapping {mm.artifact}...")
        try:
            exclude = {me["Id"], mm.artifact} | {d.artifact for d in result.deps}
            info = scan_mapping(tenant, mm, me.get("PackageId", ""), exclude, progress)
            for d in info["found"]:
                add(d)
            files = f" Files inside: {', '.join(info['files'][:8])}{', ...' if len(info['files']) > 8 else ''}." if info["files"] else ""
            if info["note"]:
                result.notes.append(f"Message Mapping {mm.artifact} {info['note']}.")
            elif info["found"]:
                result.notes.append(f"Message Mapping {mm.artifact}: {len(info['found'])} referenced artifact(s) added "
                                    f"({', '.join(d.artifact for d in info['found'])}).")
            else:
                result.notes.append(f"Message Mapping {mm.artifact}: scanned, no references to other artifacts found.{files}")
        except NetworkError:
            raise
        except (ApiError, ValueError) as exc:
            result.notes.append(f"Message Mapping {mm.artifact}: scan failed ({exc}).")

    # 2) ProcessDirect receivers -> target iFlows
    for ch in data.design.channels:
        if ch["direction"] != "Receiver" or not re.search(r"processdirect", ch["type"], re.I) or not ch["address"]:
            continue
        addr = resolve_val(ch["address"]).strip()
        if not addr or "{{" in addr:
            add(Dep(system="Integration Flow", manual=True, include=False,
                    source="ProcessDirect receiver: address could not be resolved, please select the iFlow"))
            continue
        t = find_pd_target(tenant, addr, me, progress)
        if t:
            add(Dep(pkg=t["pkg_name"], pkg_id=t["pkg_id"], artifact=t["Id"], system="Integration Flow", source="ProcessDirect " + addr))
        else:
            add(Dep(system="Integration Flow", manual=True, include=False,
                    source=f"ProcessDirect {addr}: target iFlow not found, please select it"))
    return result
