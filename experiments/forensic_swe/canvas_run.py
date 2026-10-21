"""Send the TriageKit task through OpenHands Canvas to the mini-swe engine, once per context policy, then score.

  python canvas_run.py append sliding memory

Each policy uses Canvas profile mswe-<policy>-32k and its own workspace canvas/<policy>/.
The conversations show up in the Canvas Web UI while they run.
"""

import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from generate import build_spec

HERE = Path(__file__).resolve().parent
SERVER = "http://127.0.0.1:18000"
KEY = (HERE.parents[1] / ".runtime" / "openhands" / "agent-canvas" / "api-key.txt").read_text().strip()
RUNS = HERE.parents[1] / "mini-swe-agent" / ".runs"


def api(method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        SERVER + path, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"X-Session-API-Key": KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read() or b"{}")


def engine_run(started: float) -> dict:
    """Summarize the mini-swe session (.runs/<id>) created by this conversation."""
    sessions = [p for p in RUNS.iterdir() if p.stat().st_ctime >= started]
    if not sessions:
        return {}
    session = max(sessions, key=lambda p: p.stat().st_ctime)
    trajectory = json.loads((session / "turn-0001" / "trajectory.traj.json").read_text(encoding="utf-8"))
    messages = trajectory["messages"]
    prompts = [
        m["extra"]["response"]["usage"]["prompt_tokens"]
        for m in messages if (m.get("extra") or {}).get("response", {}).get("usage")
    ]
    notes = session / "notes.md"
    return {
        "session": str(session),
        "exit_status": (messages[-1].get("extra") or {}).get("exit_status"),
        "model_calls": len(prompts),
        "max_prompt_tokens": max(prompts, default=0),
        "notes": notes.read_text(encoding="utf-8-sig").splitlines() if notes.exists() else [],
        "final": (messages[-1].get("content") or "")[:500],
    }


def score(workspace: Path) -> dict:
    spec = Path(tempfile.mkdtemp()) / "spec.json"
    spec.write_text(json.dumps(build_spec(seed=1)), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(HERE / "score.py"), str(spec)],
        cwd=workspace, capture_output=True, text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def main(*policies: str) -> None:
    ids = {p["name"]: p["id"] for p in api("GET", "/api/agent-profiles")["profiles"]}
    for policy in policies:
        workspace = HERE / "canvas" / policy
        started = time.time()
        conversation = api("POST", "/api/conversations", {
            "workspace": {"working_dir": str(workspace)},
            "agent_profile_id": ids[f"mswe-{policy}-32k"],
            "initial_message": {
                "role": "user",
                "content": [{"type": "text", "text": (workspace / "TASK.md").read_text(encoding="utf-8")}],
                "run": True,
            },
        })
        print(json.dumps({"policy": policy, "conversation": conversation["id"], "started": time.strftime("%H:%M:%S")}),
              flush=True)
        status = "?"
        while status not in {"finished", "error", "stuck", "paused"}:
            time.sleep(30)
            status = api("GET", f"/api/conversations/{conversation['id']}").get("execution_status", "?")
        result = score(workspace)
        report = {
            "policy": policy,
            "canvas_status": status,
            "minutes": round((time.time() - started) / 60, 1),
            "engine": engine_run(started),
            "visible_pass": result["visible_pass"],
            "hidden_pass": result["hidden_pass"],
            "carry_over": f"{result['carry_over_pass']}/{result['carry_over_total']}",
            "failing": [
                (r["pid"], r.get("missed_rules") or r.get("error", "")) for r in result["parsers"]
                if not (r["visible"] and r["ok"])
            ],
        }
        (HERE / "results").mkdir(exist_ok=True)
        (HERE / "results" / f"{policy}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
