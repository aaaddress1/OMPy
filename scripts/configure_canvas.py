"""Generate machine-local launch paths from portable, secret-free defaults."""

import argparse
import json
import os
import shlex
import sys
from getpass import getpass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def configure(root: Path, python: Path) -> None:
    state = root / ".runtime" / "openhands"
    profiles = {}
    for name, legacy_name, policy in [("OMPy", "mini-swe-agent", None), ("OMPy-append-32k", "mswe-append-32k", "append"),
                                      ("OMPy-sliding-32k", "mswe-sliding-32k", "sliding"), ("OMPy-memory-32k", "mswe-memory-32k", "memory")]:
        args = ["-m", "minisweagent.run.windows_acp", "--step-limit", "200" if policy else "32",
                "--command-timeout", "120"]
        if policy:
            args += ["--context-window", "32768", "--context-policy", policy, "--reasoning", "low"]
        profile = {
            "schema_version": 4, "agent_kind": "acp",
            # Preserve managed profile IDs so existing conversations keep their references.
            "id": str(uuid5(NAMESPACE_URL, "tiny-harness/" + legacy_name)), "name": name, "revision": 0,
            "acp_server": "custom", "acp_prompt_timeout": 1800.0, "acp_startup_timeout": 90.0,
            "acp_command": shlex.join([python.as_posix()]), "acp_args": args,
        }
        write_json(state / "agent-profiles" / f"{name}.json", profile)
        legacy = state / "agent-profiles" / f"{legacy_name}.json"
        if legacy.exists() and json.loads(legacy.read_text(encoding="utf-8"))["id"] == profile["id"]:
            legacy.unlink()
        profiles[name] = profile
    path = state / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {"schema_version": 6}
    profile = profiles["OMPy"]
    # This launcher always uses our ACP agent; its LLM comes from the project's .env.
    settings["agent_settings"] = {
        "schema_version": 9, "agent_kind": "acp", "acp_server": "custom",
        "acp_command": [python.as_posix()], "acp_args": profile["acp_args"],
        "acp_prompt_timeout": 1800.0, "acp_startup_timeout": 90.0,
    }
    settings["active_agent_profile_id"] = profile["id"]
    settings["active_profile"] = None
    settings["active_meta_profile"] = None
    write_json(path, settings)
    workspace_file = state / "workspaces.json"
    workspaces = json.loads(workspace_file.read_text(encoding="utf-8-sig")) if workspace_file.exists() else {
        "schema_version": 1, "workspaces": [], "workspaceParents": []
    }
    marker = state / "project-root.json"
    previous = json.loads(marker.read_text(encoding="utf-8"))["path"] if marker.exists() else None
    workspaces["workspaces"] = [w for w in workspaces["workspaces"] if w["path"] not in {previous, str(root)}]
    workspaces["workspaces"].append({"id": str(root), "name": root.name, "path": str(root)})
    write_json(workspace_file, workspaces)
    write_json(marker, {"path": str(root)})


def check_env(root: Path, *, base_url: str | None = None, model: str | None = None,
              local_api: bool = False, force: bool = False) -> None:
    from dotenv import dotenv_values, set_key

    defaults = root / ".env.example"
    legacy = root / "mini-swe-agent/.env"
    path = legacy if legacy.exists() else root / ".env"
    settings = dotenv_values(defaults, encoding="utf-8-sig")
    for file in (root / ".env", legacy, root / "mini-swe-agent/.config/.env"):
        if file.exists():
            settings.update(dotenv_values(file, encoding="utf-8-sig"))
    stored_base = settings.get("HARNESS_BASE_URL", "")
    settings.update({name: value for name, value in os.environ.items() if name.startswith("HARNESS_")})
    base = base_url if base_url is not None else settings.get("HARNESS_BASE_URL", "")
    name = model if model is not None else settings.get("HARNESS_MODEL", "")
    key = "local" if local_api else settings.get("HARNESS_API_KEY", "")
    missing_key = not key or key == "replace-with-host-api-key"
    missing_model = not name or name == "your-model-name"
    missing_base = not base
    changed_origin = base_url is not None and urlsplit(base or "").netloc.lower() != urlsplit(stored_base or "").netloc.lower()
    env_key = os.getenv("HARNESS_API_KEY", "")
    explicit_key = bool(env_key and env_key != "replace-with-host-api-key")
    ask_key = not local_api and (missing_key or ((force or changed_origin) and not explicit_key))
    if force or missing_key or missing_model or missing_base or ask_key:
        print("Configure the OpenAI-compatible model API used by mini-swe (Chat Completions with tools).")
        if base_url is None:
            base = input(f"API base URL [{base or ''}]: ").strip() or base
        if model is None:
            name = input(f"Model [{name or ''}]: ").strip() or name
        if ask_key:
            if not sys.stdin.isatty():
                raise ValueError("API key required: run interactively, provide HARNESS_API_KEY, or use --local-api for an unauthenticated API.")
            prompt = "API key for this endpoint (hidden): " if changed_origin else "API key (hidden; Enter keeps the existing key): "
            entered = getpass(prompt).strip()
            key = entered or ("" if missing_key or changed_origin else key)
    url = urlsplit(base or "")
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("API base URL must be http(s), without embedded credentials, query or fragment.")
    if not name or name == "your-model-name" or not key or key == "replace-with-host-api-key" or "\n" in key or "\r" in key:
        raise ValueError("Set a model name and API key; use --local-api only for an API without authentication.")
    explicit = force or base_url is not None or model is not None or local_api
    if explicit or missing_key or missing_model or missing_base:
        if not path.exists():
            path.write_bytes(defaults.read_bytes())
        for variable, value in {"HARNESS_BASE_URL": base.rstrip("/"), "HARNESS_MODEL": name, "HARNESS_API_KEY": key}.items():
            set_key(str(path), variable, value, encoding="utf-8-sig")
        print(f"Model API saved in {path.relative_to(root)}. API key hidden; this file is excluded from Git and source ZIPs.")
    else:
        print("Existing model API configuration preserved. API key hidden.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--check-env", action="store_true")
    parser.add_argument("--configure-api", action="store_true")
    parser.add_argument("--api-base-url")
    parser.add_argument("--model")
    parser.add_argument("--local-api", action="store_true")
    args = parser.parse_args()
    if args.check_env or args.configure_api or args.api_base_url is not None or args.model is not None or args.local_api:
        check_env(args.root.resolve(), base_url=args.api_base_url, model=args.model, local_api=args.local_api, force=args.configure_api)
    elif args.python:
        configure(args.root.resolve(), args.python.resolve())
    else:
        parser.error("--python or --check-env is required")
