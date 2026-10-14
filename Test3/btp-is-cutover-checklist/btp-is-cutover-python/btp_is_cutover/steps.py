"""The cutover steps of one iFlow (rows of the CutOver sheet)."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from .models import CutoverData


def common_kind(c: Dict[str, str]) -> str:
    system, artifact = c.get("system", ""), c.get("artifact", "")
    if re.search(r"value ?mapping", system, re.I) or re.match(r"VM_", artifact, re.I):
        return "vm"
    if re.search(r"script", system, re.I) or re.match(r"SC_", artifact, re.I):
        return "sc"
    if re.search(r"message ?mapping", system, re.I) or re.match(r"MM_", artifact, re.I):
        return "mm"
    if re.search(r"message ?type", system, re.I):
        return "mt"
    if re.search(r"data ?type", system, re.I):
        return "dt"
    return "if"


def _validate_text(kind: str, artifact: str) -> str:
    return {
        "if": f"Validate {artifact} flow Deployed or not",
        "sc": f"Validate the {artifact} deployed or not",
        "mm": f"Validate the Message Mapping {artifact} deployed or not",
        "mt": f"Validate the Message Type {artifact} deployed or not",
        "dt": f"Validate the Data Type {artifact} deployed or not",
        "vm": f"Validate the Value Mapping {artifact} deployed or not",
    }[kind]


def build_steps(data: CutoverData, options: Dict[str, Any]) -> List[Dict[str, Any]]:
    """options: env, poc, team_is, team_func, team_sec, common (list of {pkg, artifact, system})."""
    env = options.get("env") or "QA"
    team_is = options.get("team_is") or "Integration Team"
    team_func = options.get("team_func") or "Functional Team"
    team_sec = options.get("team_sec") or "Security Team"
    poc = options.get("poc") or ""
    common = [c for c in (options.get("common") or []) if c.get("artifact")]
    me = data.iflow
    rows: List[Dict[str, Any]] = []

    def step(ref, pkg, action, team, who="", **extra):
        rows.append({"kind": "step", "ref": ref, "pkg": pkg, "action": action, "team": team, "poc": who, **extra})

    def banner(text):
        rows.append({"kind": "banner", "text": text})

    banner("Integration Team")
    # 1) common activities
    for kind in ("if", "sc", "vm", "mm", "mt", "dt"):
        for c in common:
            if common_kind(c) == kind:
                step("Common activities", c["pkg"], _validate_text(kind, c["artifact"]), team_is, poc)
    for cred in data.credentials:
        step("Common activities", "N/A", f"Validate the credentials ({cred['alias']}) maintained in Security Material or not", team_is, poc)

    banner("Integration Team")
    # 2) BTP activities
    vm = next((c for c in common if common_kind(c) == "vm" and re.search(r"mail", c["artifact"], re.I)), None)
    if vm:
        step("BTP Activity", "N/A", "Receive List of Email recipients from Business", team_func)
        step("BTP Activity", vm["pkg"], f"Configure the respective mail Id for the IFlow Id in {vm['artifact']} and Deploy it", team_is, poc)
    # scheduler timings: SFTP sender (polling) or timer start event
    sftp = data.has_sender and re.search(r"sftp", data.sender_type, re.I)
    if sftp or data.timer:
        note = "SFTP sender polling and timer start event" if sftp and data.timer else ("SFTP sender polling schedule" if sftp else "Timer start event")
        step("BTP Activity", "N/A", "Receive the scheduler timings from Business / Functional team", team_func, notes=note)
    step("BTP Activity", me["Package"], f"Validate the Iflow {me['Id']} present in {env} or not", team_is, poc)
    if data.receiver_credential:
        where = f" in {data.receiver_system} {env}" if data.receiver_system else ""
        step("BTP Activity", "N/A", f"Ensure the service user used by credential {data.receiver_credential} has access to post the data{where}", team_sec)
    # share / change sender details: only for a real sender adapter (not ProcessDirect, not a timer start)
    if data.has_sender:
        system = data.sender_system or "Sender system"
        t = data.sender_type or ""
        if re.search(r"AMQP|JMS|KAFKA|MQ", t, re.I):
            what, short = f"{t} queue", "queue"
        elif re.search(r"FTP", t, re.I):
            what, short = f"{t} directory and file", "directory and file"
        else:
            what, short = f"{t + ' ' if t else ''}endpoint", "endpoint"
        step(me["Id"], me["Package"], f"Share the Sender {what} details to {system} Team", team_is, poc)
        step(me["Id"], me["Package"], f"{system} Team will change the {short} details from Dev to {env}", f"{system} Team")
    step(me["Id"], me["Package"], "Configure the required artifact Parameters", team_is, poc)
    step(me["Id"], me["Package"], "Deploy the Interface", team_is, poc)

    banner("Integration Team")
    banner("Smoke Test(Post Cutover)")
    step(me["Id"], me["Package"], "Testing the Interface End to End", team_func, time="-", notes="Test the Interface End to End", smoke=True)

    n = 0
    for r in rows:
        if r["kind"] == "step":
            n += 1
            r["n"] = n
    return rows
