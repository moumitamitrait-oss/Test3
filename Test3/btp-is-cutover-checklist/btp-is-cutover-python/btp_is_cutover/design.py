"""Reading an iFlow design (.iflw BPMN XML and MANIFEST.MF)."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

INTERNAL_BUNDLES = re.compile(r"^(com\.sap|org\.|javax\.|java\.|com\.(google|fasterxml|sun)|ch\.|net\.)", re.I)


def parse_manifest(text: str) -> List[str]:
    """IDs from the Require-Bundle header of a MANIFEST.MF (other artifacts the artifact depends on)."""
    joined = re.sub(r"\r?\n ", "", text or "")
    m = re.search(r"^Require-Bundle:\s*(.*)$", joined, re.I | re.M)
    if not m:
        return []
    ids = [e.split(";")[0].strip() for e in re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', m.group(1))]
    return [i for i in ids if i and not INTERNAL_BUNDLES.match(i)]


@dataclass
class Design:
    channels: List[dict] = field(default_factory=list)          # {direction, type, participant, address}
    usage: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)  # parameter key -> [(category, component)]
    cred_refs: List[dict] = field(default_factory=list)         # {rank, value, key, owner}
    ref_props: List[dict] = field(default_factory=list)         # {key, value}: properties that may point to other artifacts
    timer: bool = False
    manifest_refs: List[str] = field(default_factory=list)


def _ln(el) -> str:
    return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""


def _props_of(el) -> Dict[str, str]:
    props: Dict[str, str] = {}
    ext = next((c for c in el if _ln(c) == "extensionElements"), None)
    if ext is None:
        return props
    for p in ext:
        if _ln(p) != "property":
            continue
        key = value = ""
        for ch in p:
            if _ln(ch) == "key":
                key = "".join(ch.itertext())
            elif _ln(ch) == "value":
                value = "".join(ch.itertext())
        if key:
            props[key] = value
    return props


RANK = {"Sender": 0, "Receiver": 1, "More": 2}
REF_KEY = re.compile(r"bundle|valuemap|mappingname|scriptcollection|referenc|resourceid", re.I)


def parse_design(xml_text: str, manifest_text: str = "") -> Design:
    """Channels (with participant names), where {{parameters}} and credentials are used, ProcessDirect addresses,
    properties that may reference other artifacts, and whether the iFlow starts with a timer."""
    try:
        root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", xml_text or "").strip())
    except ET.ParseError as exc:
        raise ValueError(f"iFlow XML could not be parsed ({exc})")
    elements = list(root.iter())
    participants = {e.get("id"): e.get("name") or "" for e in elements if _ln(e) == "participant"}
    design = Design(manifest_refs=parse_manifest(manifest_text))

    for el in elements:
        props = _props_of(el)
        if not props:
            continue
        category, component, owner = "More", "All Parameters", None
        if re.search(r"timer", props.get("activityType", ""), re.I) or (
                _ln(el) == "startEvent" and re.search(r"timer", props.get("activityType") or props.get("cmdVariantUri") or "", re.I)):
            design.timer = True
        if _ln(el) == "messageFlow" and props.get("direction") in ("Sender", "Receiver"):
            category = props["direction"]
            pid = el.get("sourceRef") if category == "Sender" else el.get("targetRef")
            component = participants.get(pid) or ("Sender" if category == "Sender" else "Receiver")
            ctype = props.get("ComponentType") or el.get("name") or ""
            design.channels.append({
                "direction": category, "type": ctype, "participant": component,
                "address": (props.get("address") or "") if re.search(r"processdirect", ctype, re.I) else ""})
            owner = {"direction": category, "participant": component, "type": ctype}
        for key, value in props.items():
            for m in re.finditer(r"\{\{([^{}]+)\}\}", value):
                param = m.group(1).strip()
                places = design.usage.setdefault(param, [])
                if (category, component) not in places:
                    places.append((category, component))
            if re.search(r"credential", key, re.I) and value.strip():
                design.cred_refs.append({"rank": RANK[category], "value": value.strip(), "key": key, "owner": owner})
            if REF_KEY.search(key) and value.strip():
                design.ref_props.append({"key": key, "value": value.strip()})
    if any(_ln(e) == "timerEventDefinition" for e in elements):
        design.timer = True
    return design
