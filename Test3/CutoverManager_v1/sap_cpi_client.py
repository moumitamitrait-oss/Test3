"""
sap_cpi_client.py

A small, dependency-light client for the SAP Cloud Integration
("Integration Suite -> Integration Content") OData v2 API.

What it talks to
-----------------
Every SAP BTP tenant that runs Cloud Integration exposes a REST/OData API at

    https://<host>/api/v1/<EntitySet>

secured with OAuth2 client-credentials. The four values needed to call it
(clientid, clientsecret, tokenurl, url) come from a *service key* that you
generate in the BTP Cockpit for a "Process Integration Runtime" service
instance (api plan). See README.md for the click path.

Responses are OData v2 style JSON, i.e. collections come back as
    {"d": {"results": [ {...}, {...} ]}}
and a package's children are reached via navigation, e.g.:
    IntegrationPackages('MyPackage')/IntegrationDesigntimeArtifacts

Writes (update_parameter, deploy_iflow, create_user_credential) all
require an X-CSRF-Token fetched first via _fetch_csrf_token(), per
SAP's OData write protocol.

This client also talks to the separate "Security Content" OData API
(same tenant, same /api/v1/ base path, different entity sets --
UserCredentials, OAuth2ClientCredentials, SecureParameters) for
managing deployed security artifacts. See list_security_artifacts and
create_user_credential.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import requests


class SAPCPIError(Exception):
    """Base class for every error this client raises on purpose."""


class SAPCPIAuthError(SAPCPIError):
    """Raised when the OAuth2 token request fails."""


class SAPCPIRequestError(SAPCPIError):
    """Raised when a call to the Integration Content API fails."""


# ---------------------------------------------------------------------------
# Environment / credentials
# ---------------------------------------------------------------------------

@dataclass
class SAPCPIEnvironment:
    """The four fields found in a Process Integration Runtime service key."""

    name: str
    clientid: str
    clientsecret: str
    tokenurl: str
    url: str  # API host, e.g. https://xxxx-tmn.<region>.hana.ondemand.com

    @classmethod
    def from_service_key(cls, name: str, key: dict) -> "SAPCPIEnvironment":
        required = ["clientid", "clientsecret", "tokenurl", "url"]
        missing = [f for f in required if not key.get(f)]
        if missing:
            raise ValueError(
                f"Service key for '{name}' is missing {', '.join(missing)}. "
                "A Process Integration Runtime (api plan) service key must "
                "contain clientid, clientsecret, tokenurl and url."
            )
        return cls(
            name=name,
            clientid=key["clientid"],
            clientsecret=key["clientsecret"],
            tokenurl=key["tokenurl"],
            url=key["url"],
        )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class SAPCPIClient:
    """Thin wrapper around two of Cloud Integration's OData v2 APIs: the
    Integration Content API (packages, iFlows, externalized parameters)
    and the separate Security Content API (deployed security artifacts).
    Both share the same tenant, token and /api/v1/ base path."""

    #: friendly label -> OData entity set, all reachable as a navigation
    #: property off a single IntegrationPackages('<Id>') entity.
    ARTIFACT_ENTITY_SETS: dict[str, str] = {
        "Integration Flows": "IntegrationDesigntimeArtifacts",
        "Value Mappings": "ValueMappingDesigntimeArtifacts",
        "Message Mappings": "MessageMappingDesigntimeArtifacts",
        "Script Collections": "ScriptCollectionDesigntimeArtifacts",
    }

    #: friendly label -> Security Content OData entity set. Each is a
    #: top-level collection (unlike ARTIFACT_ENTITY_SETS above, these
    #: aren't nested under a package) -- confirmed against SAP's Security
    #: Content API; see list_security_artifacts.
    SECURITY_ARTIFACT_ENTITY_SETS: dict[str, str] = {
        "User Credential": "UserCredentials",
        "OAuth2 Credential": "OAuth2ClientCredentials",
        "Secure Parameter": "SecureParameters",
    }

    def __init__(self, env: SAPCPIEnvironment, timeout: int = 30):
        self.env = env
        self.timeout = timeout
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0.0
        # a single Session is used for every request (read AND write) so
        # that cookies set during a CSRF-token fetch are carried along to
        # the write request that needs them -- reads never set or need
        # cookies, so this changes nothing about how they behave.
        self._session = requests.Session()

    # ---- auth -------------------------------------------------------

    def _fetch_token(self) -> None:
        token_url = self.env.tokenurl.rstrip("/")
        if not token_url.endswith("/oauth/token"):
            token_url += "/oauth/token"
        try:
            resp = self._session.post(
                token_url,
                data={"grant_type": "client_credentials"},
                auth=(self.env.clientid, self.env.clientsecret),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SAPCPIAuthError(f"Could not reach token endpoint: {exc}") from exc

        if resp.status_code != 200:
            raise SAPCPIAuthError(
                f"Authentication failed (HTTP {resp.status_code}). "
                f"Check clientid/clientsecret/tokenurl. Response: {resp.text[:300]}"
            )

        try:
            payload = resp.json()
            self._access_token = payload["access_token"]
        except (ValueError, KeyError) as exc:
            raise SAPCPIAuthError(f"Unexpected token response: {resp.text[:300]}") from exc

        # refresh a minute early to avoid edge-of-expiry failures
        expires_in = int(payload.get("expires_in", 3600))
        self._token_expiry = time.time() + max(expires_in - 60, 30)

    def _ensure_token(self) -> str:
        if not self._access_token or time.time() >= self._token_expiry:
            self._fetch_token()
        assert self._access_token is not None
        return self._access_token

    def test_connection(self) -> None:
        """Raises SAPCPIAuthError if the environment cannot be authenticated."""
        self._ensure_token()

    # ---- low level ----------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        base = self.env.url.rstrip("/")
        url = f"{base}/api/v1/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {self._ensure_token()}", "Accept": "application/json"}

        try:
            resp = self._session.get(url, headers=headers, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SAPCPIRequestError(f"Request to {path} failed: {exc}") from exc

        if resp.status_code == 401:
            # token may have been revoked/expired server-side; retry once
            self._access_token = None
            headers["Authorization"] = f"Bearer {self._ensure_token()}"
            try:
                resp = self._session.get(url, headers=headers, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                raise SAPCPIRequestError(f"Request to {path} failed: {exc}") from exc

        if resp.status_code == 404:
            raise SAPCPIRequestError(
                f"{path} was not found (HTTP 404) at {url}. This tenant/version "
                "may not expose this entity set under this name -- the full URL "
                "above can be tried directly (e.g. in a browser or Postman, with "
                "a valid bearer token) to confirm independently of this app."
            )
        if resp.status_code == 403:
            raise SAPCPIRequestError(
                f"{path} returned HTTP 403 (Forbidden). The service instance "
                "behind this environment's service key is most likely missing "
                "the role needed to read Integration Content (this varies by "
                "tenant edition — e.g. WorkspacePackagesRead, or a PI_* / "
                "Integration content read role). Check the roles assigned to "
                "the service instance in BTP Cockpit."
            )
        if resp.status_code == 501:
            raise SAPCPIRequestError(
                f"{path} returned HTTP 501 (Not Implemented). Unlike a 403 or "
                "404, this means the tenant itself doesn't implement this "
                "specific operation at all — it's not about permissions, the "
                "artifact's data, or which version was requested. Worth "
                "confirming independently by trying the same call in SAP's "
                "API Business Hub (Integration Content API, try-it-out "
                "feature) with your own credentials, and checking with your "
                "BTP administrator whether this capability is enabled for "
                "this tenant."
            )
        if resp.status_code != 200:
            raise SAPCPIRequestError(f"{path} returned HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            return resp.json()
        except ValueError as exc:
            raise SAPCPIRequestError(f"Non-JSON response from {path}: {exc}") from exc

    def _get_all(self, path: str, extra_params: Optional[dict] = None, page_size: int = 100) -> list[dict]:
        """Page through $top/$skip until a short page signals the last one."""
        results: list[dict] = []
        skip = 0
        extra_params = extra_params or {}
        while True:
            params = {**extra_params, "$top": page_size, "$skip": skip}
            payload = self._get(path, params=params)
            batch = payload.get("d", {}).get("results", [])
            results.extend(batch)
            if len(batch) < page_size:
                break
            skip += page_size
            if skip > 20000:  # sane upper bound so a bug can't loop forever
                break
        return results

    # ---- public API -----------------------------------------------------

    def list_packages(self) -> list[dict]:
        """All integration packages on the tenant, most recently modified first.

        Sorting is done client-side on purpose: some tenants only allow
        $orderby on a subset of properties (e.g. only 'Name'), and rejecting
        an unsupported $orderby raises HTTP 400. Fetching unsorted and
        sorting in Python works the same way on every tenant.
        """
        packages = self._get_all("IntegrationPackages")
        packages.sort(key=lambda p: _odata_millis(p.get("ModifiedDate")), reverse=True)
        return packages

    def list_artifacts(self, package_id: str, entity_set: str) -> list[dict]:
        """Artifacts of one kind (see ARTIFACT_ENTITY_SETS) inside a package."""
        quoted_id = package_id.replace("'", "''")
        path = f"IntegrationPackages('{quoted_id}')/{entity_set}"
        return self._get_all(path)

    def list_externalized_parameters(self, iflow_id: str, fallback_version: str) -> tuple[list[dict], str]:
        """The externalized ('Configure'-screen) parameters of one integration
        flow. Returns (parameters, version_actually_used).

        Configured *values* are tied to the active (deployed) version of the
        iFlow, which is not always the same version number shown when
        listing iFlows in a package (that's the saved workspace version, and
        can be ahead of what's actually deployed). Querying the exact saved
        version can come back empty even when the iFlow clearly has
        configured parameters in the Web UI's Configure screen.

        So this always tries 'active' first, and only falls back to
        fallback_version (normally the Version from list_artifacts) if
        'active' can't be resolved — which happens for an iFlow that has
        been saved but never deployed.

        This entity is only exposed off IntegrationDesigntimeArtifacts (i.e.
        iFlows specifically) — value mappings, message mappings and script
        collections don't have a Configurations sub-resource.
        """
        try:
            return self._fetch_configurations(iflow_id, "active"), "active"
        except SAPCPIRequestError:
            if fallback_version and fallback_version != "active":
                return self._fetch_configurations(iflow_id, fallback_version), fallback_version
            raise

    def _fetch_configurations(self, iflow_id: str, version: str) -> list[dict]:
        quoted_id = iflow_id.replace("'", "''")
        quoted_version = version.replace("'", "''")
        path = f"IntegrationDesigntimeArtifacts(Id='{quoted_id}',Version='{quoted_version}')/Configurations"
        # Deliberately NOT using _get_all here: some tenants return HTTP 501
        # for $top/$skip on this specific nested collection (confirmed via a
        # direct Postman test against a real tenant), even though the same
        # tenant supports paging fine on top-level entity sets like
        # IntegrationPackages. This collection is always small (a handful of
        # externalized parameters per iFlow), so a single unpaginated GET is
        # both safer and sufficient.
        payload = self._get(path)
        params = payload.get("d", {}).get("results", [])
        # sorted client-side rather than via $orderby, for the same reason as
        # list_packages: don't depend on which properties a given tenant
        # happens to allow server-side ordering on.
        params.sort(key=lambda p: str(p.get("ParameterKey", "")).lower())
        return params

    # ---- write operations ----------------------------------------------
    #
    # Everything below this point writes to the tenant (update a parameter
    # value, or trigger a deployment). Unlike every read method above,
    # these need a CSRF token: SAP's OData services reject POST/PUT/DELETE
    # without one, even with a valid bearer token. The mechanism (fetch a
    # token via a GET with X-CSRF-Token: Fetch, then send it back on the
    # write) and the $batch payload shape below are taken directly from a
    # working example published by an SAP Developer Advocate on SAP's own
    # Community blog, cross-checked against SAP's help documentation for
    # the same operation.

    def _fetch_csrf_token(self) -> str:
        """Fetch an X-CSRF-Token, required before any write (POST/PUT/DELETE)
        call. Uses the shared session so the cookies tied to this token are
        automatically sent with the write request that follows."""
        base = self.env.url.rstrip("/")
        url = f"{base}/api/v1/"
        headers = {"Authorization": f"Bearer {self._ensure_token()}", "X-CSRF-Token": "Fetch"}
        try:
            resp = self._session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SAPCPIRequestError(f"Could not fetch a CSRF token: {exc}") from exc

        token = resp.headers.get("X-CSRF-Token")
        if not token:
            raise SAPCPIRequestError(
                "The tenant didn't return an X-CSRF-Token header, which write "
                "operations require. This can mean the service instance behind "
                "this environment doesn't have write access to Integration "
                "Content — check its assigned roles in BTP Cockpit."
            )
        return token

    def update_parameter(
        self, iflow_id: str, version: str, parameter_key: str, new_value: str, data_type: str
    ) -> None:
        """Update one externalized parameter's value on a specific iFlow version.

        version must be the exact version the parameter currently lives on —
        normally version_used, the second value list_externalized_parameters
        returns, not a symbolic reference. data_type should be the value's
        existing DataType (e.g. "xsd:string"), unchanged.
        """
        quoted_id = iflow_id.replace("'", "''")
        quoted_version = version.replace("'", "''")
        quoted_key = parameter_key.replace("'", "''")

        csrf_token = self._fetch_csrf_token()

        batch_id = uuid.uuid4().hex
        changeset_id = uuid.uuid4().hex
        body_json = json.dumps(
            {"ParameterKey": parameter_key, "ParameterValue": new_value, "DataType": data_type}
        )

        # OData v2 $batch payload: a batch containing one changeset containing
        # one PUT. Line endings MUST be \r\n -- SAP's batch parser returns a
        # plain 400 if they're bare \n, per the source example's own note.
        payload = (
            f"--batch_{batch_id}\r\n"
            f"Content-Type: multipart/mixed; boundary=changeset_{changeset_id}\r\n"
            f"\r\n"
            f"--changeset_{changeset_id}\r\n"
            f"Content-Type: application/http\r\n"
            f"Content-Transfer-Encoding: binary\r\n"
            f"\r\n"
            f"PUT IntegrationDesigntimeArtifacts(Id='{quoted_id}',Version='{quoted_version}')"
            f"/$links/Configurations('{quoted_key}') HTTP/1.1\r\n"
            f"Accept: application/json\r\n"
            f"Content-Type: application/json\r\n"
            f"\r\n"
            f"{body_json}\r\n"
            f"\r\n"
            f"--changeset_{changeset_id}--\r\n"
            f"--batch_{batch_id}--\r\n"
        )

        base = self.env.url.rstrip("/")
        url = f"{base}/api/v1/$batch"
        headers = {
            "Authorization": f"Bearer {self._ensure_token()}",
            "X-CSRF-Token": csrf_token,
            "Content-Type": f"multipart/mixed; boundary=batch_{batch_id}",
            "Accept": "multipart/mixed",
        }
        try:
            resp = self._session.post(url, headers=headers, data=payload.encode("utf-8"), timeout=self.timeout)
        except requests.RequestException as exc:
            raise SAPCPIRequestError(f"Update request failed: {exc}") from exc

        if resp.status_code >= 300:
            raise SAPCPIRequestError(f"Batch update returned HTTP {resp.status_code}: {resp.text[:400]}")

        # The outer $batch response is ~always 2xx even when the inner PUT
        # failed -- the real result is a mini HTTP response embedded inside
        # the multipart body, so that's what has to be checked.
        inner_statuses = re.findall(r"HTTP/1\.1 (\d{3})", resp.text)
        if not inner_statuses:
            raise SAPCPIRequestError(
                f"Could not confirm the update succeeded — no embedded HTTP "
                f"status found in the batch response. Raw response: {resp.text[:500]}"
            )
        if not all(200 <= int(code) < 300 for code in inner_statuses):
            status_code, detail = _parse_batch_error_detail(resp.text)
            raise SAPCPIRequestError(
                f"The tenant rejected the update (HTTP {status_code or inner_statuses[0]}): {detail}"
            )

    def deploy_iflow(self, iflow_id: str, version: str) -> None:
        """Deploy a specific version of an integration flow, making it the
        active/running one. version must be a concrete version (e.g.
        "1.0.1"), not "active" -- "active" identifies what's *currently*
        deployed for reading, which is a different question from what
        version to deploy.

        Deployment is asynchronous on the tenant's side: a successful
        response here means the request was accepted, not that the
        deployment has finished. There's no dedicated endpoint in this
        API for polling that to completion, so the caller (the UI) should
        say so rather than imply it's instant.
        """
        quoted_id = iflow_id.replace("'", "''")
        quoted_version = version.replace("'", "''")

        csrf_token = self._fetch_csrf_token()
        base = self.env.url.rstrip("/")
        url = f"{base}/api/v1/DeployIntegrationDesigntimeArtifact"
        params = {"Id": f"'{quoted_id}'", "Version": f"'{quoted_version}'"}
        headers = {"Authorization": f"Bearer {self._ensure_token()}", "X-CSRF-Token": csrf_token}
        try:
            resp = self._session.post(url, headers=headers, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SAPCPIRequestError(f"Deploy request failed: {exc}") from exc

        if resp.status_code not in (200, 202):
            raise SAPCPIRequestError(f"Deploy returned HTTP {resp.status_code}: {resp.text[:400]}")

    # ---- Security Material: User Credentials -----------------------------
    #
    # A separate part of the same api/v1 OData service from the rest of this
    # client (same tenant, same OAuth2 token, same CSRF mechanism) -- see
    # https://api.sap.com/api/SecurityContent/resource/User_Credentials.
    # IMPORTANT: once a credential is deployed, the tenant never returns its
    # password again via GET -- only the username comes back. That's a
    # property of the API itself (any credential store that handed real
    # passwords back out on a list call would defeat the point of having
    # one), not a limitation of this client, so list_user_credentials()
    # simply won't have one to return.

    def list_user_credentials(self) -> list[dict]:
        """Every User Credential currently deployed in this tenant's
        Security Material. Passwords are never included -- see above.

        A plain GET, with NO $top/$skip. "UserCredentials" (plural) is
        the correct, confirmed entity set name -- verified directly
        against a real tenant via Postman, which succeeds with a bare
        request and no query parameters at all. This entity set has been
        observed to 404 specifically when $top/$skip ARE present, the
        same kind of per-endpoint quirk already seen with Configurations
        (which also had to drop pagination params) -- so this calls _get
        directly rather than _get_all, which always adds them.

        Falls back to the singular "UserCredential" purely as an extra
        safety net, in case some other tenant genuinely uses that name
        instead -- see discover_security_entity_sets() for how to find
        out definitively if neither works.
        """
        try:
            payload = self._get("UserCredentials")
        except SAPCPIRequestError as exc:
            if "404" not in str(exc):
                raise
            payload = self._get("UserCredential")
        return payload.get("d", {}).get("results", [])

    def _fetch_metadata_xml(self) -> str:
        """Fetches this tenant's own api/v1 $metadata (the OData service's
        own description of what it exposes) as raw XML. Shared by
        discover_security_entity_sets() and discover_entity_properties()
        below, so each one doesn't need to re-fetch it separately.
        """
        base = self.env.url.rstrip("/")
        url = f"{base}/api/v1/$metadata"
        headers = {"Authorization": f"Bearer {self._ensure_token()}"}
        try:
            resp = self._session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SAPCPIRequestError(f"Could not fetch {url}: {exc}") from exc
        if resp.status_code != 200:
            raise SAPCPIRequestError(
                f"$metadata returned HTTP {resp.status_code} at {url}: {resp.text[:300]}"
            )
        return resp.text

    def discover_security_entity_sets(self, keyword: str = "credential") -> list[str]:
        """Returns every entity set name in this tenant's own $metadata
        containing keyword (case-insensitive) -- "credential" by default,
        which matches both "UserCredentials" and "OAuth2ClientCredentials";
        pass "parameter" to find the Secure Parameter entity set instead,
        since that name doesn't contain "credential".

        This exists specifically for when a guessed entity-set name
        404s -- rather than guess yet another name, this asks the tenant
        directly what it actually calls the thing, which also surfaces
        whether the real issue is a naming mismatch (some matching name
        comes back) or more likely a missing role/permission on the
        service key (nothing does, even though the tenant is known to
        have the artifact type deployed).
        """
        xml = self._fetch_metadata_xml()
        names = re.findall(r'<EntitySet\b[^>]*\bName="([^"]+)"', xml)
        return sorted({n for n in names if keyword.lower() in n.lower()})

    def discover_entity_properties(self, entity_set_name: str) -> list[str]:
        """Returns the property names (field names) of the EntityType
        backing entity_set_name, by reading this tenant's own $metadata --
        i.e. what this tenant's real schema calls each field of a given
        entity, rather than assuming a guessed name is correct.

        Used as a diagnostic for OAuth2 Client Credentials and Secure
        Parameter specifically: unlike User Credentials (whose field
        names -- Name, Kind, Description, User, Password, CompanyId --
        were confirmed directly against a real tenant via Postman), this
        app's OAuth2/Secure Parameter field names are its best-informed
        guess from SAP's own public documentation, not something
        confirmed the same way. If a create/update call to either comes
        back with a field-related rejection, this is the way to check
        what the tenant actually calls that field.

        Returns an empty list if entity_set_name itself, or its backing
        EntityType, isn't found in $metadata (e.g. a typo, or a tenant
        that genuinely doesn't expose this artifact type).
        """
        xml = self._fetch_metadata_xml()
        # the EntitySet element names its EntityType, usually schema-qualified
        # (e.g. "ServiceNamespace.OAuth2ClientCredentials") -- strip any
        # namespace prefix so the lookup below works regardless of what the
        # tenant's own schema happens to be called.
        # (?:[^".]+\.)* strips every dot-delimited namespace segment, not
        # just one -- a real namespace like "com.sap.it.api" has several
        # dots, and a group that can only match once (a trailing ? rather
        # than *) would strip just the first segment and then capture the
        # REST of the (still dotted) namespace by mistake, not the bare
        # type name alone. Caught and fixed via this method's own test,
        # which is exactly why it's a dedicated, directly-tested function.
        set_match = re.search(
            rf'<EntitySet\b[^>]*\bName="{re.escape(entity_set_name)}"[^>]*\bEntityType="(?:[^".]+\.)*([^".]+)"',
            xml,
        )
        if not set_match:
            return []
        entity_type = set_match.group(1)
        # the EntityType element's own closing tag may be self-closing
        # (<EntityType .../>, no properties) or have a body with one
        # <Property Name="..."/> per field -- search only within that one
        # EntityType's block, not the whole document, so a same-named
        # property on a different, unrelated EntityType is never picked up.
        type_match = re.search(
            rf'<EntityType\b[^>]*\bName="{re.escape(entity_type)}"[^>]*>(.*?)</EntityType>',
            xml,
            re.DOTALL,
        )
        if not type_match:
            return []
        return re.findall(r'<Property\b[^>]*\bName="([^"]+)"', type_match.group(1))

    def create_user_credential(
        self,
        name: str,
        kind: str,
        description: str,
        user: str,
        password: str,
        company_id: str = "",
    ) -> None:
        """Create (deploy) a new User Credential.

        kind must be one of "default", "successfactors", "openconnectors".
        company_id is required by the tenant when kind == "successfactors"
        and should be left as "" otherwise -- this method doesn't enforce
        that itself (the caller/UI does, since it can show a clearer,
        earlier message than waiting for the tenant to reject the call).
        """
        csrf_token = self._fetch_csrf_token()
        body = {
            "Name": name,
            "Kind": kind,
            "Description": description,
            "User": user,
            "Password": password,
            "CompanyId": company_id,
        }
        base = self.env.url.rstrip("/")
        url = f"{base}/api/v1/UserCredentials"
        headers = {
            "Authorization": f"Bearer {self._ensure_token()}",
            "X-CSRF-Token": csrf_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            resp = self._session.post(
                url, headers=headers, data=json.dumps(body).encode("utf-8"), timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise SAPCPIRequestError(f"Create user credential request failed: {exc}") from exc

        if resp.status_code not in (200, 201, 202):
            # 202 (Accepted) is a genuine success here -- confirmed against
            # a real tenant, which deploys the credential and returns 202,
            # the same convention deploy_iflow() already accounts for
            # elsewhere in this client. Only treat this as a failure for
            # status codes outside all three.
            reason = _parse_odata_error_detail(resp.text) or resp.text[:300]
            raise SAPCPIRequestError(
                f"The tenant rejected '{name}' (HTTP {resp.status_code}): {reason}"
            )

    def get_user_credential(self, name: str) -> Optional[dict]:
        """Fetches a single User Credential by name (the keyed-entity GET,
        UserCredentials('<name>') -- the same URL pattern the tenant's own
        __metadata.id/uri already use for each row in a list response).

        Returns None if the tenant says it doesn't exist (HTTP 404);
        raises for any other error. Used by upsert_user_credential() to
        decide whether a given name needs a create (POST) or an update
        (PUT).
        """
        quoted_name = name.replace("'", "''")
        try:
            payload = self._get(f"UserCredentials('{quoted_name}')")
        except SAPCPIRequestError as exc:
            if "404" in str(exc):
                return None
            raise
        return payload.get("d")

    def update_user_credential(
        self,
        name: str,
        kind: str,
        description: str,
        user: str,
        password: str,
        company_id: str = "",
    ) -> None:
        """Update an existing User Credential (PUT to the keyed entity,
        UserCredentials('<name>')) -- same fields and the same "202 is a
        genuine success" handling as create_user_credential(), confirmed
        against a real tenant's own example for this exact call.
        """
        quoted_name = name.replace("'", "''")
        csrf_token = self._fetch_csrf_token()
        body = {
            "Name": name,
            "Kind": kind,
            "Description": description,
            "User": user,
            "Password": password,
            "CompanyId": company_id,
        }
        base = self.env.url.rstrip("/")
        url = f"{base}/api/v1/UserCredentials('{quoted_name}')"
        headers = {
            "Authorization": f"Bearer {self._ensure_token()}",
            "X-CSRF-Token": csrf_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            resp = self._session.put(
                url, headers=headers, data=json.dumps(body).encode("utf-8"), timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise SAPCPIRequestError(f"Update user credential request failed: {exc}") from exc

        if resp.status_code not in (200, 202, 204):
            # 204 (No Content) is included here too -- a very common,
            # genuinely successful response shape for a PUT/update
            # specifically (there's nothing new to return), distinct from
            # create's 200/201/202.
            reason = _parse_odata_error_detail(resp.text) or resp.text[:300]
            raise SAPCPIRequestError(
                f"The tenant rejected the update for '{name}' (HTTP {resp.status_code}): {reason}"
            )

    def upsert_user_credential(
        self,
        name: str,
        kind: str,
        description: str,
        user: str,
        password: str,
        company_id: str = "",
    ) -> str:
        """Creates a new User Credential, or updates it in place if one
        with this exact Name already exists -- checks first via
        get_user_credential(), then calls create_user_credential() or
        update_user_credential() accordingly.

        Returns "created" or "updated" so the caller can report which one
        actually happened, since both are legitimate, successful outcomes
        that still mean different things to show the person.
        """
        existing = self.get_user_credential(name)
        if existing is not None:
            self.update_user_credential(name, kind, description, user, password, company_id)
            return "updated"
        self.create_user_credential(name, kind, description, user, password, company_id)
        return "created"

    # -- OAuth2 Client Credentials -----------------------------------------
    #
    # Entity set name "OAuth2ClientCredentials" is confirmed -- it's the
    # exact name in the SAP Business Accelerator Hub's own API path for
    # this (api.sap.com/api/SecurityContent/path/post_OAuth2ClientCredentials).
    # The field names below (Name, Description, ClientId, ClientSecret,
    # TokenServiceUrl) are this app's best-informed reading of SAP's own
    # Help Portal page for deploying this artifact type (attribute labels:
    # Name, Description, Token Service URL, Client ID, Client Secret) --
    # NOT independently confirmed against a real tenant's $metadata the
    # way every User Credential field name already was. If a create/update
    # call comes back rejected in a way that looks field-related, call
    # discover_entity_properties("OAuth2ClientCredentials") to see what
    # this tenant's own schema actually calls each field, and adjust the
    # bodies built below to match.
    #
    # SAP's own Help Portal page also lists a "Grant Type" attribute (how
    # the grant type is sent to the token endpoint -- as part of the URL,
    # or as part of the request body), which is deliberately left out of
    # what's sent here: there's no public documentation of its exact field
    # name or the literal values it accepts, and guessing wrong in a
    # request body risks the tenant silently ignoring it or rejecting the
    # whole request, which is worse than simply not setting it (the tenant
    # applies its own default). Add it once discover_entity_properties
    # confirms the real field name.

    def list_oauth2_credentials(self) -> list[dict]:
        """Every OAuth2 Client Credentials artifact currently deployed in
        this tenant's Security Material. ClientSecret is expected to come
        back blank/omitted the same way Password does for User
        Credentials (SAP's API only ever returns the non-secret fields
        once something is deployed) -- not a bug in this app if so.

        A plain GET, no $top/$skip, mirroring the confirmed UserCredentials
        behavior -- this tenant's Security Content entity sets have been
        observed to 404 specifically when pagination params are present.
        """
        payload = self._get("OAuth2ClientCredentials")
        return payload.get("d", {}).get("results", [])

    def get_oauth2_credential(self, name: str) -> Optional[dict]:
        """Fetches a single OAuth2 Client Credentials artifact by name.
        Returns None on HTTP 404 (doesn't exist); raises for any other
        error. Used by upsert_oauth2_credential() to decide create vs.
        update, the same role get_user_credential() plays there.
        """
        quoted_name = name.replace("'", "''")
        try:
            payload = self._get(f"OAuth2ClientCredentials('{quoted_name}')")
        except SAPCPIRequestError as exc:
            if "404" in str(exc):
                return None
            raise
        return payload.get("d")

    def create_oauth2_credential(
        self, name: str, description: str, client_id: str, client_secret: str, token_service_url: str
    ) -> None:
        """Create (deploy) a new OAuth2 Client Credentials artifact."""
        csrf_token = self._fetch_csrf_token()
        body = {
            "Name": name,
            "Description": description,
            "ClientId": client_id,
            "ClientSecret": client_secret,
            "TokenServiceUrl": token_service_url,
        }
        base = self.env.url.rstrip("/")
        url = f"{base}/api/v1/OAuth2ClientCredentials"
        headers = {
            "Authorization": f"Bearer {self._ensure_token()}",
            "X-CSRF-Token": csrf_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            resp = self._session.post(
                url, headers=headers, data=json.dumps(body).encode("utf-8"), timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise SAPCPIRequestError(f"Create OAuth2 Client Credentials request failed: {exc}") from exc
        if resp.status_code not in (200, 201, 202):
            # 200/201/202 accepted for the same reason as User Credentials:
            # a real tenant has been confirmed to answer a successful
            # create with 202 (Accepted), not 201, for this family of
            # Security Content artifacts.
            reason = _parse_odata_error_detail(resp.text) or resp.text[:300]
            raise SAPCPIRequestError(
                f"The tenant rejected '{name}' (HTTP {resp.status_code}): {reason}"
            )

    def update_oauth2_credential(
        self, name: str, description: str, client_id: str, client_secret: str, token_service_url: str
    ) -> None:
        """Update an existing OAuth2 Client Credentials artifact (PUT to
        the keyed entity) -- same fields and status-code handling as
        create_oauth2_credential(), mirroring update_user_credential()."""
        quoted_name = name.replace("'", "''")
        csrf_token = self._fetch_csrf_token()
        body = {
            "Name": name,
            "Description": description,
            "ClientId": client_id,
            "ClientSecret": client_secret,
            "TokenServiceUrl": token_service_url,
        }
        base = self.env.url.rstrip("/")
        url = f"{base}/api/v1/OAuth2ClientCredentials('{quoted_name}')"
        headers = {
            "Authorization": f"Bearer {self._ensure_token()}",
            "X-CSRF-Token": csrf_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            resp = self._session.put(
                url, headers=headers, data=json.dumps(body).encode("utf-8"), timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise SAPCPIRequestError(f"Update OAuth2 Client Credentials request failed: {exc}") from exc
        if resp.status_code not in (200, 202, 204):
            reason = _parse_odata_error_detail(resp.text) or resp.text[:300]
            raise SAPCPIRequestError(
                f"The tenant rejected the update for '{name}' (HTTP {resp.status_code}): {reason}"
            )

    def upsert_oauth2_credential(
        self, name: str, description: str, client_id: str, client_secret: str, token_service_url: str
    ) -> str:
        """Creates a new OAuth2 Client Credentials artifact, or updates it
        in place if one with this exact Name already exists. Returns
        "created" or "updated", mirroring upsert_user_credential()."""
        existing = self.get_oauth2_credential(name)
        if existing is not None:
            self.update_oauth2_credential(name, description, client_id, client_secret, token_service_url)
            return "updated"
        self.create_oauth2_credential(name, description, client_id, client_secret, token_service_url)
        return "created"

    # -- Secure Parameters ---------------------------------------------
    #
    # Entity set name "SecureParameters" is this app's best-informed guess
    # -- NOT independently confirmed the way "OAuth2ClientCredentials" was
    # (that one came directly from the user-supplied SAP API Business Hub
    # path). SAP's own Help Portal / community documentation for deploying
    # a Secure Parameter describes only the Web UI's attributes (Name,
    # Description, and the secret value itself), not the OData entity set
    # or field names. The secret-value field is "SecureParam" -- confirmed
    # against a real tenant via Postman (POST {baseUrl}/SecureParameters
    # with body {"Name", "SecureParam", "Description"}). The PUT URL
    # (SecureParameters('<name>')) follows the same keyed-entity pattern
    # as UserCredentials('<name>') and is not yet independently confirmed;
    # if an update is rejected, verify it in Postman first.

    def list_secure_parameters(self) -> list[dict]:
        """Every Secure Parameter currently deployed in this tenant's
        Security Material. The secret Value is expected to come back
        blank/omitted once deployed, the same as Password for User
        Credentials and ClientSecret for OAuth2 Client Credentials.

        A plain GET, no $top/$skip, mirroring the other two confirmed
        entity sets in this same Security Content API family.
        """
        payload = self._get("SecureParameters")
        return payload.get("d", {}).get("results", [])

    def get_secure_parameter(self, name: str) -> Optional[dict]:
        """Fetches a single Secure Parameter by name. Returns None on HTTP
        404 (doesn't exist); raises for any other error. Used by
        upsert_secure_parameter() to decide create vs. update."""
        quoted_name = name.replace("'", "''")
        try:
            payload = self._get(f"SecureParameters('{quoted_name}')")
        except SAPCPIRequestError as exc:
            if "404" in str(exc):
                return None
            raise
        return payload.get("d")

    def create_secure_parameter(self, name: str, description: str, value: str) -> None:
        """Create (deploy) a new Secure Parameter."""
        csrf_token = self._fetch_csrf_token()
        body = {"Name": name, "Description": description, "SecureParam": value}
        base = self.env.url.rstrip("/")
        url = f"{base}/api/v1/SecureParameters"
        headers = {
            "Authorization": f"Bearer {self._ensure_token()}",
            "X-CSRF-Token": csrf_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            resp = self._session.post(
                url, headers=headers, data=json.dumps(body).encode("utf-8"), timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise SAPCPIRequestError(f"Create Secure Parameter request failed: {exc}") from exc
        if resp.status_code not in (200, 201, 202):
            reason = _parse_odata_error_detail(resp.text) or resp.text[:300]
            raise SAPCPIRequestError(
                f"The tenant rejected '{name}' (HTTP {resp.status_code}): {reason}"
            )

    def update_secure_parameter(self, name: str, description: str, value: str) -> None:
        """Update an existing Secure Parameter (PUT to the keyed entity)
        -- same status-code handling as create_secure_parameter()."""
        quoted_name = name.replace("'", "''")
        csrf_token = self._fetch_csrf_token()
        body = {"Name": name, "Description": description, "SecureParam": value}
        base = self.env.url.rstrip("/")
        url = f"{base}/api/v1/SecureParameters('{quoted_name}')"
        headers = {
            "Authorization": f"Bearer {self._ensure_token()}",
            "X-CSRF-Token": csrf_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            resp = self._session.put(
                url, headers=headers, data=json.dumps(body).encode("utf-8"), timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise SAPCPIRequestError(f"Update Secure Parameter request failed: {exc}") from exc
        if resp.status_code not in (200, 202, 204):
            reason = _parse_odata_error_detail(resp.text) or resp.text[:300]
            raise SAPCPIRequestError(
                f"The tenant rejected the update for '{name}' (HTTP {resp.status_code}): {reason}"
            )

    def upsert_secure_parameter(self, name: str, description: str, value: str) -> str:
        """Creates a new Secure Parameter, or updates it in place if one
        with this exact Name already exists. Returns "created" or
        "updated", mirroring upsert_user_credential()."""
        existing = self.get_secure_parameter(name)
        if existing is not None:
            self.update_secure_parameter(name, description, value)
            return "updated"
        self.create_secure_parameter(name, description, value)
        return "created"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ODATA_DATE_RE = re.compile(r"/Date\((-?\d+)\)/")
_EMBEDDED_STATUS_RE = re.compile(r"HTTP/1\.1 (\d{3})([^\r\n]*)\r?\n(.*)", re.DOTALL)


def _parse_batch_error_detail(response_text: str) -> tuple[Optional[int], str]:
    """Pull a short, human-readable detail out of a $batch error response,
    instead of dumping the whole raw multipart envelope (boundaries,
    MIME headers, etc.) in front of a user.

    SAP's embedded error body is sometimes genuinely useful JSON, and
    sometimes just the request URI echoed back as plain text (observed
    on a real tenant) -- in the latter case there's nothing informative
    to show, so this falls back to the bare status and reason phrase
    rather than that confusing URI.
    """
    match = _EMBEDDED_STATUS_RE.search(response_text)
    if not match:
        return None, response_text[:200]

    status_code = int(match.group(1))
    reason = match.group(2).strip()
    rest = match.group(3)

    # the embedded response's own headers end at the first blank line;
    # whatever follows, up to the next MIME boundary, is its body
    parts = re.split(r"\r?\n\r?\n", rest, maxsplit=1)
    body = parts[1] if len(parts) > 1 else ""
    body = re.split(r"\r?\n--", body, maxsplit=1)[0].strip()

    text = _parse_odata_error_detail(body)
    if text:
        return status_code, text

    return status_code, f"HTTP {status_code} {reason}".strip()


def _parse_odata_error_detail(response_text: str) -> Optional[str]:
    """Pull the human-readable message out of a standard (non-batch)
    OData v2 error body, shaped like {"error": {"message": {"value":
    "..."}}} -- or sometimes just {"error": "..."}. Returns None if the
    text isn't valid JSON in that shape, so the caller can fall back to
    showing the raw response instead.
    """
    try:
        parsed = json.loads(response_text)
        error = parsed.get("error") if isinstance(parsed, dict) else None
        message = error.get("message") if isinstance(error, dict) else error
        text = message.get("value") if isinstance(message, dict) else message
        return str(text) if text else None
    except (ValueError, AttributeError):
        return None


def _odata_millis(value: Any) -> int:
    """Best-effort numeric sort key for an OData '/Date(millis)/' string.

    Missing or unparseable values sort to the end rather than raising.
    """
    if isinstance(value, str):
        match = _ODATA_DATE_RE.match(value)
        if match:
            return int(match.group(1))
    return -1


def parse_odata_date(value: Any) -> str:
    """Turn an OData v2 '/Date(1680000000000)/' string into a readable date,
    converted to local time (the machine this app is running on) and
    formatted as DD-MM-YYYY.

    Returns the original value unchanged if it isn't in that format.
    """
    if not isinstance(value, str):
        return "" if value is None else str(value)
    match = _ODATA_DATE_RE.match(value)
    if not match:
        return value
    millis = int(match.group(1))
    dt_utc = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    dt_local = dt_utc.astimezone()  # no tz arg -- converts to this machine's local timezone
    return dt_local.strftime("%d-%m-%Y")
