"""Cutover data of one iFlow: parameters grouped by sender / receiver / more, credentials, sender and receiver systems."""
from __future__ import annotations

import re
from typing import Any, Dict, List

import zipfile

from .api import ApiError, NetworkError
from .design import Design, parse_design
from .models import CutoverData
from .tenant import Tenant

# Credential values that are settings, not alias names (for example "false", "Basic").
JUNK_KEY = re.compile(r"credentials?(type|mode|method|required|enabled?)|(use|enable)\.?credentials?", re.I)
JUNK_VAL = re.compile(r"^(true|false|none|yes|no|null|undefined|basic|oauth.*|client ?certificate|principal ?propagation|\d+)$", re.I)
ORDER = {"Sender": 0, "Receiver": 1, "More": 2}


def sys_name(participant: str, fallback: str = "") -> str:
    return re.sub(r"^[SR]_", "", participant or "", flags=re.I) or fallback


def is_empty(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def is_pd(channel: dict) -> bool:
    return bool(re.search(r"processdirect", channel.get("type", ""), re.I))


def cutover_data_for(tenant: Tenant, flow: Dict[str, Any]) -> CutoverData:
    client = tenant.client
    cfg, version = client.get_configurations(flow)
    cfg_map = {c["ParameterKey"]: c.get("ParameterValue") for c in cfg}

    warnings: List[str] = []
    design = Design()
    try:
        xml, manifest = client.get_iflow_files(flow, version)
        design = parse_design(xml, manifest)
    except (ApiError, ValueError, zipfile.BadZipFile) as exc:  # the workbook is still created without the design
        if isinstance(exc, NetworkError):
            raise
        warnings.append(f"The iFlow design could not be read ({exc}). All parameters are listed together and "
                        "sender/receiver/credential steps and dependencies are left out.")

    # parameters: under every component that uses them; unused ones under More / All Parameters
    groups: Dict[str, Dict[str, Any]] = {}
    seen_keys = set()
    for c in cfg:
        key = c["ParameterKey"]
        if key in seen_keys:
            continue
        seen_keys.add(key)
        value = "(empty)" if is_empty(c.get("ParameterValue")) else str(c.get("ParameterValue"))
        for category, component in design.usage.get(key) or [("More", "All Parameters")]:
            g = groups.setdefault(f"{category}||{component}", {"category": category, "component": component, "items": []})
            g["items"].append((key, value))
    param_groups = sorted(groups.values(), key=lambda g: ORDER[g["category"]])

    # credential aliases, each with the channel it belongs to
    credentials: List[Dict[str, Any]] = []
    seen_alias = set()
    for ref in sorted(design.cred_refs, key=lambda r: r["rank"]):
        if JUNK_KEY.search(ref.get("key") or ""):
            continue
        m = re.fullmatch(r"\{\{([^{}]+)\}\}", ref["value"])
        value = cfg_map.get(m.group(1).strip()) if m else ref["value"]
        if is_empty(value) or JUNK_VAL.match(str(value).strip()) or value in seen_alias:
            continue
        seen_alias.add(value)
        credentials.append({"alias": str(value).strip(), "rank": ref["rank"], "owner": ref["owner"]})

    # systems: ProcessDirect channels never count; sender system = participant starting with S_ (receiver: R_)
    def pick(direction: str, prefix: str):
        chans = [c for c in design.channels if c["direction"] == direction and not is_pd(c)]
        return next((c for c in chans if re.match(prefix, c["participant"], re.I)), None) or (chans[0] if chans else None)

    sender = pick("Sender", r"S_")
    cred_owner = next((c for c in credentials if c["owner"] and c["owner"]["direction"] == "Receiver" and not is_pd(c["owner"])), None)
    receiver = cred_owner["owner"] if cred_owner else pick("Receiver", r"R_")

    pkg = flow.get("pkg") or {}
    return CutoverData(
        iflow={"Id": flow["Id"], "Name": flow.get("Name") or flow["Id"], "Package": pkg.get("Name", ""),
               "PackageId": pkg.get("Id", ""), "Version": version},
        param_groups=param_groups, credentials=credentials, warnings=warnings, design=design, cfg_map=cfg_map,
        has_sender=sender is not None, timer=design.timer,
        sender_system=sys_name(sender["participant"]) if sender else ("Timer" if design.timer else ""),
        sender_type=sender["type"] if sender else "",
        receiver_system=sys_name(receiver["participant"]) if receiver else "",
        receiver_type=receiver["type"] if receiver else "",
        receiver_credential=cred_owner["alias"] if cred_owner else "",
    )
