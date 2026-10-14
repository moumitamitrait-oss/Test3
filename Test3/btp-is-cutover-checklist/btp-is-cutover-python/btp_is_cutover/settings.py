"""Remembered settings (never the client secret)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

KEYS = ["base_url", "token_url", "client_id", "env", "poc", "team_is", "team_func", "team_sec", "out_dir"]
DEFAULTS = {"env": "QA", "team_is": "BTP-IS Team", "team_func": "Functional Team", "team_sec": "Security Team"}


def settings_path() -> Path:
    return Path.home() / ".btp_is_cutover" / "settings.json"


def load_settings() -> Dict[str, Any]:
    data = dict(DEFAULTS)
    try:
        data.update({k: v for k, v in json.loads(settings_path().read_text(encoding="utf-8")).items() if k in KEYS})
    except (OSError, ValueError):
        pass
    return data


def save_settings(values: Dict[str, Any]) -> None:
    try:
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({k: values.get(k, "") for k in KEYS}, indent=2), encoding="utf-8")
    except OSError:
        pass  # not being able to remember settings is not worth failing for
