"""Command line: list packages / iFlows, or create the cutover workbooks from a JSON file (no window needed)."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .api import ApiError
from .detect import hint_for  # noqa: F401  (kept importable for scripts)
from .models import Dep
from .service import analyze, generate, validate_details
from .tenant import SelectedFlow, Tenant, TYPE_LABELS


def _connect(args, cfg: Optional[Dict[str, Any]] = None, session=None) -> Tenant:
    api = (cfg or {}).get("api", {})
    base = args.base_url or api.get("base_url") or os.environ.get("BTP_IS_BASE_URL", "")
    token = args.token_url or api.get("token_url") or os.environ.get("BTP_IS_TOKEN_URL", "")
    cid = args.client_id or api.get("client_id") or os.environ.get("BTP_IS_CLIENT_ID", "")
    secret = os.environ.get("BTP_IS_CLIENT_SECRET") or (getpass.getpass("Client secret: ") if sys.stdin.isatty() else "")
    if not (base and token and cid and secret):
        raise ApiError("API base URL, token URL, client ID and client secret are required "
                       "(options, config file or BTP_IS_* environment variables; the secret only via BTP_IS_CLIENT_SECRET or prompt).")
    return Tenant.connect(base, token, cid, secret, session=session)


def _add_connect(p: argparse.ArgumentParser) -> None:
    p.add_argument("--base-url", help="the url value of the service key")
    p.add_argument("--token-url", help="the tokenurl value of the service key")
    p.add_argument("--client-id", help="the clientid value of the service key")


def cmd_packages(args, session=None) -> int:
    for p in _connect(args, session=session).packages:
        print(f"{p['Id']}\t{p['Name']}")
    return 0


def cmd_iflows(args, session=None) -> int:
    tenant = _connect(args, session=session)
    wanted = args.package.lower()
    pkg = next((p for p in tenant.packages if wanted in (p["Id"].lower(), p["Name"].lower())), None) or \
        next((p for p in tenant.packages if wanted in (p["Id"] + " " + p["Name"]).lower()), None)
    if not pkg:
        print(f"No package found for '{args.package}'.", file=sys.stderr)
        return 1
    for f in tenant.list_flows(pkg["Id"]):
        print(f"{f['Id']}\t{f['Name']}\tv{f['Version']}")
    return 0


def cmd_create(args, session=None) -> int:
    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    flows_cfg: List[Dict[str, str]] = cfg.get("iflows") or []
    if not flows_cfg:
        print("The config file has no 'iflows'.", file=sys.stderr)
        return 2
    tenant = _connect(args, cfg, session)
    selected: List[SelectedFlow] = []
    for item in flows_cfg:
        f = tenant.resolve_iflow_by_id(item["id"], print)
        f.interface_id = (item.get("interface_id") or "").strip()
        selected.append(f)
    missing = validate_details(selected, cfg.get("poc", ""))
    if missing:
        print("Please provide " + " and ".join(missing) + " in the config file.", file=sys.stderr)
        return 2

    analyses = analyze(tenant, selected, lambda m: print("  " + m))
    for f in selected:
        a = analyses[f.key]
        if not a.data:
            continue
        for extra in cfg.get("extra_dependencies") or []:
            a.result.deps.append(Dep(pkg=extra.get("package", ""), artifact=extra["artifact"],
                                     system=extra.get("type", ""), source="Config file"))
        drop = set(cfg.get("exclude_dependencies") or [])
        for d in a.result.deps:
            if d.artifact in drop:
                d.include = False
        print(f"\n{f.name} ({f.interface_id}): dependencies")
        for d in a.result.deps:
            print(f"  [{'x' if d.include else ' '}] {d.pkg or 'N/A'} | {d.artifact or '-'} | {d.system} ({d.source})")
        for n in a.result.notes:
            print("  " + n)

    opts = {"env": cfg.get("env") or "QA", "poc": cfg.get("poc"), "team_is": cfg.get("team_is"),
            "team_func": cfg.get("team_func"), "team_sec": cfg.get("team_sec")}
    out_dir = args.out or cfg.get("output_dir") or "output"
    paths, notes = generate(analyses, selected, opts, out_dir)
    print()
    for p in paths:
        print("Created", os.path.abspath(p))
    for n in notes:
        print("Note:", n)
    return 0 if paths else 1


def main(argv: Optional[List[str]] = None, session=None) -> int:
    parser = argparse.ArgumentParser(prog="btp_is_cutover", description="BTP IS Cutover Wizard (Python)")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="open the wizard window (default)")
    p = sub.add_parser("packages", help="list the packages of the tenant")
    _add_connect(p)
    p = sub.add_parser("iflows", help="list the iFlows of a package")
    _add_connect(p)
    p.add_argument("--package", required=True, help="package ID or name")
    p = sub.add_parser("create", help="create the cutover workbooks described in a JSON config file")
    _add_connect(p)
    p.add_argument("--config", required=True)
    p.add_argument("--out", help="output folder (default: output_dir of the config, or ./output)")
    args = parser.parse_args(argv)
    try:
        if args.cmd in (None, "gui"):
            from .gui import run_gui
            run_gui()
            return 0
        return {"packages": cmd_packages, "iflows": cmd_iflows, "create": cmd_create}[args.cmd](args, session)
    except ApiError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
