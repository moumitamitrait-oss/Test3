"""SAP Integration Suite design-time API client (OAuth client credentials, read-only)."""
from __future__ import annotations

import io
import re
import time
import zipfile
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

import requests

PAGE_SIZE = 100
RETRY_STATUSES = (429, 500, 502, 503, 504)
BUNDLE_TEXT_FILES = re.compile(
    r"(\.(mmap|xml|xsl|xslt|groovy|js|prop|properties|json|xsd|wsdl|edmx|project|txt|iflw)|MANIFEST\.MF)$", re.I)


class ApiError(Exception):
    """An API or network problem. `status` is the HTTP status, or None for network / format problems."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class NetworkError(ApiError):
    """The request never got an answer (DNS, proxy, TLS, timeout...)."""


def lit(value: Any) -> str:
    """OData string literal for use in a URL: 'it''s' -> %27... (apostrophes stay readable like in a browser)."""
    return quote("'" + str(value).replace("'", "''") + "'", safe="'()*!~")


def normalize_base(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    if base and not base.endswith("/api/v1"):
        base += "/api/v1"
    return base


def get_token(token_url: str, client_id: str, client_secret: str, session=None, timeout: int = 30) -> str:
    url = (token_url or "").strip()
    if not re.search(r"/oauth/token/?$", url):
        url = url.rstrip("/") + "/oauth/token"
    http = session or requests
    try:
        resp = http.post(url, data={"grant_type": "client_credentials"}, auth=(client_id, client_secret),
                         headers={"Accept": "application/json"}, timeout=timeout)
    except requests.RequestException as exc:
        raise NetworkError(f"Token request failed: {exc}")
    if resp.status_code != 200:
        raise ApiError(f"Token request failed: HTTP {resp.status_code} (check token URL, client ID and client secret).",
                       resp.status_code)
    token = (resp.json() or {}).get("access_token")
    if not token:
        raise ApiError("Token response had no access_token.")
    return token


class CpiClient:
    def __init__(self, base_url: str, token: str, session=None, timeout: int = 60, sleep=time.sleep):
        self.base = normalize_base(base_url)
        self._token = token
        self.session = session or requests.Session()
        self.timeout = timeout
        self._sleep = sleep

    # ---- low level -------------------------------------------------------------------------
    @staticmethod
    def _short(url: str) -> str:
        path = unquote(url.split("?")[0])
        path = re.sub(r"^https?://[^/]+", "", path)
        path = re.sub(r"^.*?/api/v1", "", path)
        return path[:140]

    def _get(self, url: str, accept: str = "application/json"):
        attempt = 1
        while True:
            try:
                resp = self.session.get(url, headers={"Authorization": f"Bearer {self._token}", "Accept": accept},
                                        timeout=self.timeout)
            except requests.RequestException as exc:
                raise NetworkError(f"Network error: {exc}")
            if resp.status_code in RETRY_STATUSES and attempt < 4:
                self._sleep(0.5 * attempt * attempt)
                attempt += 1
                continue
            if not 200 <= resp.status_code < 300:
                raise ApiError(f"HTTP {resp.status_code} for {self._short(url)}", resp.status_code)
            return resp

    def fetch_json(self, url: str) -> Dict[str, Any]:
        resp = self._get(url, "application/json")
        if "json" not in (resp.headers.get("content-type") or "").lower():
            raise ApiError("Non-JSON response (wrong API URL or not authorized)")
        return resp.json()

    def fetch_binary(self, url: str) -> bytes:
        return self._get(url, "*/*").content

    # ---- collections -----------------------------------------------------------------------
    def get_all_paged(self, path: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        sep = "&" if "?" in path else "?"
        skip = 0
        last_first = None
        for _ in range(500):
            data = self.fetch_json(f"{self.base}{path}{sep}$format=json&$top={PAGE_SIZE}&$skip={skip}")
            rows = (data.get("d") or {}).get("results") or []
            if not rows:
                break
            if rows[0].get("Id") and rows[0].get("Id") == last_first:
                break  # the server ignored $skip
            last_first = rows[0].get("Id")
            out.extend(rows)
            if len(rows) < PAGE_SIZE:
                break
            skip += PAGE_SIZE
        return out

    def read_entity(self, entity: str, artifact_id: str, version: str = "active") -> Optional[Dict[str, Any]]:
        data = self.fetch_json(f"{self.base}/{entity}(Id={lit(artifact_id)},Version={lit(version)})?$format=json")
        return data.get("d")

    # ---- iFlows ----------------------------------------------------------------------------
    def get_configurations(self, flow: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
        """Externalized parameters: IntegrationDesigntimeArtifacts(Id='X',Version='1.0.6')/Configurations"""
        def call(version: str):
            data = self.fetch_json(
                f"{self.base}/IntegrationDesigntimeArtifacts(Id={lit(flow['Id'])},Version={lit(version)})/Configurations?$format=json")
            return (data.get("d") or {}).get("results") or []

        req = (flow.get("reqVersion") or "").strip()
        if req:
            try:
                return call(req), req
            except ApiError as exc:
                if exc.status == 404:
                    raise ApiError(f"Version '{req}' not found for iFlow '{flow['Id']}' (HTTP 404)", 404)
                raise
        last: Optional[ApiError] = None
        for version in [v for v in (flow.get("Version"), "active") if v]:
            try:
                return call(version), version
            except ApiError as exc:
                last = exc
                if exc.status != 404:
                    raise
        raise last  # type: ignore[misc]

    def _bundle(self, entity: str, artifact_id: str, version: str) -> zipfile.ZipFile:
        raw = self.fetch_binary(f"{self.base}/{entity}(Id={lit(artifact_id)},Version={lit(version)})/$value")
        return zipfile.ZipFile(io.BytesIO(raw))

    def get_iflow_files(self, flow: Dict[str, Any], version: str) -> Tuple[str, str]:
        """The .iflw XML and the MANIFEST.MF text of an iFlow bundle."""
        zf = self._bundle("IntegrationDesigntimeArtifacts", flow["Id"], version)
        names = zf.namelist()
        iflw = next((n for n in names if n.lower().endswith(".iflw")), None)
        if not iflw:
            raise ApiError("No .iflw file found in the iFlow bundle")
        mf = next((n for n in names if n.upper().endswith("MANIFEST.MF")), None)
        read = lambda n: zf.read(n).decode("utf-8", errors="replace")
        return read(iflw), (read(mf) if mf else "")

    def get_bundle_texts(self, entity: str, artifact_id: str, version: str) -> Dict[str, str]:
        """Text files of any design-time artifact bundle (message mapping, script collection, ...)."""
        zf = self._bundle(entity, artifact_id, version)
        out: Dict[str, str] = {}
        for info in zf.infolist():
            if info.is_dir() or not BUNDLE_TEXT_FILES.search(info.filename):
                continue
            text = zf.read(info).decode("utf-8", errors="replace")
            if len(text) < 3_000_000:
                out[info.filename] = text
        return out
