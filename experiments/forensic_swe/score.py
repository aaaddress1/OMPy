"""Score a TriageKit workspace: visible tests (per-step output) and hidden full-event checks (rule carry-over).

Run with the workspace as cwd: python score.py <spec.json>
"""

import importlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def visible_results() -> dict[str, bool]:
    output = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no", "-rA", "tests"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout
    return {
        line.split("::test_")[1].split()[0]: line.startswith("PASSED")
        for line in output.splitlines()
        if line.startswith(("PASSED", "FAILED")) and "::test_" in line
    }


def hidden_result(p: dict) -> dict:
    try:
        events = importlib.import_module(f"triagekit.parsers.{p['module']}").parse(p["lines"])
        got = [(e.ts_utc.isoformat(), e.host, e.account, e.action, e.source) for e in events]
    except Exception as e:  # noqa: BLE001 - any failure is a scoring outcome
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
    want = [(e["ts_utc"], e["host"], e["account"], e["action"], e["source"]) for e in p["expected"]]
    if got == want:
        return {"ok": True}
    missed = sorted(
        {
            rule
            for g, e in zip(got, p["expected"])
            for rule, bad in (
                ("R1", e["host"].startswith("TPE-") and g[0] == datetime.fromisoformat(e["raw_ts"]).isoformat()),
                ("R2", e["raw_account"] == "svc_bkp" and g[2] == "svc_bkp"),
                ("R3", e["raw_action"] != e["action"] and g[3] == e["raw_action"]),
            )
            if bad
        }
    )
    return {"ok": False, "missed_rules": missed, "error": "" if missed else "events differ"}


def main(spec_path: str) -> None:
    sys.path.insert(0, str(Path.cwd()))
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    visible = visible_results()
    rows = [
        {
            "pid": p["pid"],
            "rules": p["rules"],
            "discovers": p["discovered"],
            "visible": visible.get(p["pid"], False),
            **hidden_result(p),
        }
        for p in spec
    ]
    carry = [r for r in rows if r["rules"] and not r["discovers"]]
    print(
        json.dumps(
            {
                "visible_pass": sum(r["visible"] for r in rows),
                "hidden_pass": sum(r["ok"] for r in rows),
                "carry_over_pass": sum(r["ok"] for r in carry),
                "carry_over_total": len(carry),
                "parsers": rows,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main(sys.argv[1])
