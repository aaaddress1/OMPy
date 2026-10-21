"""Check the local UI, generated ACP profiles and conversation creation; no model calls."""

import argparse
import shlex
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def check(url: str, timeout: int) -> None:
    key_file = ROOT / ".runtime" / "openhands" / "agent-canvas" / "api-key.txt"
    with httpx.Client(base_url=url, trust_env=False, timeout=10) as client:
        deadline = time.monotonic() + timeout
        while True:
            try:
                client.headers["X-Session-API-Key"] = key_file.read_text().strip()
                client.get("/server_info").raise_for_status()
                page = client.get("/canvas")
                page.raise_for_status()
                assert "window.__AGENT_CANVAS_DO_NOT_TRACK__=true;" in page.text
                assert "data-ompy-brand" in page.text
                break
            except (OSError, httpx.HTTPError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Canvas did not become ready; inspect .runtime/openhands/agent-canvas/logs.")
                time.sleep(1)
        response = client.get("/api/agent-profiles")
        response.raise_for_status()
        profiles = {p["name"]: p for p in response.json()["profiles"]}
        assert {"OMPy", "OMPy-append-32k", "OMPy-sliding-32k", "OMPy-memory-32k"} <= profiles.keys()
        profile = profiles["OMPy"]
        assert response.json()["active_agent_profile_id"] == profile["id"]
        settings = client.get("/api/settings")
        settings.raise_for_status()
        assert settings.json()["agent_settings"]["agent_kind"] == "acp"
        detail = client.get("/api/agent-profiles/OMPy")
        detail.raise_for_status()
        assert shlex.split(detail.json()["profile"]["acp_command"]) == [(ROOT / ".tools/agent-env/Scripts/python.exe").as_posix()]
        response = client.post("/api/conversations", json={
            "workspace": {"working_dir": str(ROOT)},
            "agent_profile_id": response.json()["active_agent_profile_id"],
        })
        response.raise_for_status()
        conversation_id = response.json()["id"]
        assert response.json()["agent"]["acp_command"][0] == (ROOT / ".tools/agent-env/Scripts/python.exe").as_posix()
        # No initial_message/run means this checks launch configuration without invoking the LLM.
        client.delete(f"/api/conversations/{conversation_id}").raise_for_status()
        print("Canvas UI, four local ACP profiles and conversation creation: passed (no model calls).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18080")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    check(args.url, args.timeout)
