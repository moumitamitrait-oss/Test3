"""A small fake Integration Suite tenant (requests-like session) for the tests and for the GUI smoke test."""
import io
import json
import re
import zipfile
from urllib.parse import unquote


def prop(k, v):
    return f"<ifl:property><key>{k}</key><value>{v}</value></ifl:property>"


HEAD = ('<?xml version="1.0" encoding="UTF-8"?><bpmn2:definitions xmlns:bpmn2="http://www.omg.org/spec/BPMN/20100524/MODEL" '
        'xmlns:ifl="http:///com.sap.ifl.model/Ifl.xsd">')


def part(i, n):
    return f'<bpmn2:participant id="{i}" name="{n}"/>'


def mf(i, direction, ctype, src, tgt, extra=""):
    return (f'<bpmn2:messageFlow id="{i}" name="{ctype}" sourceRef="{src}" targetRef="{tgt}"><bpmn2:extensionElements>'
            f'{prop("direction", direction)}{prop("ComponentType", ctype)}{extra}</bpmn2:extensionElements></bpmn2:messageFlow>')


def wrap(collab, proc=""):
    return f'{HEAD}<bpmn2:collaboration id="C">{collab}</bpmn2:collaboration><bpmn2:process id="PR">{proc}</bpmn2:process></bpmn2:definitions>'


def activity(i, name, *props):
    return f'<bpmn2:callActivity id="{i}" name="{name}"><bpmn2:extensionElements>{"".join(props)}</bpmn2:extensionElements></bpmn2:callActivity>'


# ---- iFlows ------------------------------------------------------------------------------------------
IF_OSS = wrap(
    part("P0", "ProcessDirect") + part("P1", "S_OSS") + part("P2", "R_AlertNotification") + part("P3", "R_SAPS4HANA")
    + mf("M0", "Sender", "ProcessDirect", "P0", "X", prop("address", "/in"))           # a ProcessDirect sender comes first in the file
    + mf("M1", "Sender", "SFTP", "P1", "X", prop("host", "{{SFTP_Host}}"))
    + mf("M2", "Receiver", "ProcessDirect", "Y", "P2", prop("address", "{{PD_Url}}"))
    + mf("M3", "Receiver", "HTTP", "Y", "P3", prop("credentialName", "{{S4_Credential}}") + prop("address", "https://{{S4_Host}}:{{S4_Port}}/orders")),
    activity("CA1", "Script", prop("activityType", "GroovyScript"), prop("scriptBundleId", "SC_Global_ReusableScripts"))
    + activity("CA2", "Map1", prop("activityType", "Mapping"), prop("mappingname", "MM_WithGlobals"), prop("mappingReference", "static"))
    + activity("CA3", "Map2", prop("activityType", "Mapping"), prop("mappingname", "MM_Plain"))
    + activity("CA4", "Map3", prop("activityType", "Mapping"), prop("mappingname", "MM_Locked"))
    + activity("CA5", "Junk", prop("activityType", "Enricher"), prop("credentialName", "false"), prop("senderCredentialType", "Basic"),
               prop("basicAuthCredentialRequired", "false"), prop("useCredential", "true"), prop("scriptBundleId", "SC_Not_In_Tenant")))
IF_PDONLY = wrap(part("P0", "Sender") + part("P3", "R_SAPS4HANA") + mf("M0", "Sender", "ProcessDirect", "P0", "X", prop("address", "/sub"))
                 + mf("M3", "Receiver", "HTTP", "Y", "P3", prop("credentialName", "{{S4_Credential}}")))
IF_TIMER = wrap(part("P3", "R_SAPS4HANA") + mf("M3", "Receiver", "HTTP", "Y", "P3", ""),
                f'<bpmn2:startEvent id="SE" name="Start Timer"><bpmn2:extensionElements>{prop("activityType", "StartTimerEvent")}</bpmn2:extensionElements>'
                '<bpmn2:timerEventDefinition id="T"/></bpmn2:startEvent>')
IF_AMQP = wrap(part("P1", "S_ACTIAN") + part("P3", "R_SAPS4HANA") + mf("M1", "Sender", "AMQP", "P1", "X")
               + mf("M3", "Receiver", "XI", "Y", "P3", prop("credentialName", "{{S4_Credential}}")))
IF_ERR = wrap(part("P1", "Sender") + mf("M1", "Sender", "ProcessDirect", "P1", "X", prop("address", "/Global_ErrorLogging_AlertMail_ServiceNow_AIF")))

MM_WITH = {
    "src/main/resources/mapping/MM_WithGlobals.mmap":
        '<mapping xmlns:x="urn:x"><function name="valueMapping" resource="VM_Global_IFlow_to_EMailRecipients"/><udf import="SC_Global_ReusableScripts"/>'
        '<sourceMessageType name="MT_Order_Source" namespace="urn:demo:orders"/><mapping targetMessageType="MT_Order_Target"/>'
        '<note>ordinary words and ids like Customer_Name</note></mapping>',
    "src/main/resources/xsd/MT_Common_Header.xsd": '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:demo:hdr"/>',
    "META-INF/MANIFEST.MF": 'Manifest-Version: 1.0\r\nRequire-Bundle: Global_Lib_Artifact;bundle-version="1.0.0",com.sap.esb.core;bundle-version="1"\r\nBundle-Name: MM\r\n',
}
MM_PLAIN = {"src/main/resources/mapping/MM_Plain.mmap": "<mapping><function name='copy'/></mapping>",
            "META-INF/MANIFEST.MF": "Manifest-Version: 1.0\r\nBundle-Name: MM_Plain\r\n"}


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


BUNDLES = {
    ("IntegrationDesigntimeArtifacts", "IF_OSS"): _zip({"src/main/resources/scenarioflows/integrationflow/a.iflw": IF_OSS}),
    ("IntegrationDesigntimeArtifacts", "IF_PDONLY"): _zip({"a.iflw": IF_PDONLY}),
    ("IntegrationDesigntimeArtifacts", "IF_TIMER"): _zip({"a.iflw": IF_TIMER}),
    ("IntegrationDesigntimeArtifacts", "IF_AMQP"): _zip({"a.iflw": IF_AMQP}),
    ("IntegrationDesigntimeArtifacts", "IF_Global_ErrorLogging_AlertEmail_ServiceNow_AIF"): _zip({"a.iflw": IF_ERR}),
    ("MessageMappingDesigntimeArtifacts", "MM_WithGlobals"): _zip(MM_WITH),
    ("MessageMappingDesigntimeArtifacts", "MM_Plain"): _zip(MM_PLAIN),
}
PACKAGES = [{"Id": "P_APP", "Name": "P66_HVC_App"}, {"Id": "P_GLOBAL", "Name": "P66_Global_GenericHandlers"},
            {"Id": "P_SC", "Name": "P66_Global_Common_ScriptCollection"}, {"Id": "P_OTHER", "Name": "Unrelated_Package"}]
FLOWS = {
    "P_APP": [("IF_OSS", "IF_OSS_to_SAPS4HANA", "1.0.3"), ("IF_PDONLY", "IF_PDONLY_Sub", "1.0.0"), ("IF_TIMER", "IF_TIMER_Job", "1.0.0"), ("IF_AMQP", "IF_AMQP_Invoice", "1.0.1")],
    "P_GLOBAL": [("IF_Global_ErrorLogging_AlertEmail_ServiceNow_AIF", "IF_Global_ErrorLogging_AlertEmail_ServiceNow_AIF", "2.1.0")],
    "P_SC": [], "P_OTHER": [],
}
CONFIG = {
    "IF_OSS": [("SFTP_Host", "sftp.demo.example.com"), ("S4_Host", "s4.example.com"), ("S4_Port", "443"),
               ("S4_Credential", "SAP_S4_110_PROXY_Cred"), ("PD_Url", "/Global_ErrorLogging_AlertMail_ServiceNow_AIF"), ("Unused_Flag", "")],
    "IF_PDONLY": [("S4_Credential", "SAP_S4_110_PROXY_Cred")], "IF_TIMER": [], "IF_AMQP": [("S4_Credential", "SAP_S4_110_PROXY_Cred")],
}
LISTS = {  # (package, entity) -> [(Id, Name, Version)]
    ("P_APP", "MessageMappingDesigntimeArtifacts"): [("MM_WithGlobals",) * 2 + ("1.0.0",), ("MM_Plain",) * 2 + ("1.0.0",), ("MM_Locked",) * 2 + ("1.0.0",)],
    ("P_GLOBAL", "ValueMappingDesigntimeArtifacts"): [("VM_Global_IFlow_to_EMailRecipients",) * 2 + ("1.0.0",)],
    ("P_GLOBAL", "MessageTypeDesigntimeArtifacts"): [("MT_Order_Source", "MT_Order_Source", "1.0.0"), ("MT_Common_Header", "MT_Common_Header", "1.0.0")],
    ("P_SC", "ScriptCollectionDesigntimeArtifacts"): [("SC_Global_ReusableScripts",) * 2 + ("1.0.0",), ("Global_Lib_Artifact",) * 2 + ("1.0.0",)],
    ("P_OTHER", "ScriptCollectionDesigntimeArtifacts"): [("Customer_Name",) * 2 + ("1.0.0",)],
}
DIRECT = {  # (entity, id) -> package id
    ("ScriptCollectionDesigntimeArtifacts", "SC_Global_ReusableScripts"): "P_SC",
    ("MessageMappingDesigntimeArtifacts", "MM_WithGlobals"): "P_APP", ("MessageMappingDesigntimeArtifacts", "MM_Plain"): "P_APP",
    ("MessageMappingDesigntimeArtifacts", "MM_Locked"): "P_APP",
}
for _pkg, _flows in FLOWS.items():
    for _id, _name, _ver in _flows:
        DIRECT[("IntegrationDesigntimeArtifacts", _id)] = _pkg


class Resp:
    def __init__(self, status=200, body=None, content=None, ctype="application/json"):
        self.status_code = status
        self._body = body
        self.content = content if content is not None else (json.dumps(body).encode() if body is not None else b"")
        self.headers = {"content-type": ctype}

    def json(self):
        return self._body


class FakeSession:
    """Mimics requests.Session.get/post for the tenant above. Set `fail` to simulate problems."""

    def __init__(self):
        self.calls = []
        self.fail = {}          # substring -> status code
        self.direct_status = 200   # 200: direct artifact reads work; 404 / 501: the tenant does not answer them

    def post(self, url, data=None, auth=None, headers=None, timeout=None):
        if auth and auth[1] == "wrong":
            return Resp(401, {})
        return Resp(200, {"access_token": "TOKEN"})

    def get(self, url, headers=None, timeout=None):
        u = unquote(url)
        path = u.split("/api/v1", 1)[1] if "/api/v1" in u else u
        self.calls.append(path.split("?")[0])
        for needle, code in self.fail.items():
            if needle in path:
                return Resp(code, {})
        ok = lambda results: Resp(200, {"d": {"results": [dict(zip(("Id", "Name", "Version"), r)) if isinstance(r, tuple) else r for r in results]}})
        m = re.search(r"/(\w+DesigntimeArtifacts)\(Id='([^']+)',Version='([^']+)'\)(/\$value|/Configurations)?", path)
        if m:
            entity, art_id, version, tail = m.groups()
            if tail == "/$value":
                data = BUNDLES.get((entity, art_id))
                return Resp(200, content=data, ctype="application/octet-stream") if data else Resp(404, {})
            if tail == "/Configurations":
                if version == "9.9.9" or art_id not in CONFIG:
                    return Resp(404, {})
                return Resp(200, {"d": {"results": [{"ParameterKey": k, "ParameterValue": v} for k, v in CONFIG[art_id]]}})
            pkg = DIRECT.get((entity, art_id))
            if pkg and self.direct_status == 200:
                return Resp(200, {"d": {"Id": art_id, "Name": art_id, "PackageId": pkg, "Version": "1.0.0"}})
            return Resp(self.direct_status if self.direct_status != 200 else 404, {})
        m = re.search(r"IntegrationPackages\('([^']+)'\)/(\w+)", path)
        if m:
            pkg, entity = m.groups()
            if entity == "IntegrationDesigntimeArtifacts":
                return ok(FLOWS.get(pkg, []))
            rows = LISTS.get((pkg, entity))
            return ok(rows or [])
        if "IntegrationPackages" in path:
            return ok(PACKAGES)
        return Resp(404, {})
