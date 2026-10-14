"""
SAP Integration Suite Browser
==============================

Connect to a Cloud Integration tenant, pick DEV / QA / PROD, pick one or
more packages and then one or more integration flows across them, and
view/edit/deploy every selected iFlow's externalized parameters on one
screen -- including loading values from a multi-sheet, multi-environment
Excel workbook, and downloading the current values back out the same way.

Run with:
    streamlit run app.py
"""

from __future__ import annotations

import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import streamlit as st
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import auth
from sap_cpi_client import (
    SAPCPIClient,
    SAPCPIEnvironment,
    parse_odata_date,
)

IFLOW_ENTITY_SET = "IntegrationDesigntimeArtifacts"

# caps how many iFlows' parameters get fetched and shown at once in the
# multi-iFlow view -- each one is a separate API call, so this bounds how
# long that page can take to load (and how much state it has to track).
# raise it if you need more and don't mind the wait.
MAX_BULK_IFLOWS = 10

ENV_FILE = Path(__file__).resolve().parent / "environments.json"
ENV_COLORS = {"DEV": "blue", "QA": "orange", "PROD": "red"}


# ---------------------------------------------------------------------------
# Environment config
# ---------------------------------------------------------------------------

def load_environments() -> dict[str, SAPCPIEnvironment]:
    """Read named environments (DEV/QA/PROD/...) from environments.json, if present."""
    if not ENV_FILE.exists():
        return {}
    try:
        raw = json.loads(ENV_FILE.read_text())
    except json.JSONDecodeError as exc:
        st.sidebar.error(f"environments.json is not valid JSON: {exc}")
        return {}

    envs: dict[str, SAPCPIEnvironment] = {}
    for name, key in raw.items():
        try:
            envs[name] = SAPCPIEnvironment.from_service_key(name, key)
        except ValueError as exc:
            st.sidebar.warning(str(exc))
    return envs


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------

def try_connect(env: SAPCPIEnvironment, label: str) -> None:
    with st.spinner(f"Connecting to {label}…"):
        client = SAPCPIClient(env)
        try:
            client.test_connection()
        except Exception as exc:  # noqa: BLE001 - never let a bad tenant/key crash the app
            st.error(f"Connection to **{label}** failed: {exc}")
            return

    st.session_state.client = client
    st.session_state.env_name = label
    for key in (
        "packages_raw",
        "artifact_cache",
        "param_cache",
        "step",
        "selected_package_ids",
        "selected_iflows",
        "pkg_selected_ids",
        "iflow_selected_keys",
        "pkg_select_gen",
        "iflow_select_gen",
    ):
        st.session_state.pop(key, None)
    st.rerun()


def disconnect() -> None:
    for key in (
        "client",
        "env_name",
        "packages_raw",
        "artifact_cache",
        "param_cache",
        "step",
        "selected_package_ids",
        "selected_iflows",
        "pkg_selected_ids",
        "iflow_selected_keys",
        "pkg_select_gen",
        "iflow_select_gen",
    ):
        st.session_state.pop(key, None)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_login_screen() -> bool:
    """Gates the rest of the app behind a username/password check. Returns
    True if the person is already authenticated (main() should proceed
    normally); False if a login form (or a setup message) was shown
    instead (main() should stop there and render nothing else this run).

    See auth.py's own module docstring for what this does and doesn't
    protect against -- this is a baseline local deterrent, not a
    substitute for real single sign-on.
    """
    if st.session_state.get("authenticated"):
        return True

    st.subheader("🔐 Log in")

    if not auth.has_any_users():
        st.warning(
            "No users are configured yet for this installation. An administrator needs to "
            "run, directly on this machine: `python manage_users.py add <username>` -- "
            "there's no way to create the first account from this screen itself."
        )
        return False

    with st.form("login_form"):
        username = st.text_input("Username", key="login_username")
        password = st.text_input("Password", type="password", key="login_password")
        submitted = st.form_submit_button("Log in", type="primary")

    if submitted:
        if auth.verify_credentials(username.strip(), password):
            st.session_state["authenticated"] = True
            st.session_state["auth_username"] = username.strip()
            st.rerun()
        else:
            # Deliberately the same message whether the username doesn't
            # exist or the password is wrong for one that does -- auth.py's
            # own verify_credentials() already keeps the two cases
            # indistinguishable in timing; showing a different message
            # here would undo that by leaking it a different way.
            st.error("Invalid username or password.")
    return False


def render_sidebar() -> None:
    with st.sidebar:
        if st.session_state.get("authenticated"):
            st.caption(f"👤 Logged in as **{st.session_state.get('auth_username', '?')}**")
            if st.button("Log out", use_container_width=True, key="logout_button"):
                # Clearing the whole session state (not just the auth
                # flag) on logout, so a later login in the same browser
                # tab/session never inherits a previous user's selected
                # environment, cached packages/iFlows, or in-progress
                # cutover checklist -- a clean slate, same as actually
                # closing and reopening the app.
                st.session_state.clear()
                st.rerun()
            st.divider()

        st.header("Connection")

        client: Optional[SAPCPIClient] = st.session_state.get("client")
        if client is not None:
            env_name = st.session_state.get("env_name", "?")
            st.badge(env_name, color=ENV_COLORS.get(env_name, "green"), icon=":material/dns:")
            st.caption(client.env.url)
            if st.button("Disconnect", use_container_width=True):
                disconnect()
                st.rerun()
            return

        envs = load_environments()

        if envs:
            choice = st.radio("Environment", list(envs.keys()), key="env_choice")
            if st.button("Connect", type="primary", use_container_width=True):
                try_connect(envs[choice], choice)
        elif ENV_FILE.exists():
            st.info(
                "`environments.json` exists but has no usable environments in "
                "it (see any warning above for why). You can still connect "
                "with a one-off key below."
            )
        else:
            st.info(
                f"No `environments.json` found. The app is looking for it at "
                f"exactly:\n\n`{ENV_FILE}`\n\n"
                "Copy `environments.example.json` to `environments.json` in "
                "that exact folder and fill in your DEV/QA/PROD service keys, "
                "or connect with a one-off key below."
            )

        with st.expander(
            "Connect with a one-off service key" if envs else "Connect with a service key",
            expanded=not envs,
        ):
            with st.form("custom_connect_form"):
                uploaded = st.file_uploader("Service key JSON file", type=["json"])
                pasted = st.text_area(
                    "...or paste the JSON",
                    height=120,
                    placeholder='{"clientid": "...", "clientsecret": "...", '
                    '"tokenurl": "...", "url": "..."}',
                )
                custom_name = st.text_input("Label for this connection", value="Custom")
                submitted = st.form_submit_button("Connect", type="primary", use_container_width=True)

            if submitted:
                raw_text = None
                if uploaded is not None:
                    raw_text = uploaded.read().decode("utf-8")
                elif pasted.strip():
                    raw_text = pasted

                if not raw_text:
                    st.error("Upload or paste a service key first.")
                else:
                    try:
                        key = json.loads(raw_text)
                        env = SAPCPIEnvironment.from_service_key(custom_name or "Custom", key)
                    except json.JSONDecodeError as exc:
                        st.error(f"That doesn't look like valid JSON: {exc}")
                    except ValueError as exc:
                        st.error(str(exc))
                    else:
                        try_connect(env, custom_name or "Custom")


# ---------------------------------------------------------------------------
# Main views
# ---------------------------------------------------------------------------

def render_welcome() -> None:
    st.markdown(
        "Connect to a tenant on the left to get started. Once connected:\n\n"
        "1. **Choose package(s)** — check the ones you want\n"
        "2. **Choose integration flow(s)** from across those packages — check "
        f"up to {MAX_BULK_IFLOWS} at once\n"
        "3. **View, edit and save their externalized parameters** — type values "
        "directly, load them from an Excel workbook (including a multi-sheet, "
        "multi-environment one), or download the current values back out the "
        "same way — plus deploy any of them, all behind an explicit confirmation step\n\n"
        "Browsing is read-only. Saving parameter values and deploying genuinely "
        "write to your tenant, immediately — see the README before pointing "
        "this at anything other than DEV."
    )


def _render_iflow_param_editor(client: SAPCPIClient, iflow: dict) -> None:
    """Everything for one iFlow's externalized parameters: load/cache,
    the Excel-import panel, the editable table (with a flag for
    SAP-managed-looking parameters), and the Save button + its result.
    Used both by the single-iFlow drill-down view and, once per selected
    iFlow, by the bulk multi-iFlow view -- identical behavior either way.
    """
    cache_key = f"{iflow['Id']}::{iflow['Version']}"
    cache = st.session_state.setdefault("param_cache", {})
    pending_key = f"param_pending_{cache_key}"
    gen_key = f"param_editor_gen_{cache_key}"

    refresh = st.button("🔄 Refresh", key=f"param_refresh_{cache_key}")

    if cache_key not in cache or refresh:
        with st.spinner("Loading externalized parameters…"):
            try:
                cache[cache_key] = client.list_externalized_parameters(iflow["Id"], iflow["Version"])
            except Exception as exc:  # noqa: BLE001 - surface it, never crash the page
                st.warning(f"Could not load externalized parameters: {exc}")
                cache[cache_key] = ([], None)
        # a fresh fetch invalidates any pending (unsaved) edits -- they
        # were tracked against data that's no longer current
        st.session_state.pop(pending_key, None)
        st.session_state[gen_key] = 0

    params, version_used = cache.get(cache_key, ([], None))
    if version_used and version_used != "active":
        st.caption(
            f"⚠️ This iFlow doesn't appear to have an active (deployed) version yet, "
            f"so this is showing the saved workspace version's configuration instead "
            f"(**{version_used}**)."
        )

    if params:
        if pending_key not in st.session_state:
            st.session_state[pending_key] = {
                p["ParameterKey"]: ("" if p.get("ParameterValue") is None else str(p["ParameterValue"]))
                for p in params
            }
            st.session_state[gen_key] = 0

        with st.expander("📥 Load values from Excel"):
            _render_excel_import(params, pending_key, gen_key, cache_key)

        editor_df = pd.DataFrame(
            [
                {
                    "Parameter": p["ParameterKey"],
                    "DataType": p.get("DataType") or "xsd:string",
                    "Value": st.session_state[pending_key].get(p["ParameterKey"], ""),
                    "Flag": (
                        "⚙️ SAP-defined dropdown value" if _is_dropdown_constrained_parameter(p["ParameterKey"])
                        else "⚠️ WARNING: SAP-managed" if _is_sap_restricted_parameter(p["ParameterKey"])
                        else ""
                    ),
                    "Description": p.get("Description"),
                }
                for p in params
            ]
        )
        st.caption(f"{len(editor_df)} parameter(s) — edit **Value** directly below, then Save.")
        if (editor_df["Flag"].str.startswith("⚙️")).any():
            st.caption(
                "⚙️ Rows flagged `SAP-defined dropdown value` correspond to a field that's a "
                "dropdown in the actual SAP CPI adapter UI (e.g. Proxy Type, Authentication) -- "
                "only specific, SAP-provided values are valid there, not arbitrary text. You can "
                "still type into these cells, but **the change is never actually saved** -- a "
                "warning appears below the table if you do, and Save silently skips it regardless "
                "of what's typed here, since this app's free-text table has no way to offer only "
                "the real, valid options the way SAP's own dropdown does. Change these from the "
                "real SAP CPI Configure screen instead."
            )
        if (editor_df["Flag"].str.startswith("⚠️")).any():
            st.caption(
                "ℹ️ Rows flagged `⚠️ WARNING: SAP-managed` are `SAP_ProfileId` specifically -- "
                "the one SAP_-prefixed externalized parameter confirmed, on a real tenant, to "
                "reject an edit with an HTTP 400. It's still editable here, but an update to it "
                "may come back rejected -- that's a tenant-side restriction on that one entry, "
                "not an error in this app. Every other SAP_-prefixed parameter is an ordinary, "
                "fully editable configuration value despite the naming convention, and isn't "
                "flagged."
            )
        # NOTE: deliberately no search/filter box on this table. Filtering
        # would change which parameter sits at which row index, but
        # st.data_editor tracks edits BY row index -- combine the two and
        # a filtered view could silently apply an edit to the wrong
        # parameter. Safe > convenient for something that writes live.
        edited = st.data_editor(
            editor_df,
            hide_index=True,
            width="stretch",
            disabled=["Parameter", "DataType", "Flag", "Description"],
            column_config={"Value": st.column_config.TextColumn("Value")},
            key=f"param_editor_{cache_key}_{st.session_state[gen_key]}",
        )
        # keep the pending map in sync with whatever's currently shown, so
        # edits survive into the next rerun (e.g. before a later Excel
        # import, or just some other interaction elsewhere on the page)
        for _, row in edited.iterrows():
            st.session_state[pending_key][row["Parameter"]] = "" if pd.isna(row["Value"]) else str(row["Value"])

        changed = _diff_parameter_edits(params, edited)
        dropdown_attempts = _find_dropdown_constrained_attempts(params, edited)
        if dropdown_attempts:
            names = ", ".join(f"`{c['Parameter']}`" for c in dropdown_attempts)
            st.warning(
                f"⚙️ {names} {'is a' if len(dropdown_attempts) == 1 else 'are'} dropdown-only "
                f"field{'s' if len(dropdown_attempts) != 1 else ''} in the actual SAP CPI adapter UI "
                f"-- only specific, SAP-provided values are valid there (e.g. Proxy Type only accepts "
                f"Internet/On-Premise, Authentication only accepts None/Basic/Client Certificate/"
                f"OAuth2 Client Credentials/etc.), not arbitrary text. **This change will NOT be "
                f"saved** — edit this field from the real SAP CPI Configure screen instead, where "
                f"only valid options can be selected."
            )
        if st.button(
            f"💾 Save {len(changed)} change(s)" if changed else "💾 Save changes",
            type="primary",
            disabled=not changed,
            key=f"save_btn_{cache_key}",
        ):
            _confirm_update_dialog(
                client=client,
                iflow=iflow,
                version_used=version_used or iflow["Version"],
                changes=changed,
                cache_key=cache_key,
            )
        if not changed:
            st.caption("Change a value above (or load from Excel) to enable Save.")
    else:
        st.info("This integration flow has no externalized parameters.")

    # results left behind by a just-closed dialog, scoped to THIS iFlow --
    # the dialog records which cache_key it was for, so in the bulk view
    # (several of these sections on one page) a result only ever shows up
    # under the iFlow it actually belongs to.
    if st.session_state.get("_param_update_result_key") == cache_key:
        for key, ok, msg in st.session_state.pop("_param_update_result", []):
            if ok:
                st.success(f"`{key}`: {msg}")
            elif _is_expected_system_param_rejection(key, msg):
                st.warning(
                    f"`{key}`: not updated — this looks like a SAP-managed configuration "
                    f"entry (flagged above) rather than something broke. Tenant said: {msg}"
                )
            else:
                st.error(f"`{key}`: {msg}")
        st.session_state.pop("_param_update_result_key", None)


def _render_iflow_deploy_section(client: SAPCPIClient, iflow: dict) -> None:
    """Deployment is intentionally NOT gated on having parameters --
    deploying and configuring are independent actions (you might want to
    push a content change to an iFlow with no externalized parameters at
    all), so this always renders alongside _render_iflow_param_editor."""
    cache_key = f"{iflow['Id']}::{iflow['Version']}"
    st.divider()
    st.markdown("##### 🚀 Deployment")
    deploy_version = iflow["Version"]
    st.caption(
        f"Deploys **{iflow['Name']}** version **{deploy_version}**. Parameter value changes "
        "above take effect immediately without a deploy — only deploy if you specifically "
        "need to push new design-time content live."
    )
    if st.button("Deploy…", key=f"deploy_btn_{cache_key}"):
        _confirm_deploy_dialog(client=client, iflow=iflow, version=deploy_version, cache_key=cache_key)

    if st.session_state.get("_deploy_result_key") == cache_key:
        kind, msg = st.session_state.pop("_deploy_result", (None, None))
        if kind:
            (st.success if kind == "success" else st.error)(msg)
        st.session_state.pop("_deploy_result_key", None)


def _diff_parameter_edits(original_params: list[dict], edited_df: pd.DataFrame) -> list[dict]:
    """Compare the tenant-fetched parameter list against the edited
    dataframe currently shown in the UI, matched by Parameter (the
    ParameterKey), and return only the ones whose Value actually
    differs. This decides what gets written to the tenant, so — like
    _apply_parameter_update below — it's kept free of any Streamlit
    dependency and tested directly rather than only through the UI."""
    original_by_key = {p["ParameterKey"]: p for p in original_params}
    changed = []
    for _, row in edited_df.iterrows():
        key = row["Parameter"]
        orig = original_by_key.get(key)
        if orig is None:
            continue
        if _is_dropdown_constrained_parameter(key):
            # Never write to one of these, regardless of how its Value
            # cell ended up changed (hand-typed, or matched from an
            # uploaded Excel file). The real SAP CPI UI only lets you
            # pick from a fixed dropdown here -- this app's free-text
            # table has no equivalent restriction, so a typo or a wrong
            # Excel value could otherwise reach the tenant as a value
            # that was never actually selectable in the first place. The
            # on-screen warning (shown wherever the value is edited)
            # explains this; this is what actually enforces it.
            continue
        orig_value = orig.get("ParameterValue")
        orig_str = "" if orig_value is None else str(orig_value)
        new_str = "" if pd.isna(row["Value"]) else str(row["Value"])
        if new_str != orig_str:
            changed.append(
                {"Parameter": key, "DataType": orig.get("DataType") or "xsd:string", "Old": orig_str, "New": new_str}
            )
    return changed


def _find_dropdown_constrained_attempts(original_params: list[dict], edited_df: pd.DataFrame) -> list[dict]:
    """Like _diff_parameter_edits, but the OPPOSITE selection: only the
    dropdown-constrained parameters (see _is_dropdown_constrained_parameter)
    whose Value cell was changed -- the ones _diff_parameter_edits
    deliberately excludes from what gets saved. Used to show a clear
    warning for exactly these attempted changes, separately from the
    save-worthy diff, since they'd otherwise disappear silently (the
    person edited the cell, nothing happens on Save, no indication why).
    Kept as its own plain function for the same reason as
    _diff_parameter_edits: directly testable, no Streamlit dependency.
    """
    original_by_key = {p["ParameterKey"]: p for p in original_params}
    attempts = []
    for _, row in edited_df.iterrows():
        key = row["Parameter"]
        if not _is_dropdown_constrained_parameter(key):
            continue
        orig = original_by_key.get(key)
        if orig is None:
            continue
        orig_value = orig.get("ParameterValue")
        orig_str = "" if orig_value is None else str(orig_value)
        new_str = "" if pd.isna(row["Value"]) else str(row["Value"])
        if new_str != orig_str:
            attempts.append({"Parameter": key, "Old": orig_str, "New": new_str})
    return attempts


def _excel_cell_to_str(value: Any) -> Optional[str]:
    """One Excel cell -> a clean string, or None for 'no value provided'
    (blank/NaN) -- so a blank lookup cell never overwrites a real
    current value with an empty string. A whole-number float (pandas'
    usual reading of an integer-looking cell) is rendered without the
    trailing '.0'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value).strip()
    return text or None


def _match_excel_values(
    excel_df: pd.DataFrame, name_col: str, value_col: str, param_keys: list[str]
) -> dict[str, str]:
    """Match param_keys against name_col (case-insensitive, trimmed) and
    return {ParameterKey: value} for matches that have a non-blank
    value. If a name appears on more than one row, the last row wins."""
    lookup: dict[str, str] = {}
    for _, row in excel_df.iterrows():
        name = row.get(name_col)
        if name is None or (isinstance(name, float) and pd.isna(name)):
            continue
        value_str = _excel_cell_to_str(row.get(value_col))
        if value_str is None:
            continue
        lookup[str(name).strip().lower()] = value_str

    return {key: lookup[key.strip().lower()] for key in param_keys if key.strip().lower() in lookup}


def _render_excel_import(params: list[dict], pending_key: str, gen_key: str, cache_key: str) -> None:
    uploaded = st.file_uploader("Excel file", type=["xlsx", "xls"], key=f"excel_upload_{cache_key}")
    if uploaded is None:
        st.caption(
            "Upload a spreadsheet with a column of parameter names and a column of "
            "values — matched parameters get pre-filled into the table below."
        )
        return

    try:
        excel_file = pd.ExcelFile(uploaded)
    except Exception as exc:  # noqa: BLE001 - e.g. not a real Excel file
        st.error(f"Could not read this file as Excel: {exc}")
        return

    sheet = (
        excel_file.sheet_names[0]
        if len(excel_file.sheet_names) == 1
        else st.selectbox("Sheet", excel_file.sheet_names, key=f"excel_sheet_{cache_key}")
    )
    try:
        excel_df = excel_file.parse(sheet)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read sheet '{sheet}': {exc}")
        return

    if excel_df.empty or len(excel_df.columns) < 2:
        st.warning("This sheet doesn't have at least two columns to pick a name and a value from.")
        return

    columns = [str(c) for c in excel_df.columns]
    name_guess = next((c for c in columns if any(w in c.lower() for w in ("param", "name", "key"))), columns[0])
    value_guess = next((c for c in columns if "value" in c.lower()), columns[-1])

    col_name, col_value = st.columns(2)
    with col_name:
        name_col = st.selectbox(
            "Column with parameter name", columns, index=columns.index(name_guess), key=f"excel_name_col_{cache_key}"
        )
    with col_value:
        value_col = st.selectbox(
            "Column with value", columns, index=columns.index(value_guess), key=f"excel_value_col_{cache_key}"
        )

    st.caption("Preview:")
    st.dataframe(excel_df.head(5).astype(str), hide_index=True, width="stretch")

    # Dropdown-constrained parameters are never matched/staged from an
    # uploaded file -- Save would silently exclude them anyway, and
    # showing a staged value here that then never actually saves would
    # be more confusing than not offering it in the first place.
    matches = _match_excel_values(
        excel_df, name_col, value_col,
        [p["ParameterKey"] for p in params if not _is_dropdown_constrained_parameter(p["ParameterKey"])],
    )
    if not matches:
        st.info("No parameter names in this sheet matched this iFlow's parameters.")
        return

    st.success(f"Matched {len(matches)} parameter(s): {', '.join(sorted(matches))}")
    if st.button(f"Apply {len(matches)} value(s) to the table below", key=f"excel_apply_{cache_key}"):
        st.session_state[pending_key].update(matches)
        st.session_state[gen_key] = st.session_state.get(gen_key, 0) + 1
        st.rerun()


# Names of externalized parameters that correspond to a dropdown-only
# field in the actual SAP CPI adapter UI -- e.g. "Proxy Type" (Internet /
# On-Premise) or "Authentication" (None / Basic / Client Certificate /
# OAuth2 Client Credentials / OAuth2 SAML Bearer Assertion / Principal
# Propagation). Confirmed against SAP's own documentation and community
# material for these specific fields -- not a guess the way a pure
# keyword match would be -- but the list itself is still necessarily
# incomplete: this API doesn't expose which externalized parameters are
# dropdown-constrained or what their allowed values are, only the
# parameter's name, current value, and a generic DataType, so there's no
# way to enumerate every such field across every adapter type with
# certainty. Matching is case-insensitive, normalized (spaces/underscores/
# hyphens treated the same), substring-based. Extend this list if a real
# tenant uses a dropdown field under a name not covered here.
KNOWN_DROPDOWN_PARAMETER_NAMES = (
    "proxy type",
    "authentication",
    "connection type",
    "transport protocol",
    "message protocol",
    "processing type",
    "processing mode",
    "adapter type",
)


def _normalize_parameter_name(parameter_key: str) -> str:
    """Lowercase, with underscores/hyphens treated as spaces and repeated
    whitespace collapsed -- so "Proxy_Type", "proxy-type" and "Proxy  Type"
    all normalize to the same "proxy type", for matching against
    KNOWN_DROPDOWN_PARAMETER_NAMES regardless of the exact separator or
    casing a given tenant happens to use."""
    return re.sub(r"[\s_-]+", " ", parameter_key).strip().lower()


def _is_dropdown_constrained_parameter(parameter_key: str) -> bool:
    """True if parameter_key's name matches a known dropdown-only SAP CPI
    adapter field (see KNOWN_DROPDOWN_PARAMETER_NAMES above for the exact
    list, what it's based on, and its limits)."""
    normalized = _normalize_parameter_name(parameter_key)
    return any(name in normalized for name in KNOWN_DROPDOWN_PARAMETER_NAMES)


# SAP_-prefixed externalized parameters that are genuinely known to
# reject writes on a real tenant -- narrower than "any SAP_-prefixed
# key". Most SAP_-prefixed externalized parameters are ordinary,
# user-editable configuration values despite the naming convention;
# SAP_ProfileId is the one confirmed (via a real HTTP 400 on an edit
# attempt) to actually be restricted. Matched case-insensitively.
# Extend this tuple if another specific SAP_-prefixed field is confirmed
# to behave the same way.
SAP_RESTRICTED_PARAMETER_NAMES = ("sap_profileid",)


def _is_sap_restricted_parameter(parameter_key: str) -> bool:
    """True only for the specific SAP_-prefixed parameter(s) confirmed to
    reject writes (see SAP_RESTRICTED_PARAMETER_NAMES above),
    case-insensitive. Every other SAP_-prefixed externalized parameter is
    treated as a normal, fully editable one -- the prefix alone is not
    treated as a signal of restriction."""
    return parameter_key.strip().lower() in SAP_RESTRICTED_PARAMETER_NAMES


def _is_expected_system_param_rejection(parameter_key: str, message: str) -> bool:
    """True for the known, expected failure pattern -- SAP_ProfileId (the
    one SAP-managed parameter confirmed to reject writes, see
    _is_sap_restricted_parameter) coming back with HTTP 400 -- as opposed
    to a genuine, unexpected failure that still deserves a harder error
    treatment (a real auth problem, a network error, or a 400 on a
    parameter that ISN'T known to be restricted, which is actually
    surprising and worth flagging as such).
    """
    return _is_sap_restricted_parameter(parameter_key) and "400" in message


def _apply_parameter_update(
    client: SAPCPIClient, iflow: dict, version_used: str, parameter_key: str, new_value: str, data_type: str
) -> tuple[bool, str]:
    """Performs the actual tenant write for a parameter update. Returns
    (ok, message). Deliberately has no Streamlit dependency, unlike the
    dialog that calls it, so the exact logic a confirm-click runs can be
    unit-tested directly without going through Streamlit's dialog
    machinery (which AppTest can't drive)."""
    try:
        client.update_parameter(iflow["Id"], version_used, parameter_key, new_value, data_type)
    except Exception as exc:  # noqa: BLE001 - report it, let the caller decide how to show it
        return False, f"Update failed: {exc}"
    return True, f"Updated `{parameter_key}` on this environment."


def _apply_parameter_updates(
    client: SAPCPIClient, iflow: dict, version_used: str, changes: list[dict]
) -> list[tuple[str, bool, str]]:
    """Applies every change in order, continuing past an individual
    failure so one bad parameter doesn't block the rest. Returns
    (parameter_key, ok, message) per change — same no-Streamlit-dependency
    reasoning as _apply_parameter_update, which this calls for each row."""
    results = []
    for change in changes:
        ok, msg = _apply_parameter_update(
            client, iflow, version_used, change["Parameter"], change["New"], change["DataType"]
        )
        results.append((change["Parameter"], ok, msg))
    return results


def _apply_deploy(client: SAPCPIClient, iflow: dict, version: str) -> tuple[bool, str]:
    """Performs the actual deploy request. Returns (ok, message). Same
    no-Streamlit-dependency reasoning as _apply_parameter_update above."""
    try:
        client.deploy_iflow(iflow["Id"], version)
    except Exception as exc:  # noqa: BLE001
        return False, f"Deploy failed: {exc}"
    return True, (
        f"Deployment of version **{version}** requested. It runs asynchronously — "
        "check Web UI's Monitor tab shortly to confirm it completed."
    )


@st.dialog("Confirm parameter update")
def _confirm_update_dialog(
    client: SAPCPIClient,
    iflow: dict,
    version_used: str,
    changes: list[dict],
    cache_key: str,
) -> None:
    env_name = st.session_state.get("env_name", "")
    st.badge(env_name, color=ENV_COLORS.get(env_name, "green"), icon=":material/dns:")
    st.write(f"**iFlow:** {iflow['Name']}  ·  version **{version_used}**")
    st.write(f"About to update **{len(changes)}** parameter(s):")
    for c in changes:
        st.markdown(f"- `{c['Parameter']}`:  `{c['Old'] or '(empty)'}`  →  `{c['New'] or '(empty)'}`")

    system_managed = [c["Parameter"] for c in changes if _is_sap_restricted_parameter(c["Parameter"])]
    if system_managed:
        st.warning(
            f"`{'`, `'.join(system_managed)}` {'is' if len(system_managed) == 1 else 'are'} "
            "known to reject writes on some tenants — this update may come back rejected (HTTP "
            "400) regardless of confirming here. Safe to try; just not guaranteed to apply."
        )

    proceed = True
    if env_name.upper() == "PROD":
        st.error("⚠️ You're connected to **PROD**. This changes live configuration values immediately.")
        proceed = st.checkbox("I understand this updates PROD right now")

    col_confirm, col_cancel = st.columns(2)
    with col_confirm:
        if st.button("Confirm update", type="primary", use_container_width=True, disabled=not proceed):
            with st.spinner(f"Updating {len(changes)} parameter(s)…"):
                results = _apply_parameter_updates(client, iflow, version_used, changes)
            st.session_state["_param_update_result"] = results
            st.session_state["_param_update_result_key"] = cache_key
            if any(ok for _, ok, _ in results):
                st.session_state.get("param_cache", {}).pop(cache_key, None)
                st.session_state.pop(f"param_pending_{cache_key}", None)
            st.rerun()
    with col_cancel:
        if st.button("Cancel", use_container_width=True, key="cancel_update"):
            st.rerun()


@st.dialog("Confirm deployment")
def _confirm_deploy_dialog(client: SAPCPIClient, iflow: dict, version: str, cache_key: str) -> None:
    env_name = st.session_state.get("env_name", "")
    st.badge(env_name, color=ENV_COLORS.get(env_name, "green"), icon=":material/dns:")
    st.write(f"**iFlow:** {iflow['Name']}  (`{iflow['Id']}`)")
    st.write(f"**Version to deploy:** {version}")
    st.caption(
        "Deployment is asynchronous — this starts it but doesn't wait for it to finish. "
        "Check the Monitor tab in Web UI in a minute to confirm it completed."
    )

    proceed = True
    if env_name.upper() == "PROD":
        st.error("⚠️ You're connected to **PROD**. This triggers a live deployment right now.")
        proceed = st.checkbox("I understand this deploys to PROD right now")

    col_confirm, col_cancel = st.columns(2)
    with col_confirm:
        if st.button("Confirm deploy", type="primary", use_container_width=True, disabled=not proceed):
            with st.spinner("Requesting deployment…"):
                ok, msg = _apply_deploy(client, iflow, version)
            st.session_state["_deploy_result"] = ("success" if ok else "error", msg)
            st.session_state["_deploy_result_key"] = cache_key
            st.rerun()
    with col_cancel:
        if st.button("Cancel", use_container_width=True, key="cancel_deploy"):
            st.rerun()


# ---------------------------------------------------------------------------
# Package / iFlow selection and the parameters view
# ---------------------------------------------------------------------------

def render_package_picker(client: SAPCPIClient) -> None:
    st.subheader("Step 1 of 3 — Choose Package(s)")

    refresh = st.button("🔄 Refresh", key="pkg_refresh")
    if "packages_raw" not in st.session_state or refresh:
        with st.spinner("Loading packages…"):
            try:
                st.session_state.packages_raw = client.list_packages()
            except Exception as exc:  # noqa: BLE001 - surface it, never crash the page
                st.error(f"Could not load packages: {exc}")
                st.session_state.packages_raw = []
        # a refresh can change which packages exist at all -- clear
        # tracked selections rather than risk carrying forward an Id that
        # no longer means the same thing (or doesn't exist anymore)
        st.session_state.pop("pkg_selected_ids", None)

    packages = st.session_state.get("packages_raw", [])
    if not packages:
        st.info("No packages found on this tenant.")
        return

    # Selections are tracked by package Id in this set, NOT by row
    # position -- that's what makes filtering safe below. The table
    # itself is rebuilt from this set on every render (so a checked
    # package shows checked regardless of the current filter), and every
    # edit is synced back into it immediately after rendering.
    selected_ids_set: set[str] = st.session_state.setdefault("pkg_selected_ids", set())
    before_selection = set(selected_ids_set)  # snapshot, to detect deselections for cascading below

    search = st.text_input(
        "Filter", key="pkg_filter", placeholder="Filter by name or ID…", label_visibility="collapsed"
    )
    if search:
        term = search.strip().lower()
        filtered = [p for p in packages if term in p["Name"].lower() or term in p["Id"].lower()]
    else:
        filtered = packages

    # Select all / Deselect all apply to whatever the current filter is
    # showing -- not necessarily every package on the tenant -- same as
    # ticking every visible checkbox by hand would.
    col_all, col_none, _col_spacer = st.columns([1, 1, 3])
    with col_all:
        if st.button(f"Select all ({len(filtered)})", key="pkg_select_all", use_container_width=True):
            selected_ids_set.update(p["Id"] for p in filtered)
            st.session_state["pkg_select_gen"] = st.session_state.get("pkg_select_gen", 0) + 1
            # deliberately NOT calling st.rerun() here: execution continues
            # straight into the rest of this function below (which builds
            # the table from the just-updated selected_ids_set and the
            # just-bumped generation key), so it renders correctly without
            # one -- and, importantly, so the cascading-deselection check
            # further down still runs in THIS pass rather than being
            # skipped by an early return.
    with col_none:
        if st.button("Deselect all", key="pkg_deselect_all", use_container_width=True):
            selected_ids_set.difference_update(p["Id"] for p in filtered)
            st.session_state["pkg_select_gen"] = st.session_state.get("pkg_select_gen", 0) + 1

    df = pd.DataFrame(
        [
            {
                "Select": p["Id"] in selected_ids_set,
                "Name": p["Name"],
                "Id": p["Id"],
                "Version": p.get("Version"),
                "Vendor": p.get("Vendor"),
                "Modified": parse_odata_date(p.get("ModifiedDate")),
            }
            for p in filtered
        ]
    )
    st.caption(f"{len(df)} of {len(packages)} packages shown. Select package(s) to continue.")
    # The editor's key changes whenever the filter text changes OR a
    # Select all/Deselect all button fires (via the generation counter),
    # so either one always starts from a FRESH widget built from the
    # (Id-tracked, filter-independent) selection above -- never a stale,
    # row-position-based delta left over from a different filter or a
    # bulk select/deselect action.
    gen = st.session_state.get("pkg_select_gen", 0)
    edited = st.data_editor(
        df,
        hide_index=True,
        width="stretch",
        disabled=["Name", "Id", "Version", "Vendor", "Modified"],
        column_config={"Select": st.column_config.CheckboxColumn("Select", default=False)},
        # Id/Vendor/Modified are hidden by default (not removed -- the
        # person can still re-show them via the column-visibility toggle
        # in the table's own toolbar); Select/Name/Version are the ones
        # worth seeing at a glance.
        column_order=["Select", "Name", "Version"],
        key=f"pkg_select_editor_{search}_{gen}",
    )
    for _, row in edited.iterrows():
        if row["Select"]:
            selected_ids_set.add(row["Id"])
        else:
            selected_ids_set.discard(row["Id"])

    # Cascading deselection: any iFlow already picked from a package
    # that's no longer checked (whether unchecked individually or via
    # Deselect all) gets dropped too, so the iFlow picker and parameters
    # view never silently carry forward an iFlow whose package you just
    # unchecked here.
    newly_deselected = before_selection - selected_ids_set
    if newly_deselected:
        iflow_keys = st.session_state.get("iflow_selected_keys")
        if iflow_keys:
            to_drop = {k for k in iflow_keys if k.split("::", 1)[0] in newly_deselected}
            if to_drop:
                st.session_state["iflow_selected_keys"] = iflow_keys - to_drop
                st.info(
                    f"Also removed {len(to_drop)} previously-picked integration flow(s) that "
                    f"belonged to the package(s) you just unchecked."
                )

    selected_ids = sorted(selected_ids_set)
    if st.button(
        f"Next: choose iFlows ({len(selected_ids)} package(s) selected) →" if selected_ids else "Next: choose iFlows →",
        type="primary",
        disabled=not selected_ids,
    ):
        st.session_state.selected_package_ids = selected_ids
        st.session_state.step = "pick_iflows"
        st.rerun()


def render_iflow_picker(client: SAPCPIClient) -> None:
    st.subheader("Step 2 of 3 — Choose Integration Flow(s)")
    col_back, col_refresh = st.columns([1, 1])
    with col_back:
        if st.button("← Back to package selection", key="back_to_pkg_picker"):
            st.session_state.step = "pick_packages"
            st.rerun()
    with col_refresh:
        refresh = st.button("🔄 Refresh", key="iflow_refresh")

    package_ids = st.session_state.get("selected_package_ids", [])
    if not package_ids:
        st.warning("No packages were selected.")
        return
    pkg_meta = {p["Id"]: p for p in st.session_state.get("packages_raw", [])}

    all_iflows: list[dict] = []
    artifact_cache = st.session_state.setdefault("artifact_cache", {})
    with st.spinner("Loading integration flows from the selected package(s)…"):
        for pkg_id in package_ids:
            pkg_cache = artifact_cache.setdefault(pkg_id, {})
            if IFLOW_ENTITY_SET not in pkg_cache or refresh:
                try:
                    pkg_cache[IFLOW_ENTITY_SET] = client.list_artifacts(pkg_id, IFLOW_ENTITY_SET)
                except Exception as exc:  # noqa: BLE001
                    st.warning(f"Could not load iFlows for package `{pkg_id}`: {exc}")
                    pkg_cache[IFLOW_ENTITY_SET] = []
            pkg_name = pkg_meta.get(pkg_id, {}).get("Name", pkg_id)
            for iflow in pkg_cache[IFLOW_ENTITY_SET]:
                all_iflows.append({**iflow, "PackageId": pkg_id, "_package_name": pkg_name})

    if refresh:
        # the set of iFlows (and which row is which) can genuinely change
        # on refresh -- clear tracked selections rather than carry forward
        # a key that may no longer mean the same thing
        st.session_state.pop("iflow_selected_keys", None)

    if not all_iflows:
        st.info("No integration flows found in the selected package(s).")
        return

    # Same Id-tracked-selection pattern as the package picker, with a
    # composite key (package :: id :: version) since the same iFlow name
    # could exist in more than one package.
    selected_keys_set: set[str] = st.session_state.setdefault("iflow_selected_keys", set())

    search = st.text_input(
        "Filter", key="iflow_filter", placeholder="Filter by name, ID or package…", label_visibility="collapsed"
    )
    if search:
        term = search.strip().lower()
        filtered = [
            i for i in all_iflows
            if term in i["Name"].lower() or term in i["Id"].lower() or term in i["_package_name"].lower()
        ]
    else:
        filtered = all_iflows

    # Select all / Deselect all apply to whatever the current filter is
    # showing, same reasoning as the package picker. Select all can still
    # push the total over MAX_BULK_IFLOWS -- that's fine, the existing
    # over-cap error and disabled button below handle it the same way a
    # manual over-selection would.
    col_all, col_none, _col_spacer = st.columns([1, 1, 3])
    with col_all:
        if st.button(f"Select all ({len(filtered)})", key="iflow_select_all", use_container_width=True):
            selected_keys_set.update(f"{i['PackageId']}::{i['Id']}::{i['Version']}" for i in filtered)
            st.session_state["iflow_select_gen"] = st.session_state.get("iflow_select_gen", 0) + 1
            # no st.rerun() here -- same reasoning as the package picker's
            # Select all/Deselect all above: execution continues straight
            # into the rest of this function, which already renders
            # correctly from the just-updated state and just-bumped key.
    with col_none:
        if st.button("Deselect all", key="iflow_deselect_all", use_container_width=True):
            selected_keys_set.difference_update(f"{i['PackageId']}::{i['Id']}::{i['Version']}" for i in filtered)
            st.session_state["iflow_select_gen"] = st.session_state.get("iflow_select_gen", 0) + 1

    df = pd.DataFrame(
        [
            {
                "Select": f"{i['PackageId']}::{i['Id']}::{i['Version']}" in selected_keys_set,
                "Name": i["Name"],
                "Id": i["Id"],
                "Package": i["_package_name"],
                "Version": i.get("Version"),
                "Description": i.get("Description"),
                "_key": f"{i['PackageId']}::{i['Id']}::{i['Version']}",
            }
            for i in filtered
        ]
    )
    st.caption(
        f"{len(df)} of {len(all_iflows)} integration flow(s) shown, across {len(package_ids)} "
        f"package(s). Choose up to {MAX_BULK_IFLOWS} at a time."
    )
    gen = st.session_state.get("iflow_select_gen", 0)
    edited = st.data_editor(
        df,
        hide_index=True,
        width="stretch",
        disabled=["Name", "Id", "Package", "Version", "Description", "_key"],
        column_config={
            "Select": st.column_config.CheckboxColumn("Select", default=False),
            "_key": None,  # tracked internally, not shown
        },
        # Id/Description are hidden by default (not removed -- toggleable
        # via the table's own column-visibility control); Select/Name/
        # Package/Version are the ones worth seeing at a glance.
        column_order=["Select", "Name", "Package", "Version"],
        key=f"iflow_select_editor_{search}_{gen}",
    )
    for _, row in edited.iterrows():
        if row["Select"]:
            selected_keys_set.add(row["_key"])
        else:
            selected_keys_set.discard(row["_key"])

    selected_count = len(selected_keys_set)
    over_cap = selected_count > MAX_BULK_IFLOWS

    if over_cap:
        st.error(
            f"{selected_count} selected, but only {MAX_BULK_IFLOWS} can be worked with at once. "
            f"Uncheck {selected_count - MAX_BULK_IFLOWS} to continue."
        )

    if st.button(
        f"View parameters for {selected_count} iFlow(s) →" if selected_count else "View parameters →",
        type="primary",
        disabled=not selected_count or over_cap,
    ):
        by_key = {f"{i['PackageId']}::{i['Id']}::{i['Version']}": i for i in all_iflows}
        st.session_state.selected_iflows = [by_key[k] for k in selected_keys_set if k in by_key]
        st.session_state.step = "params"
        st.rerun()


def render_parameters_view(client: SAPCPIClient) -> None:
    st.subheader("Step 3 of 3 — Externalized Parameters")
    selected_iflows = st.session_state.get("selected_iflows", [])

    if st.button("← Change iFlow selection", key="back_to_iflow_picker"):
        st.session_state.step = "pick_iflows"
        st.rerun()

    if not selected_iflows:
        st.info("No iFlows selected.")
        return

    st.caption(f"Showing {len(selected_iflows)} selected integration flow(s).")

    # Pre-load every selected iFlow's parameters up front, in one spinner,
    # BEFORE the Excel-import/download sections below: both match/read
    # against real parameter keys, which only exist once this has run.
    # Without this, those sections would see an empty cache on the very
    # first render (Python runs top-to-bottom; each iFlow's own section
    # further down the page hasn't populated its cache yet at that point).
    cache = st.session_state.setdefault("param_cache", {})
    to_fetch = [i for i in selected_iflows if f"{i['Id']}::{i['Version']}" not in cache]
    if to_fetch:
        with st.spinner(f"Loading externalized parameters for {len(to_fetch)} iFlow(s)…"):
            for iflow in to_fetch:
                cache_key = f"{iflow['Id']}::{iflow['Version']}"
                try:
                    cache[cache_key] = client.list_externalized_parameters(iflow["Id"], iflow["Version"])
                except Exception as exc:  # noqa: BLE001
                    cache[cache_key] = ([], None)

    env_name = st.session_state.get("env_name", "")
    col_import, col_export = st.columns(2)
    with col_import:
        with st.expander("📥 Load values from Excel (one tab per iFlow, multiple environment columns)"):
            _render_bulk_excel_import(selected_iflows)
    with col_export:
        st.download_button(
            "📤 Download current parameters as Excel",
            data=_build_parameters_excel(selected_iflows, cache, env_name),
            file_name="iflow_parameters.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="download_params_excel",
            help=f"One tab per iFlow, named by its technical ID (shortened if needed, since Excel "
            f"caps tab names at 31 characters), with columns Parameter Name / "
            f"{' / '.join(EXCEL_ENV_COLUMNS)}. Current values go in the '{env_name}' column if it "
            f"matches one of those; the others are left blank. SAP-managed parameters are left out, "
            f"since the tenant won't accept a write to them. An 'Index' tab lists every iFlow's "
            f"full ID alongside its (possibly shortened) tab name -- don't rename tabs or edit that "
            f"sheet, since re-uploading this file uses it to match tabs back to iFlows correctly.",
        )
        if env_name.strip().upper() not in EXCEL_ENV_COLUMNS:
            st.caption(
                f"ℹ️ Connected environment **{env_name or '(none)'}** doesn't match "
                f"{'/'.join(EXCEL_ENV_COLUMNS)}, so all three value columns will be blank in "
                f"the download — fill them in manually."
            )

    for iflow in list(selected_iflows):
        cache_key = f"{iflow['Id']}::{iflow['Version']}"
        label = f"⚙️ **{iflow['Name']}**"
        if iflow.get("_package_name"):
            label += f"  ·  {iflow['_package_name']}"
        with st.expander(label, expanded=True):
            if st.button("✕ Remove this iFlow from the list", key=f"remove_iflow_{cache_key}"):
                st.session_state.selected_iflows = [
                    i for i in st.session_state.get("selected_iflows", [])
                    if f"{i['Id']}::{i['Version']}" != cache_key
                ]
                st.rerun()
            st.caption(f"iFlow ID: `{iflow['Id']}`  ·  Version {iflow['Version']}")

            # Point 4: this iFlow's own matched sheet from the last bulk
            # Excel upload, if any -- shown here, in its own section,
            # instead of a single generic "first sheet only" preview
            # elsewhere that only ever reflected one iFlow regardless of
            # how many were actually matched.
            preview = st.session_state.get(f"_bulk_sheet_preview_{cache_key}")
            if preview is not None:
                sheet_df, name_col, env_col = preview
                other_env_cols = [c for c in sheet_df.columns if c != name_col and c != env_col]
                st.caption(f"📄 Matched values from the uploaded file for **{iflow['Name']}**:")
                st.dataframe(
                    sheet_df.astype(str),
                    hide_index=True,
                    width="stretch",
                    # Parameter-name column plus the currently-selected
                    # environment column are shown by default; any other
                    # environment columns in the same sheet are hidden
                    # (not removed -- still toggleable via the table's own
                    # column-visibility control), since only one column's
                    # values are actually being used right now.
                    column_order=[name_col, env_col],
                    key=f"bulk_sheet_preview_table_{cache_key}",
                )
                if other_env_cols:
                    st.caption(f"(Other column(s) in this sheet — {', '.join(other_env_cols)} — are hidden by "
                               f"default; use the table's column-visibility control to show them.)")

            _render_iflow_param_editor(client, iflow)
            _render_iflow_deploy_section(client, iflow)


def _safe_excel_sheet_name(raw_name: str, used_names: set[str]) -> str:
    """Excel sheet names: max 31 characters, can't contain : \\ / ? * [ ],
    and must be unique within the workbook -- sanitizes and disambiguates
    a candidate name (an iFlow's technical Id) accordingly."""
    cleaned = re.sub(r"[:\\/?*\[\]]", "_", raw_name).strip() or "Sheet"
    cleaned = cleaned[:31]
    candidate = cleaned
    used_lower = {u.lower() for u in used_names}
    suffix = 1
    while candidate.lower() in used_lower:
        suffix += 1
        tag = f"_{suffix}"
        candidate = f"{cleaned[: 31 - len(tag)]}{tag}"
    return candidate


EXCEL_ENV_COLUMNS = ["DEV", "QA", "PROD"]

# A sheet name Excel's 31-character limit can always accommodate, reserved
# up front so no iFlow's (possibly truncated) sheet name can ever collide
# with it. Maps each iFlow's full, untruncated ID to whichever (possibly
# truncated and/or disambiguated) sheet name it actually got -- see
# _render_bulk_excel_import, which reads this back to match by full ID
# rather than by comparing a truncated sheet name against the full ID.
INDEX_SHEET_NAME = "Index"
INDEX_COLUMNS = ["iFlow ID (full, untruncated)", "iFlow Name", "Sheet name in this workbook"]


def _build_parameters_excel(selected_iflows: list[dict], cache: dict, env_name: str) -> bytes:
    """Builds an in-memory .xlsx with one sheet per iFlow, named by the
    iFlow's technical Id, listing that iFlow's current externalized
    parameters under four fixed columns: Parameter Name, DEV, QA, PROD.
    The current value is populated into whichever of those three matches
    env_name (the environment you're actually connected to) -- the other
    two are left blank, ready to fill in as a cross-environment template.
    SAP_ProfileId (see _is_sap_restricted_parameter -- the one SAP_-
    prefixed parameter confirmed to reject writes) and dropdown-
    constrained parameters (see _is_dropdown_constrained_parameter) are
    both left out entirely -- this app never writes to either, so
    there's no point including a row you can't actually use the
    download/re-upload round trip to change. Every other SAP_-prefixed
    parameter is a normal, writable value and IS included.

    The FIRST sheet is always "Index": full iFlow ID -> actual sheet name
    for every iFlow in this export. iFlow IDs can exceed Excel's 31-char
    sheet-name limit (and two different IDs can even truncate to the same
    31 characters), so the data sheets' own names can't always be trusted
    to carry the full ID -- the Index sheet is what lets the importer
    match a re-uploaded sheet back to the correct iFlow regardless, by
    full ID rather than by (possibly truncated/disambiguated) sheet name.

    Takes the parameter cache and environment name as explicit arguments
    rather than reading st.session_state internally, same reasoning as
    _apply_parameter_update elsewhere: it keeps this a plain, directly
    unit-testable function.
    """
    columns = ["Parameter Name", *EXCEL_ENV_COLUMNS]
    target_col = env_name.strip().upper() if env_name.strip().upper() in EXCEL_ENV_COLUMNS else None

    # Pass 1: decide every iFlow's final sheet name up front (truncated
    # and/or disambiguated as needed), so the Index sheet can be written
    # FIRST, before any data sheet -- its own name reserved first so no
    # iFlow's sheet can ever collide with it.
    used_names: set[str] = {INDEX_SHEET_NAME.strip().lower()}
    sheet_name_by_key: dict[str, str] = {}
    for iflow in selected_iflows:
        cache_key = f"{iflow['Id']}::{iflow['Version']}"
        sheet_name = _safe_excel_sheet_name(iflow["Id"], used_names)
        used_names.add(sheet_name)
        sheet_name_by_key[cache_key] = sheet_name

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        index_rows = [
            {
                INDEX_COLUMNS[0]: iflow["Id"],
                INDEX_COLUMNS[1]: iflow.get("Name", ""),
                INDEX_COLUMNS[2]: sheet_name_by_key[f"{iflow['Id']}::{iflow['Version']}"],
            }
            for iflow in selected_iflows
        ]
        pd.DataFrame(index_rows, columns=INDEX_COLUMNS).to_excel(
            writer, sheet_name=INDEX_SHEET_NAME, index=False
        )

        if not selected_iflows:
            pd.DataFrame(columns=columns).to_excel(writer, sheet_name="Sheet1", index=False)
        for iflow in selected_iflows:
            cache_key = f"{iflow['Id']}::{iflow['Version']}"
            params, _ = cache.get(cache_key, ([], None))
            rows = []
            for p in params:
                key = p.get("ParameterKey") or ""
                if _is_sap_restricted_parameter(key):
                    continue  # confirmed to reject writes -- not worth including
                if _is_dropdown_constrained_parameter(key):
                    continue  # never written to from this app -- see Save's own exclusion
                row = {"Parameter Name": key, "DEV": "", "QA": "", "PROD": ""}
                if target_col:
                    row[target_col] = p.get("ParameterValue", "")
                rows.append(row)
            sheet_df = pd.DataFrame(rows, columns=columns)
            sheet_df.to_excel(writer, sheet_name=sheet_name_by_key[cache_key], index=False)
    return buf.getvalue()


# -- Cutover Checklist generator ------------------------------------------
#
# Produces a .xlsx matching the exact visual format of a real,
# user-supplied reference template (CutOver_QA_PTP-I-514_1_ACTIAN_2.xlsx):
# three sheets (List of Dependent Artifacts / the main checklist, named
# after the cutover itself / Config Params), specific header fill colors
# per sheet, green "section banner" rows that break the checklist into
# labeled phases, Calibri throughout, and the exact column widths
# measured from that file. Uses openpyxl directly (not pandas) since full
# control over fonts/fills/borders/widths is the point -- there are no
# formulas anywhere in the reference file, so no recalculation step is
# needed the way a financial model would need one.

_THIN_SIDE = Side(style="thin")
_THIN_BORDER = Border(left=_THIN_SIDE, right=_THIN_SIDE, top=_THIN_SIDE, bottom=_THIN_SIDE)
_CUTOVER_FONT_NAME = "Calibri"
_CUTOVER_MAIN_HEADER_FILL = PatternFill("solid", fgColor="00B0F0")
_CUTOVER_BANNER_FILL = PatternFill("solid", fgColor="70AD47")
_CUTOVER_DEPENDENT_HEADER_FILL = PatternFill("solid", fgColor="9BC2E6")
_CUTOVER_CONFIG_HEADER_FILL = PatternFill("solid", fgColor="8EA9DB")

CUTOVER_MAIN_COLUMNS = [
    "Step", "Reference Artifact", "Integration Package", "Actions",
    "Task execution time", "Notes", "Assignee Team", "Point Of Contact",
    "Status", "Completion Date",
]
_CUTOVER_MAIN_COLUMN_WIDTHS = [20.45, 36.27, 51.0, 54.73, 22.54, 43.0, 27.18, 29.27, 34.73, 18.18]
_CUTOVER_DEPENDENT_COLUMNS = ["S.No", "Package", "Artifact", "System"]
_CUTOVER_DEPENDENT_COLUMN_WIDTHS = [4.63, 42.73, 44.63, 29.73]
_CUTOVER_CONFIG_COLUMNS = ["Parameter Name", "Parameter Value", "Parameter Value", "Parameter Value"]
_CUTOVER_CONFIG_COLUMN_WIDTHS = [39.36, 52.18, 36.18, 37.36]

CUTOVER_STATUS_OPTIONS = ["Not Started", "In Progress", "Completed", "Blocked", "N/A"]


def _cutover_style_header_cell(cell, fill: PatternFill) -> None:
    cell.font = Font(name=_CUTOVER_FONT_NAME, size=11, bold=True)
    cell.fill = fill
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    cell.border = _THIN_BORDER


def _cutover_style_data_cell(cell, wrap: bool = True) -> None:
    cell.font = Font(name=_CUTOVER_FONT_NAME, size=11, bold=False)
    cell.alignment = Alignment(horizontal="left", vertical="top", wrap_text=wrap)
    cell.border = _THIN_BORDER


def _build_cutover_dependent_artifacts_sheet(wb, dependent_artifacts: list[dict]) -> None:
    ws = wb.create_sheet("List of Dependent Artifacts")
    for col_idx, (header, width) in enumerate(zip(_CUTOVER_DEPENDENT_COLUMNS, _CUTOVER_DEPENDENT_COLUMN_WIDTHS), start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        _cutover_style_header_cell(cell, _CUTOVER_DEPENDENT_HEADER_FILL)
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    for row_idx, artifact in enumerate(dependent_artifacts, start=2):
        ws.cell(row=row_idx, column=1, value=row_idx - 1)
        ws.cell(row=row_idx, column=2, value=artifact.get("Package", ""))
        ws.cell(row=row_idx, column=3, value=artifact.get("Artifact", ""))
        ws.cell(row=row_idx, column=4, value=artifact.get("System", ""))
        for col_idx in range(1, 5):
            _cutover_style_data_cell(ws.cell(row=row_idx, column=col_idx))
    ws.sheet_view.showGridLines = False


def _build_cutover_main_checklist_sheet(wb, sheet_name: str, checklist_entries: list[dict]) -> None:
    """checklist_entries: each either {"type": "section", "label": str}
    (a green banner row) or {"type": "step", ...the 9 non-Step fields...}
    (a numbered data row, numbered sequentially among step-type entries
    only -- section banners don't consume a Step number, matching the
    reference template's own convention)."""
    ws = wb.create_sheet(sheet_name)
    for col_idx, header in enumerate(CUTOVER_MAIN_COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        _cutover_style_header_cell(cell, _CUTOVER_MAIN_HEADER_FILL)
    for letter, width in zip("ABCDEFGHIJ", _CUTOVER_MAIN_COLUMN_WIDTHS):
        ws.column_dimensions[letter].width = width
    ws.row_dimensions[1].height = 14.5

    step_number = 1
    row_idx = 2
    for entry in checklist_entries:
        if entry["type"] == "section":
            for col_idx in range(1, 11):
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.fill = _CUTOVER_BANNER_FILL
                cell.font = Font(name=_CUTOVER_FONT_NAME, size=14, bold=True, color="000000")
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ws.cell(row=row_idx, column=4, value=entry["label"])
            ws.row_dimensions[row_idx].height = 20
        else:  # "step"
            values = [
                step_number, entry.get("reference_artifact", ""), entry.get("integration_package", ""),
                entry.get("actions", ""), entry.get("task_execution_time", ""), entry.get("notes", ""),
                entry.get("assignee_team", ""), entry.get("point_of_contact", ""),
                entry.get("status", "Not Started"), entry.get("completion_date", ""),
            ]
            for col_idx, value in enumerate(values, start=1):
                _cutover_style_data_cell(ws.cell(row=row_idx, column=col_idx, value=value))
            step_number += 1
        row_idx += 1
    ws.sheet_view.showGridLines = False


def _build_cutover_config_params_sheet(wb, config_params: list[dict]) -> None:
    """config_params: [{"Parameter Name": ..., "DEV": ..., "QA": ...,
    "PROD": ...}, ...] -- the SAME row shape the existing Parameter
    Management Excel download already uses, so a list already fetched
    that way (or pulled live via list_externalized_parameters) plugs in
    directly with no reshaping. Column HEADERS match the reference
    template exactly ("Parameter Value" repeated three times, with no
    explicit DEV/QA/PROD label) -- but the three columns are still
    populated in DEV/QA/PROD order underneath, mirroring the one
    multi-environment row (ActiveMQ_Queue_Name) the reference template
    itself had.
    """
    ws = wb.create_sheet("Config Params")
    for col_idx, (header, width) in enumerate(zip(_CUTOVER_CONFIG_COLUMNS, _CUTOVER_CONFIG_COLUMN_WIDTHS), start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        _cutover_style_header_cell(cell, _CUTOVER_CONFIG_HEADER_FILL)
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    for row_idx, param in enumerate(config_params, start=2):
        ws.cell(row=row_idx, column=1, value=param.get("Parameter Name", ""))
        ws.cell(row=row_idx, column=2, value=param.get("DEV", ""))
        ws.cell(row=row_idx, column=3, value=param.get("QA", ""))
        ws.cell(row=row_idx, column=4, value=param.get("PROD", ""))
        for col_idx in range(1, 5):
            _cutover_style_data_cell(ws.cell(row=row_idx, column=col_idx), wrap=False)
    ws.sheet_view.showGridLines = False


def _build_cutover_checklist_excel(
    checklist_title: str, dependent_artifacts: list[dict], checklist_entries: list[dict], config_params: list[dict]
) -> bytes:
    """Builds the complete 3-sheet cutover checklist workbook as bytes,
    ready for st.download_button. checklist_title becomes the main
    sheet's name (Excel sheet names are capped at 31 characters and
    can't contain []:*?/\\ -- _safe_excel_sheet_name, already used
    elsewhere in this app for the same reason, handles both)."""
    wb = Workbook()
    wb.remove(wb.active)  # drop the default blank sheet every new Workbook() starts with
    _build_cutover_dependent_artifacts_sheet(wb, dependent_artifacts)
    safe_title = _safe_excel_sheet_name(checklist_title or "Cutover Checklist", {"list of dependent artifacts", "config params"})
    _build_cutover_main_checklist_sheet(wb, safe_title, checklist_entries)
    _build_cutover_config_params_sheet(wb, config_params)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _render_bulk_excel_import(selected_iflows: list[dict]) -> None:
    """Multi-sheet import: one sheet per iFlow, with separate columns per
    environment (DEV/QA/PROD or whatever the sheets actually use) -- the
    user picks which column to pull from, once, and it applies across
    every matched sheet.

    Matching prefers an "Index" sheet (full iFlow ID -> actual sheet
    name), if the workbook has one -- which is how a file downloaded from
    this app's own download button carries each iFlow's full ID even
    when its data sheet's name had to be truncated (or disambiguated) to
    fit Excel's 31-character sheet-name limit. Falls back to matching the
    iFlow's name or ID directly against sheet names when there's no
    Index sheet (e.g. a hand-built file, or a download from before this
    existed), same as always.

    An iFlow with no externalized parameters at all is skipped outright,
    with an explicit note saying so -- there's nothing for it to match
    against regardless of whether a sheet was found for it.
    """
    # shown once, right after the apply button's rerun -- st.success()
    # called in the SAME run as st.rerun() would otherwise never actually
    # be seen, since rerun abandons whatever that run had queued to show
    if "_bulk_excel_result" in st.session_state:
        kind, msg = st.session_state.pop("_bulk_excel_result")
        (st.success if kind == "success" else st.info)(msg)

    uploaded = st.file_uploader(
        "Excel file", type=["xlsx", "xls"], key="bulk_excel_upload",
        help="One sheet per iFlow. If the workbook has an 'Index' sheet (as this app's own "
        "download produces), that's used to match sheets to iFlows by full ID; otherwise sheets "
        "are matched by name or ID directly. Each data sheet needs a parameter-name column and "
        "one column per environment (e.g. DEV, QA, PROD).",
    )
    if uploaded is None:
        st.caption(
            "Each sheet's name should match one of the selected iFlows (by name or ID) -- or, "
            "if the workbook has an 'Index' sheet, that's used instead, matching by full iFlow "
            "ID even if a data sheet's own name had to be shortened to fit Excel's 31-character "
            "limit. Within a sheet, one column should hold parameter names, and separate columns "
            "should hold each environment's value -- you'll be asked which one to use, once, for "
            "the whole import."
        )
        return

    try:
        excel_file = pd.ExcelFile(uploaded)
    except Exception as exc:  # noqa: BLE001 - e.g. not a real Excel file
        st.error(f"Could not read this file as Excel: {exc}")
        return

    # The Index sheet (if present) is a lookup table, never a data sheet in
    # its own right -- excluded here so it can never be accidentally
    # matched to an iFlow via the name/ID fallback below (e.g. if an iFlow
    # happened to be named "Index").
    index_sheet_actual_name = next(
        (s for s in excel_file.sheet_names if str(s).strip().lower() == INDEX_SHEET_NAME.strip().lower()),
        None,
    )
    sheet_lookup = {
        str(s).strip().lower(): s for s in excel_file.sheet_names if s != index_sheet_actual_name
    }

    # Full iFlow ID -> actual sheet name, read from the Index sheet when
    # one exists. Reading it is best-effort: any problem parsing it just
    # means every iFlow falls through to the ordinary name/ID matching
    # below, same as a workbook with no Index sheet at all.
    id_to_sheet: dict[str, str] = {}
    if index_sheet_actual_name:
        try:
            index_df = excel_file.parse(index_sheet_actual_name)
            if len(index_df.columns) >= 3:
                id_col, sheet_col = index_df.columns[0], index_df.columns[2]
                for _, row in index_df.iterrows():
                    full_id = str(row[id_col]).strip().lower()
                    sheet_name = str(row[sheet_col]).strip()
                    if full_id and sheet_name:
                        id_to_sheet[full_id] = sheet_name
        except Exception:  # noqa: BLE001 - fall back to name/ID matching entirely
            id_to_sheet = {}

    cache = st.session_state.get("param_cache", {})
    matched_sheet: dict[str, str] = {}  # iFlow cache_key -> matched sheet name
    unmatched_names = []
    skipped_no_params_names = []
    for iflow in selected_iflows:
        cache_key = f"{iflow['Id']}::{iflow['Version']}"

        # Point 4: nothing to update for an iFlow with no externalized
        # parameters, regardless of whether a sheet is found for it --
        # skip it outright, with an explicit reason, rather than silently
        # matching zero parameter names later.
        params, _ = cache.get(cache_key, ([], None))
        if not params:
            skipped_no_params_names.append(iflow["Name"])
            continue

        sheet = None
        indexed_sheet_name = id_to_sheet.get(iflow["Id"].strip().lower())
        if indexed_sheet_name and indexed_sheet_name.strip().lower() in sheet_lookup:
            sheet = sheet_lookup[indexed_sheet_name.strip().lower()]
        if not sheet:
            candidates = [iflow["Name"].strip().lower(), iflow["Id"].strip().lower()]
            sheet = next((sheet_lookup[c] for c in candidates if c in sheet_lookup), None)

        if sheet:
            matched_sheet[cache_key] = sheet
        else:
            unmatched_names.append(iflow["Name"])

    if skipped_no_params_names:
        st.info(
            f"Skipped (no externalized parameters to update): {', '.join(skipped_no_params_names)}."
        )
    if unmatched_names:
        st.warning(
            f"No matching sheet found for: {', '.join(unmatched_names)}. "
            "Those iFlows won't get any values from this file."
        )
    if not matched_sheet:
        st.info("No sheets matched any of the selected iFlows.")
        return

    # peek at the first matched sheet to discover the parameter-name column
    # and the candidate environment columns -- assumed consistent across
    # sheets, per how this format is described (one column per environment
    # on every tab)
    first_cache_key, first_sheet = next(iter(matched_sheet.items()))
    try:
        sample_df = excel_file.parse(first_sheet)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read sheet '{first_sheet}': {exc}")
        return

    if sample_df.empty or len(sample_df.columns) < 2:
        st.warning(f"Sheet '{first_sheet}' doesn't have at least two columns to work with.")
        return

    columns = [str(c) for c in sample_df.columns]
    name_guess = next((c for c in columns if any(w in c.lower() for w in ("param", "name", "key"))), columns[0])
    name_col = st.selectbox(
        "Column with parameter name (same on every sheet)",
        columns,
        index=columns.index(name_guess),
        key="bulk_excel_name_col",
    )

    env_options = [c for c in columns if c != name_col]
    if not env_options:
        st.warning("No columns left once the parameter-name column is excluded.")
        return
    env_col = st.selectbox(
        "Which environment's column should be used to update the parameters?",
        env_options,
        key="bulk_excel_env_col",
    )

    if st.button("Preview Values", key="bulk_excel_apply"):
        total_matched = 0
        summary_lines = []
        for iflow in selected_iflows:
            cache_key = f"{iflow['Id']}::{iflow['Version']}"
            sheet = matched_sheet.get(cache_key)
            if not sheet:
                continue
            try:
                sheet_df = sample_df if sheet == first_sheet else excel_file.parse(sheet)
            except Exception as exc:  # noqa: BLE001
                st.warning(f"Could not read sheet '{sheet}' for {iflow['Name']}: {exc}")
                continue
            if name_col not in sheet_df.columns or env_col not in sheet_df.columns:
                st.warning(
                    f"Sheet '{sheet}' for {iflow['Name']} doesn't have both '{name_col}' and "
                    f"'{env_col}' columns — skipped."
                )
                continue

            # Point 4: this iFlow's own matched sheet, shown later in its
            # own section below -- not a single, generic "first sheet
            # only" preview here, which only ever reflected one iFlow out
            # of however many were actually matched.
            st.session_state[f"_bulk_sheet_preview_{cache_key}"] = (sheet_df, name_col, env_col)

            params, _ = cache.get(cache_key, ([], None))
            # Same reasoning as the single-sheet importer: dropdown-
            # constrained parameters are never matched/staged from a
            # bulk upload either.
            param_keys = [p["ParameterKey"] for p in params if not _is_dropdown_constrained_parameter(p["ParameterKey"])]
            iflow_matches = _match_excel_values(sheet_df, name_col, env_col, param_keys)
            if iflow_matches:
                pending_key = f"param_pending_{cache_key}"
                gen_key = f"param_editor_gen_{cache_key}"
                st.session_state.setdefault(pending_key, {}).update(iflow_matches)
                st.session_state[gen_key] = st.session_state.get(gen_key, 0) + 1
                total_matched += len(iflow_matches)
                summary_lines.append(f"{iflow['Name']}: {len(iflow_matches)}")

        if total_matched:
            st.session_state["_bulk_excel_result"] = (
                "success",
                f"Applied {total_matched} value(s) from the **{env_col}** column across "
                f"{len(summary_lines)} iFlow(s) — {'; '.join(summary_lines)}. Review below, then Save "
                f"per iFlow.",
            )
            st.rerun()
        else:
            st.info("No parameter names matched in the sheets found for these iFlows.")


# ---------------------------------------------------------------------------
# Security Artifacts Management
# ---------------------------------------------------------------------------

# "All" (a single combined view across every type) is visible in the
# dropdown but not yet implemented -- everything else is. OAuth
# Credential and Secure Parameter's field names are this app's
# best-informed reading of SAP's own documentation, NOT independently
# confirmed against a real tenant's $metadata the way every User
# Credential field name already was -- see the client methods'
# docstrings in sap_cpi_client.py, and discover_entity_properties()
# there for how to verify/correct them against a real tenant.
SECURITY_MATERIAL_TYPES = ["User Credential", "Secure Parameter", "OAuth Credential", "All"]
USER_CREDENTIAL_KINDS = ["default", "successfactors", "openconnectors"]


def render_security_material_view(client: SAPCPIClient) -> None:
    st.subheader("🔒 Security Artifacts Management")
    st.caption(
        "View, add, and bulk-upload deployed security material on this tenant. **Passwords are "
        "never shown here, by design** -- SAP's API only ever returns the username once a "
        "credential is deployed, the same as the Manage Security Material screen in Web UI."
    )

    material_type = st.selectbox("Security artifact type", SECURITY_MATERIAL_TYPES, key="sec_mat_type")

    if material_type == "All":
        st.info(
            "**All** (a single combined view across every artifact type) isn't implemented yet. "
            "Switch to one specific type above."
        )
        return

    action = st.radio(
        "What would you like to do?",
        [
            "📋 View deployed credentials",
            "➕ Add/Update a single credential",
            "📤 Add/Update credentials in bulk (CSV)",
        ],
        key="sec_mat_action",
    )
    st.divider()

    if material_type == "User Credential":
        if action == "📋 View deployed credentials":
            _render_user_credentials_list(client)
        elif action == "➕ Add/Update a single credential":
            _render_user_credential_single_add(client)
        else:
            _render_user_credential_bulk_add(client)
    elif material_type == "OAuth Credential":
        if action == "📋 View deployed credentials":
            _render_oauth2_credentials_list(client)
        elif action == "➕ Add/Update a single credential":
            _render_oauth2_credential_single_add(client)
        else:
            _render_oauth2_credential_bulk_add(client)
    else:  # "Secure Parameter"
        if action == "📋 View deployed credentials":
            _render_secure_parameters_list(client)
        elif action == "➕ Add/Update a single credential":
            _render_secure_parameter_single_add(client)
        else:
            _render_secure_parameter_bulk_add(client)


def _diagnose_usercred_failure(client: SAPCPIClient, exc: Exception) -> dict:
    """Builds structured diagnostic info for a failed list_user_credentials()
    call: the original error, plus a direct check of the tenant's own
    $metadata for anything resembling a credentials entity set. That
    check tells us whether this is a naming mismatch (something turns
    up) or more likely a missing role/permission on the service key
    (nothing does, even though the tenant is known to have credentials
    deployed) -- either way, it's a definitive next step rather than
    another guess at a name.
    """
    info: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tenant_url": client.env.url,
        "error": str(exc),
        "discovered_entity_sets": None,
        "discovery_error": None,
    }
    try:
        info["discovered_entity_sets"] = client.discover_security_entity_sets()
    except Exception as diag_exc:  # noqa: BLE001
        info["discovery_error"] = str(diag_exc)
    return info


def _format_usercred_diagnostic_log(info: dict) -> str:
    lines = [
        f"Timestamp: {info['timestamp']}",
        f"Tenant URL: {info['tenant_url']}",
        f"Error from list_user_credentials(): {info['error']}",
    ]
    if info["discovered_entity_sets"] is not None:
        if info["discovered_entity_sets"]:
            lines.append(
                "Entity set(s) containing 'credential' found in this tenant's own $metadata: "
                + ", ".join(info["discovered_entity_sets"])
            )
        else:
            lines.append(
                "No entity set containing 'credential' was found in this tenant's $metadata at "
                "all -- this points toward a missing role/permission on the service key for "
                "Security Content specifically, rather than a naming mismatch."
            )
    else:
        lines.append(f"Additionally, could not fetch $metadata to check further: {info['discovery_error']}")
    return "\n".join(lines)


def _render_user_credentials_list(client: SAPCPIClient) -> None:
    refresh = st.button("🔄 Refresh", key="usercred_refresh")
    if "user_credentials_raw" not in st.session_state or refresh:
        with st.spinner("Loading deployed User Credentials…"):
            try:
                st.session_state.user_credentials_raw = client.list_user_credentials()
                st.session_state.pop("_usercred_diag_info", None)
            except Exception as exc:  # noqa: BLE001
                st.session_state.user_credentials_raw = []
                st.session_state["_usercred_diag_info"] = _diagnose_usercred_failure(client, exc)

    diag_info = st.session_state.get("_usercred_diag_info")
    if diag_info:
        st.error(f"Could not load User Credentials: {diag_info['error']}")
        found = diag_info["discovered_entity_sets"]
        if found:
            st.warning(
                f"This tenant's own `$metadata` lists these entity set(s) containing "
                f"'credential': **{', '.join(found)}**. If that's different from "
                f"'UserCredentials' / 'UserCredential', that's very likely the real name "
                f"needed here."
            )
        elif found == []:
            st.warning(
                "This tenant's own `$metadata` has **no** entity set containing 'credential' "
                "at all. That points toward a missing role/permission on the service key for "
                "Security Content specifically (a separate role from Integration Content), "
                "rather than a naming mismatch -- worth checking with your BTP administrator."
            )
        else:
            st.caption(f"(Could not check `$metadata` for more detail: {diag_info['discovery_error']})")

        st.download_button(
            "📥 Download diagnostic log",
            data=_format_usercred_diagnostic_log(diag_info).encode("utf-8"),
            file_name="user_credentials_diagnostic.txt",
            mime="text/plain",
            key="usercred_diag_download",
            help="The error, the tenant URL, and whatever this tenant's own $metadata says "
            "about available credential-related entity sets, as a plain-text file -- share "
            "this with Claude or your BTP administrator for further troubleshooting.",
        )

    creds = st.session_state.get("user_credentials_raw", [])
    if not creds:
        if not diag_info:
            st.info("No User Credentials found on this tenant.")
        return

    st.caption(
        f"{len(creds)} User Credential(s) deployed. 🔒 The **Password** column is never "
        f"populated — see the note above."
    )
    df = pd.DataFrame(
        [
            {
                "Name": c.get("Name", ""),
                "Kind": c.get("Kind", ""),
                "Description": c.get("Description", ""),
                "User": c.get("User", ""),
                "Password": "🔒 (not returned by API)",
                "CompanyId": c.get("CompanyId", ""),
                "Type": c.get("Type", "User Credentials"),
                "Deployed By": c.get("DeployedBy", ""),
                "Deployed On": parse_odata_date(c.get("DeployedOn")),
                "Status": c.get("Status", ""),
            }
            for c in creds
        ]
    )
    st.dataframe(df, hide_index=True, width="stretch")

    st.download_button(
        "📤 Download this list as CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="user_credentials.csv",
        mime="text/csv",
        key="usercred_download",
        help="A record of what's deployed, for reference -- not meant to be re-uploaded as-is, "
        "since the Password column never has a real value to carry forward.",
    )


def _render_user_credential_single_add(client: SAPCPIClient) -> None:
    name = st.text_input("Name", key="usercred_name")
    kind = st.selectbox("Kind", USER_CREDENTIAL_KINDS, key="usercred_kind")
    description = st.text_input("Description", key="usercred_description")
    user = st.text_input("User", key="usercred_user")
    password = st.text_input("Password", type="password", key="usercred_password")
    company_id = ""
    if kind == "successfactors":
        company_id = st.text_input(
            "Company ID", key="usercred_company_id", help="Required for the 'successfactors' kind."
        )

    st.caption(
        "If a credential named exactly this already exists, it's **updated** in place (PUT) "
        "rather than rejected as a duplicate; otherwise a **new** one is created (POST)."
    )
    if st.button("Add/Update user credential in security material", type="primary", key="usercred_submit"):
        if not name.strip():
            st.error("Name is required.")
        elif kind == "successfactors" and not company_id.strip():
            st.error("Company ID is required when Kind is 'successfactors'.")
        else:
            try:
                action = client.upsert_user_credential(
                    name=name.strip(), kind=kind, description=description, user=user,
                    password=password, company_id=company_id.strip(),
                )
                verb = "Updated" if action == "updated" else "Added"
                st.success(f"✅ {verb} '{name.strip()}' in Security Material.")
                st.session_state.pop("user_credentials_raw", None)  # next list view fetches fresh
            except Exception as exc:  # noqa: BLE001
                st.error(f"❌ Could not add/update '{name.strip()}': {exc}")


def _render_user_credential_bulk_add(client: SAPCPIClient) -> None:
    st.caption(
        "Upload a CSV with header row: **Name, Kind, Description, user, password, CompanyId**. "
        "`Kind` must be exactly one of: default, successfactors, openconnectors. `CompanyId` is "
        "required only when `Kind` is `successfactors` — leave it blank for the other two. A row "
        "whose Name already exists is **updated** in place (PUT) rather than rejected as a "
        "duplicate; a new Name is **created** (POST)."
    )
    template_df = pd.DataFrame(
        [{
            "Name": "ExampleCred", "Kind": "default", "Description": "Example credential",
            "user": "exampleuser", "password": "examplepass", "CompanyId": "",
        }]
    )
    st.download_button(
        "📥 Download a CSV template",
        data=template_df.to_csv(index=False).encode("utf-8"),
        file_name="user_credentials_template.csv",
        mime="text/csv",
        key="usercred_bulk_template",
    )

    uploaded = st.file_uploader("CSV file", type=["csv"], key="usercred_bulk_upload")
    if uploaded is None:
        return

    try:
        upload_df = pd.read_csv(uploaded, dtype=str).fillna("")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read this file as CSV: {exc}")
        return

    # case/whitespace-insensitive column matching, same spirit as the
    # parameter Excel importer elsewhere in this app
    col_lookup = {str(c).strip().lower(): c for c in upload_df.columns}
    required = ["name", "kind", "description", "user", "password", "companyid"]
    missing = [r for r in required if r not in col_lookup]
    if missing:
        st.error(
            f"Missing required column(s): {', '.join(missing)}. Found: {', '.join(upload_df.columns)}."
        )
        return

    st.caption(f"Preview ({len(upload_df)} row(s)):")
    st.dataframe(upload_df.astype(str).head(10), hide_index=True, width="stretch")

    if st.button(
        f"Add/Update all {len(upload_df)} credential(s) in security material",
        type="primary", key="usercred_bulk_apply",
    ):
        results = []
        for i, row in upload_df.iterrows():
            name = str(row[col_lookup["name"]]).strip()
            kind = str(row[col_lookup["kind"]]).strip()
            description = str(row[col_lookup["description"]]).strip()
            user = str(row[col_lookup["user"]]).strip()
            password = str(row[col_lookup["password"]]).strip()
            company_id = str(row[col_lookup["companyid"]]).strip()
            spreadsheet_row = i + 2  # +1 for 0-index, +1 for the header row

            if not name:
                results.append((f"(row {spreadsheet_row}, no Name)", None, "Name is blank — skipped"))
                continue
            try:
                action = client.upsert_user_credential(
                    name=name, kind=kind, description=description, user=user,
                    password=password, company_id=company_id,
                )
                results.append((name, action, "Updated" if action == "updated" else "Added"))
            except Exception as exc:  # noqa: BLE001
                results.append((name, False, str(exc)))

        st.session_state.pop("user_credentials_raw", None)
        succeeded = sum(1 for _, action, _ in results if action)
        if succeeded == len(results):
            st.success(f"All {succeeded} of {len(results)} credential(s) added/updated successfully.")
        elif succeeded:
            st.warning(
                f"{succeeded} of {len(results)} credential(s) added/updated — see details below for the rest."
            )
        else:
            st.error("None of the credentials were added or updated — see details below.")

        result_df = pd.DataFrame(
            [
                {
                    "Name": n,
                    "Result": f"✅ {detail}" if action else "❌ Failed",
                    "Detail": detail if not action else "",
                }
                for n, action, detail in results
            ]
        )
        st.dataframe(result_df, hide_index=True, width="stretch")


# -- OAuth2 Client Credentials ------------------------------------------

def _diagnose_oauth2_failure(client: SAPCPIClient, exc: Exception) -> dict:
    """Same role as _diagnose_usercred_failure above, for OAuth2 Client
    Credentials -- "credential" is still the right keyword, since
    "OAuth2ClientCredentials" contains it too."""
    info: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tenant_url": client.env.url,
        "error": str(exc),
        "discovered_entity_sets": None,
        "discovery_error": None,
    }
    try:
        info["discovered_entity_sets"] = client.discover_security_entity_sets("credential")
    except Exception as diag_exc:  # noqa: BLE001
        info["discovery_error"] = str(diag_exc)
    return info


def _format_oauth2_diagnostic_log(info: dict) -> str:
    lines = [
        f"Timestamp: {info['timestamp']}",
        f"Tenant URL: {info['tenant_url']}",
        f"Error from list_oauth2_credentials(): {info['error']}",
    ]
    if info["discovered_entity_sets"] is not None:
        if info["discovered_entity_sets"]:
            lines.append(
                "Entity set(s) containing 'credential' found in this tenant's own $metadata: "
                + ", ".join(info["discovered_entity_sets"])
            )
        else:
            lines.append(
                "No entity set containing 'credential' was found in this tenant's $metadata at "
                "all -- this points toward a missing role/permission on the service key for "
                "Security Content specifically, rather than a naming mismatch."
            )
    else:
        lines.append(f"Additionally, could not fetch $metadata to check further: {info['discovery_error']}")
    return "\n".join(lines)


def _render_oauth2_credentials_list(client: SAPCPIClient) -> None:
    refresh = st.button("🔄 Refresh", key="oauth2cred_refresh")
    if "oauth2_credentials_raw" not in st.session_state or refresh:
        with st.spinner("Loading deployed OAuth2 Client Credentials…"):
            try:
                st.session_state.oauth2_credentials_raw = client.list_oauth2_credentials()
                st.session_state.pop("_oauth2cred_diag_info", None)
            except Exception as exc:  # noqa: BLE001
                st.session_state.oauth2_credentials_raw = []
                st.session_state["_oauth2cred_diag_info"] = _diagnose_oauth2_failure(client, exc)

    diag_info = st.session_state.get("_oauth2cred_diag_info")
    if diag_info:
        st.error(f"Could not load OAuth2 Client Credentials: {diag_info['error']}")
        found = diag_info["discovered_entity_sets"]
        if found:
            st.warning(
                f"This tenant's own `$metadata` lists these entity set(s) containing "
                f"'credential': **{', '.join(found)}**. If that's different from "
                f"'OAuth2ClientCredentials', that's very likely the real name needed here."
            )
        elif found == []:
            st.warning(
                "This tenant's own `$metadata` has **no** entity set containing 'credential' "
                "at all. That points toward a missing role/permission on the service key for "
                "Security Content specifically (a separate role from Integration Content), "
                "rather than a naming mismatch -- worth checking with your BTP administrator."
            )
        else:
            st.caption(f"(Could not check `$metadata` for more detail: {diag_info['discovery_error']})")

        st.download_button(
            "📥 Download diagnostic log",
            data=_format_oauth2_diagnostic_log(diag_info).encode("utf-8"),
            file_name="oauth2_credentials_diagnostic.txt",
            mime="text/plain",
            key="oauth2cred_diag_download",
            help="The error, the tenant URL, and whatever this tenant's own $metadata says "
            "about available credential-related entity sets, as a plain-text file -- share "
            "this with Claude or your BTP administrator for further troubleshooting.",
        )

    creds = st.session_state.get("oauth2_credentials_raw", [])
    if not creds:
        if not diag_info:
            st.info("No OAuth2 Client Credentials found on this tenant.")
        return

    st.caption(
        f"{len(creds)} OAuth2 Client Credential(s) deployed. 🔒 The **Client Secret** column is "
        f"never populated — SAP's API only ever returns it write-only, same reasoning as "
        f"Password for User Credentials."
    )
    df = pd.DataFrame(
        [
            {
                "Name": c.get("Name", ""),
                "Description": c.get("Description", ""),
                "Client ID": c.get("ClientId", ""),
                "Client Secret": "🔒 (not returned by API)",
                "Token Service URL": c.get("TokenServiceUrl", ""),
                "Type": c.get("Type", "OAuth2 Client Credentials"),
                "Deployed By": c.get("DeployedBy", ""),
                "Deployed On": parse_odata_date(c.get("DeployedOn")),
                "Status": c.get("Status", ""),
            }
            for c in creds
        ]
    )
    st.dataframe(df, hide_index=True, width="stretch")

    st.download_button(
        "📤 Download this list as CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="oauth2_credentials.csv",
        mime="text/csv",
        key="oauth2cred_download",
        help="A record of what's deployed, for reference -- not meant to be re-uploaded as-is, "
        "since the Client Secret column never has a real value to carry forward.",
    )


def _render_oauth2_credential_single_add(client: SAPCPIClient) -> None:
    name = st.text_input("Name", key="oauth2cred_name")
    description = st.text_input("Description", key="oauth2cred_description")
    client_id = st.text_input("Client ID", key="oauth2cred_client_id")
    client_secret = st.text_input("Client Secret", type="password", key="oauth2cred_client_secret")
    token_service_url = st.text_input(
        "Token Service URL", key="oauth2cred_token_service_url",
        help="URL of the OAuth2 authorization server that issues the access token.",
    )

    st.caption(
        "If a credential named exactly this already exists, it's **updated** in place (PUT) "
        "rather than rejected as a duplicate; otherwise a **new** one is created (POST)."
    )
    if st.button("Add/Update OAuth2 client credential in security material", type="primary", key="oauth2cred_submit"):
        if not name.strip():
            st.error("Name is required.")
        elif not client_id.strip():
            st.error("Client ID is required.")
        elif not token_service_url.strip():
            st.error("Token Service URL is required.")
        else:
            try:
                action = client.upsert_oauth2_credential(
                    name=name.strip(), description=description, client_id=client_id.strip(),
                    client_secret=client_secret, token_service_url=token_service_url.strip(),
                )
                verb = "Updated" if action == "updated" else "Added"
                st.success(f"✅ {verb} '{name.strip()}' in Security Material.")
                st.session_state.pop("oauth2_credentials_raw", None)  # next list view fetches fresh
            except Exception as exc:  # noqa: BLE001
                st.error(f"❌ Could not add/update '{name.strip()}': {exc}")


def _render_oauth2_credential_bulk_add(client: SAPCPIClient) -> None:
    st.caption(
        "Upload a CSV with header row: **Name, Description, ClientId, ClientSecret, "
        "TokenServiceUrl**. A row whose Name already exists is **updated** in place (PUT) "
        "rather than rejected as a duplicate; a new Name is **created** (POST)."
    )
    template_df = pd.DataFrame(
        [{
            "Name": "ExampleOAuthCred", "Description": "Example OAuth2 credential",
            "ClientId": "example-client-id", "ClientSecret": "example-client-secret",
            "TokenServiceUrl": "https://auth.example.com/oauth/token",
        }]
    )
    st.download_button(
        "📥 Download a CSV template",
        data=template_df.to_csv(index=False).encode("utf-8"),
        file_name="oauth2_credentials_template.csv",
        mime="text/csv",
        key="oauth2cred_bulk_template",
    )

    uploaded = st.file_uploader("CSV file", type=["csv"], key="oauth2cred_bulk_upload")
    if uploaded is None:
        return

    try:
        upload_df = pd.read_csv(uploaded, dtype=str).fillna("")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read this file as CSV: {exc}")
        return

    col_lookup = {str(c).strip().lower(): c for c in upload_df.columns}
    required = ["name", "description", "clientid", "clientsecret", "tokenserviceurl"]
    missing = [r for r in required if r not in col_lookup]
    if missing:
        st.error(
            f"Missing required column(s): {', '.join(missing)}. Found: {', '.join(upload_df.columns)}."
        )
        return

    st.caption(f"Preview ({len(upload_df)} row(s)):")
    st.dataframe(upload_df.astype(str).head(10), hide_index=True, width="stretch")

    if st.button(
        f"Add/Update all {len(upload_df)} credential(s) in security material",
        type="primary", key="oauth2cred_bulk_apply",
    ):
        results = []
        for i, row in upload_df.iterrows():
            name = str(row[col_lookup["name"]]).strip()
            description = str(row[col_lookup["description"]]).strip()
            client_id = str(row[col_lookup["clientid"]]).strip()
            client_secret = str(row[col_lookup["clientsecret"]]).strip()
            token_service_url = str(row[col_lookup["tokenserviceurl"]]).strip()
            spreadsheet_row = i + 2

            if not name:
                results.append((f"(row {spreadsheet_row}, no Name)", None, "Name is blank — skipped"))
                continue
            if not client_id:
                results.append((name, False, "ClientId is blank — skipped"))
                continue
            if not token_service_url:
                results.append((name, False, "TokenServiceUrl is blank — skipped"))
                continue
            try:
                action = client.upsert_oauth2_credential(
                    name=name, description=description, client_id=client_id,
                    client_secret=client_secret, token_service_url=token_service_url,
                )
                results.append((name, action, "Updated" if action == "updated" else "Added"))
            except Exception as exc:  # noqa: BLE001
                results.append((name, False, str(exc)))

        st.session_state.pop("oauth2_credentials_raw", None)
        succeeded = sum(1 for _, action, _ in results if action)
        if succeeded == len(results):
            st.success(f"All {succeeded} of {len(results)} credential(s) added/updated successfully.")
        elif succeeded:
            st.warning(
                f"{succeeded} of {len(results)} credential(s) added/updated — see details below for the rest."
            )
        else:
            st.error("None of the credentials were added or updated — see details below.")

        result_df = pd.DataFrame(
            [
                {
                    "Name": n,
                    "Result": f"✅ {detail}" if action else "❌ Failed",
                    "Detail": detail if not action else "",
                }
                for n, action, detail in results
            ]
        )
        st.dataframe(result_df, hide_index=True, width="stretch")


# -- Secure Parameters ---------------------------------------------------

def _diagnose_secure_param_failure(client: SAPCPIClient, exc: Exception) -> dict:
    """Same role as _diagnose_usercred_failure above, for Secure
    Parameters -- "parameter" is the keyword here instead of
    "credential", since "SecureParameters" doesn't contain the latter."""
    info: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tenant_url": client.env.url,
        "error": str(exc),
        "discovered_entity_sets": None,
        "discovery_error": None,
    }
    try:
        info["discovered_entity_sets"] = client.discover_security_entity_sets("parameter")
    except Exception as diag_exc:  # noqa: BLE001
        info["discovery_error"] = str(diag_exc)
    return info


def _format_secure_param_diagnostic_log(info: dict) -> str:
    lines = [
        f"Timestamp: {info['timestamp']}",
        f"Tenant URL: {info['tenant_url']}",
        f"Error from list_secure_parameters(): {info['error']}",
    ]
    if info["discovered_entity_sets"] is not None:
        if info["discovered_entity_sets"]:
            lines.append(
                "Entity set(s) containing 'parameter' found in this tenant's own $metadata: "
                + ", ".join(info["discovered_entity_sets"])
            )
        else:
            lines.append(
                "No entity set containing 'parameter' was found in this tenant's $metadata at "
                "all -- this points toward a missing role/permission on the service key for "
                "Security Content specifically, rather than a naming mismatch."
            )
    else:
        lines.append(f"Additionally, could not fetch $metadata to check further: {info['discovery_error']}")
    return "\n".join(lines)


def _render_secure_parameters_list(client: SAPCPIClient) -> None:
    refresh = st.button("🔄 Refresh", key="secureparam_refresh")
    if "secure_parameters_raw" not in st.session_state or refresh:
        with st.spinner("Loading deployed Secure Parameters…"):
            try:
                st.session_state.secure_parameters_raw = client.list_secure_parameters()
                st.session_state.pop("_secureparam_diag_info", None)
            except Exception as exc:  # noqa: BLE001
                st.session_state.secure_parameters_raw = []
                st.session_state["_secureparam_diag_info"] = _diagnose_secure_param_failure(client, exc)

    diag_info = st.session_state.get("_secureparam_diag_info")
    if diag_info:
        st.error(f"Could not load Secure Parameters: {diag_info['error']}")
        found = diag_info["discovered_entity_sets"]
        if found:
            st.warning(
                f"This tenant's own `$metadata` lists these entity set(s) containing "
                f"'parameter': **{', '.join(found)}**. If that's different from "
                f"'SecureParameters', that's very likely the real name needed here."
            )
        elif found == []:
            st.warning(
                "This tenant's own `$metadata` has **no** entity set containing 'parameter' "
                "at all. That points toward a missing role/permission on the service key for "
                "Security Content specifically (a separate role from Integration Content), "
                "rather than a naming mismatch -- worth checking with your BTP administrator."
            )
        else:
            st.caption(f"(Could not check `$metadata` for more detail: {diag_info['discovery_error']})")

        st.download_button(
            "📥 Download diagnostic log",
            data=_format_secure_param_diagnostic_log(diag_info).encode("utf-8"),
            file_name="secure_parameters_diagnostic.txt",
            mime="text/plain",
            key="secureparam_diag_download",
            help="The error, the tenant URL, and whatever this tenant's own $metadata says "
            "about available parameter-related entity sets, as a plain-text file -- share "
            "this with Claude or your BTP administrator for further troubleshooting.",
        )

    params = st.session_state.get("secure_parameters_raw", [])
    if not params:
        if not diag_info:
            st.info("No Secure Parameters found on this tenant.")
        return

    st.caption(
        f"{len(params)} Secure Parameter(s) deployed. 🔒 The **Value** column is never "
        f"populated — SAP's API only ever returns it write-only, same reasoning as Password "
        f"for User Credentials."
    )
    df = pd.DataFrame(
        [
            {
                "Name": p.get("Name", ""),
                "Description": p.get("Description", ""),
                "Value": "🔒 (not returned by API)",
                "Type": p.get("Type", "Secure Parameter"),
                "Deployed By": p.get("DeployedBy", ""),
                "Deployed On": parse_odata_date(p.get("DeployedOn")),
                "Status": p.get("Status", ""),
            }
            for p in params
        ]
    )
    st.dataframe(df, hide_index=True, width="stretch")

    st.download_button(
        "📤 Download this list as CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="secure_parameters.csv",
        mime="text/csv",
        key="secureparam_download",
        help="A record of what's deployed, for reference -- not meant to be re-uploaded as-is, "
        "since the Value column never has a real value to carry forward.",
    )


def _render_secure_parameter_single_add(client: SAPCPIClient) -> None:
    name = st.text_input("Name", key="secureparam_name")
    description = st.text_input("Description", key="secureparam_description")
    value = st.text_input("Value", type="password", key="secureparam_value")

    st.caption(
        "If a parameter named exactly this already exists, it's **updated** in place (PUT) "
        "rather than rejected as a duplicate; otherwise a **new** one is created (POST)."
    )
    if st.button("Add/Update secure parameter in security material", type="primary", key="secureparam_submit"):
        if not name.strip():
            st.error("Name is required.")
        elif not value:
            st.error("Value is required.")
        else:
            try:
                action = client.upsert_secure_parameter(name=name.strip(), description=description, value=value)
                verb = "Updated" if action == "updated" else "Added"
                st.success(f"✅ {verb} '{name.strip()}' in Security Material.")
                st.session_state.pop("secure_parameters_raw", None)  # next list view fetches fresh
            except Exception as exc:  # noqa: BLE001
                st.error(f"❌ Could not add/update '{name.strip()}': {exc}")


def _render_secure_parameter_bulk_add(client: SAPCPIClient) -> None:
    st.caption(
        "Upload a CSV with header row: **Name, Description, Value**. A row whose Name already "
        "exists is **updated** in place (PUT) rather than rejected as a duplicate; a new Name "
        "is **created** (POST)."
    )
    template_df = pd.DataFrame(
        [{"Name": "ExampleSecureParam", "Description": "Example secure parameter", "Value": "example-secret-value"}]
    )
    st.download_button(
        "📥 Download a CSV template",
        data=template_df.to_csv(index=False).encode("utf-8"),
        file_name="secure_parameters_template.csv",
        mime="text/csv",
        key="secureparam_bulk_template",
    )

    uploaded = st.file_uploader("CSV file", type=["csv"], key="secureparam_bulk_upload")
    if uploaded is None:
        return

    try:
        upload_df = pd.read_csv(uploaded, dtype=str).fillna("")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not read this file as CSV: {exc}")
        return

    col_lookup = {str(c).strip().lower(): c for c in upload_df.columns}
    required = ["name", "description", "value"]
    missing = [r for r in required if r not in col_lookup]
    if missing:
        st.error(
            f"Missing required column(s): {', '.join(missing)}. Found: {', '.join(upload_df.columns)}."
        )
        return

    st.caption(f"Preview ({len(upload_df)} row(s)):")
    st.dataframe(upload_df.astype(str).head(10), hide_index=True, width="stretch")

    if st.button(
        f"Add/Update all {len(upload_df)} parameter(s) in security material",
        type="primary", key="secureparam_bulk_apply",
    ):
        results = []
        for i, row in upload_df.iterrows():
            name = str(row[col_lookup["name"]]).strip()
            description = str(row[col_lookup["description"]]).strip()
            value = str(row[col_lookup["value"]]).strip()
            spreadsheet_row = i + 2

            if not name:
                results.append((f"(row {spreadsheet_row}, no Name)", None, "Name is blank — skipped"))
                continue
            if not value:
                results.append((name, False, "Value is blank — skipped"))
                continue
            try:
                action = client.upsert_secure_parameter(name=name, description=description, value=value)
                results.append((name, action, "Updated" if action == "updated" else "Added"))
            except Exception as exc:  # noqa: BLE001
                results.append((name, False, str(exc)))

        st.session_state.pop("secure_parameters_raw", None)
        succeeded = sum(1 for _, action, _ in results if action)
        if succeeded == len(results):
            st.success(f"All {succeeded} of {len(results)} parameter(s) added/updated successfully.")
        elif succeeded:
            st.warning(
                f"{succeeded} of {len(results)} parameter(s) added/updated — see details below for the rest."
            )
        else:
            st.error("None of the parameters were added or updated — see details below.")

        result_df = pd.DataFrame(
            [
                {
                    "Name": n,
                    "Result": f"✅ {detail}" if action else "❌ Failed",
                    "Detail": detail if not action else "",
                }
                for n, action, detail in results
            ]
        )
        st.dataframe(result_df, hide_index=True, width="stretch")


def render_cutover_checklist_view(client: SAPCPIClient) -> None:
    st.subheader("📝 Cutover Checklist Generator")
    st.caption(
        "Builds a cutover checklist .xlsx matching a real-world reference template's exact "
        "visual format (fonts, colors, column layout) -- fill in each part below, then generate."
    )

    st.session_state.setdefault("cutover_dependent_artifacts", [])
    st.session_state.setdefault("cutover_checklist_entries", [])

    st.text_input(
        "Checklist title (becomes the main sheet's name)",
        key="cutover_title", placeholder="e.g. CutOver_QA_PTP-I-514.1",
    )

    st.divider()

    # --- Interface selection, for Config Params ---
    st.markdown("#### Interface(s) for this cutover")
    st.caption("Used to pull real, current externalized parameters into the Config Params sheet below.")

    if "cutover_packages_raw" not in st.session_state:
        with st.spinner("Loading packages…"):
            st.session_state.cutover_packages_raw = client.list_packages()
    packages = st.session_state.cutover_packages_raw
    package_name_by_id = {p["Id"]: p["Name"] for p in packages}

    selected_package_ids = st.multiselect(
        "Package(s)",
        options=sorted(package_name_by_id.keys(), key=lambda pid: package_name_by_id[pid]),
        format_func=lambda pid: package_name_by_id.get(pid, pid),
        key="cutover_selected_package_ids",
    )

    cutover_iflow_options: dict[str, dict] = {}
    for pkg_id in selected_package_ids:
        cache_key = f"_cutover_iflow_cache_{pkg_id}"
        if cache_key not in st.session_state:
            with st.spinner(f"Loading integration flows in {package_name_by_id.get(pkg_id, pkg_id)}…"):
                st.session_state[cache_key] = client.list_artifacts(pkg_id, IFLOW_ENTITY_SET)
        for iflow in st.session_state[cache_key]:
            key = f"{iflow['Id']}::{iflow['Version']}"
            entry = dict(iflow)
            entry["_package_name"] = package_name_by_id.get(pkg_id, pkg_id)
            cutover_iflow_options[key] = entry

    selected_iflow_keys = st.multiselect(
        "Integration Flow(s)",
        options=list(cutover_iflow_options.keys()),
        format_func=lambda k: f"{cutover_iflow_options[k]['Name']} ({cutover_iflow_options[k]['_package_name']})",
        key="cutover_selected_iflow_keys",
    )
    selected_iflows = [cutover_iflow_options[k] for k in selected_iflow_keys if k in cutover_iflow_options]

    st.divider()

    # --- Dependent artifacts ---
    st.markdown("#### List of Dependent Artifacts")
    with st.form("cutover_add_dependent_artifact", clear_on_submit=True):
        cols = st.columns(3)
        da_package = cols[0].text_input("Package")
        da_artifact = cols[1].text_input("Artifact")
        da_system = cols[2].text_input("System")
        if st.form_submit_button("➕ Add dependent artifact"):
            if da_package.strip() or da_artifact.strip():
                st.session_state.cutover_dependent_artifacts.append(
                    {"Package": da_package.strip(), "Artifact": da_artifact.strip(), "System": da_system.strip()}
                )
            else:
                st.warning("Enter at least a Package or Artifact name.")

    for i, artifact in enumerate(st.session_state.cutover_dependent_artifacts):
        row_cols = st.columns([1, 4, 4, 3, 1])
        row_cols[0].write(i + 1)
        row_cols[1].write(artifact["Package"])
        row_cols[2].write(artifact["Artifact"])
        row_cols[3].write(artifact["System"])
        if row_cols[4].button("✕", key=f"cutover_da_remove_{i}", help="Remove this row"):
            st.session_state.cutover_dependent_artifacts.pop(i)
            st.rerun()
    if not st.session_state.cutover_dependent_artifacts:
        st.caption("No dependent artifacts added yet.")

    st.divider()

    # --- Checklist steps/sections ---
    st.markdown("#### Checklist Steps")
    st.caption(
        "Build the checklist as a sequence: add a **section banner** to start a new labeled "
        "phase (e.g. \"Integration Team\", \"Smoke Test(Post Cutover)\"), then add **steps** "
        "under it. Steps are numbered automatically in the order added; section banners don't "
        "consume a number, matching the reference template's own convention."
    )

    entry_kind = st.radio("Add a…", ["Step", "Section banner"], key="cutover_entry_kind", horizontal=True)

    if entry_kind == "Section banner":
        with st.form("cutover_add_section", clear_on_submit=True):
            label = st.text_input("Section label", placeholder="e.g. Integration Team")
            if st.form_submit_button("➕ Add section banner"):
                if label.strip():
                    st.session_state.cutover_checklist_entries.append({"type": "section", "label": label.strip()})
                else:
                    st.warning("Enter a section label.")
    else:
        package_hint_options = sorted({i["_package_name"] for i in selected_iflows})
        with st.form("cutover_add_step", clear_on_submit=True):
            reference_artifact = st.text_input(
                "Reference Artifact", placeholder="e.g. Common activities, or an iFlow name"
            )
            if package_hint_options:
                integration_package_pick = st.selectbox(
                    "Integration Package", options=["(type manually below)"] + package_hint_options,
                )
            else:
                integration_package_pick = "(type manually below)"
            integration_package_manual = st.text_input(
                "Integration Package (type here if not using the selection above, or it's blank)"
            )
            actions = st.text_area("Actions", placeholder="What needs to be done in this step")
            col_a, col_b = st.columns(2)
            task_execution_time = col_a.text_input("Task execution time", placeholder="e.g. 30 mins, or -")
            completion_date = col_b.text_input("Completion Date", placeholder="Leave blank until completed")
            notes = st.text_area("Notes", placeholder="Optional")
            col_c, col_d = st.columns(2)
            assignee_team = col_c.text_input("Assignee Team")
            point_of_contact = col_d.text_input("Point Of Contact")
            status = st.selectbox("Status", CUTOVER_STATUS_OPTIONS)
            if st.form_submit_button("➕ Add step"):
                if not actions.strip():
                    st.warning("Actions is required -- it's the step's own description.")
                else:
                    final_package = integration_package_manual.strip() or (
                        integration_package_pick if integration_package_pick != "(type manually below)" else ""
                    )
                    st.session_state.cutover_checklist_entries.append({
                        "type": "step", "reference_artifact": reference_artifact.strip(),
                        "integration_package": final_package, "actions": actions.strip(),
                        "task_execution_time": task_execution_time.strip(), "notes": notes.strip(),
                        "assignee_team": assignee_team.strip(), "point_of_contact": point_of_contact.strip(),
                        "status": status, "completion_date": completion_date.strip(),
                    })

    if st.session_state.cutover_checklist_entries:
        st.caption(f"{len(st.session_state.cutover_checklist_entries)} entrie(s) so far:")
        step_no = 1
        for i, entry in enumerate(st.session_state.cutover_checklist_entries):
            row_cols = st.columns([1, 9, 1])
            if entry["type"] == "section":
                row_cols[0].write("—")
                row_cols[1].markdown(f"🟩 **Section: {entry['label']}**")
            else:
                row_cols[0].write(step_no)
                row_cols[1].write(entry["actions"][:100])
                step_no += 1
            if row_cols[2].button("✕", key=f"cutover_entry_remove_{i}", help="Remove this entry"):
                st.session_state.cutover_checklist_entries.pop(i)
                st.rerun()
    else:
        st.caption("No checklist entries added yet.")

    st.divider()

    # --- Config Params preview/fetch ---
    st.markdown("#### Config Params")
    if not selected_iflows:
        st.caption("Select at least one integration flow above to pull Config Params from.")
    else:
        env_name = st.session_state.get("env_name", "")
        target_col = env_name.strip().upper() if env_name.strip().upper() in EXCEL_ENV_COLUMNS else None
        if st.button("🔄 Load Config Params from selected iFlow(s)", key="cutover_load_config_params"):
            all_params = []
            seen_names = set()
            with st.spinner("Loading externalized parameters…"):
                for iflow in selected_iflows:
                    try:
                        params, _ = client.list_externalized_parameters(iflow["Id"], iflow["Version"])
                    except Exception as exc:  # noqa: BLE001
                        st.warning(f"Could not load parameters for {iflow['Name']}: {exc}")
                        continue
                    for p in params:
                        key = p.get("ParameterKey") or ""
                        if key in seen_names:
                            continue  # same parameter name shared across iFlows -- list it once
                        seen_names.add(key)
                        row = {"Parameter Name": key, "DEV": "", "QA": "", "PROD": ""}
                        if target_col:
                            row[target_col] = p.get("ParameterValue", "")
                        all_params.append(row)
            st.session_state.cutover_config_params = all_params

        config_params = st.session_state.get("cutover_config_params", [])
        if config_params:
            st.dataframe(pd.DataFrame(config_params), hide_index=True, width="stretch")
            st.caption(
                f"Values above came from the **{env_name or 'connected'}** environment this app is "
                f"currently connected to -- fill in the other two columns by hand before "
                f"generating if you need all three populated."
            )

    st.divider()

    # --- Generate ---
    if st.button("📥 Generate Cutover Checklist", type="primary", key="cutover_generate"):
        title = st.session_state.get("cutover_title", "").strip()
        if not title:
            st.error("Enter a checklist title first.")
        elif not st.session_state.cutover_checklist_entries:
            st.error("Add at least one checklist step first.")
        else:
            file_bytes = _build_cutover_checklist_excel(
                title, st.session_state.cutover_dependent_artifacts,
                st.session_state.cutover_checklist_entries, st.session_state.get("cutover_config_params", []),
            )
            st.success("✅ Cutover checklist generated.")
            st.download_button(
                "⬇️ Download the checklist (.xlsx)", data=file_bytes,
                file_name=f"{_safe_excel_sheet_name(title, set())}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="cutover_download",
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="SAP BTP-IS Cutover Automation & Management Platform", page_icon="🧩", layout="wide"
    )
    st.markdown(
        "<h1 style='text-align: center;'>🧩 SAP BTP-IS Cutover Automation &amp; Management Platform</h1>",
        unsafe_allow_html=True,
    )

    if not render_login_screen():
        return

    render_sidebar()

    client: Optional[SAPCPIClient] = st.session_state.get("client")
    if client is None:
        render_welcome()
        return

    tab_params, tab_security, tab_cutover = st.tabs(
        ["📦 Parameter Management", "🔒 Security Artifacts Management", "📝 Cutover Checklist"]
    )

    with tab_params:
        step = st.session_state.get("step", "pick_packages")
        step_labels = {"pick_packages": "Packages", "pick_iflows": "Integration Flows", "params": "Parameters"}
        crumb = f"**{st.session_state.get('env_name', '')}**  ›  {step_labels.get(step, step)}"
        st.caption(crumb)

        if step == "params":
            render_parameters_view(client)
        elif step == "pick_iflows":
            render_iflow_picker(client)
        else:
            render_package_picker(client)

    with tab_security:
        render_security_material_view(client)

    with tab_cutover:
        render_cutover_checklist_view(client)


if __name__ == "__main__":
    main()
