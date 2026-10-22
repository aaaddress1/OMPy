"""Run PowerShell through an argv list and preserve complete command output."""

import json
import shutil
from pathlib import Path
from uuid import uuid4

from minisweagent.context_store import preview
from minisweagent.environments.local import LocalEnvironment


class PowerShellEnvironment(LocalEnvironment):
    def __init__(self, *, output_dir: Path, shell: str | None = None, context=None, **kwargs):
        super().__init__(**kwargs)
        self.output_dir = output_dir
        self.context = context
        self.shell = shutil.which(shell) if shell else shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not self.shell:
            raise FileNotFoundError("找不到 pwsh.exe 或 powershell.exe。")

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict:
        if "context" in action:
            result = self.context.store.tool(action["context"])
            return {"output": json.dumps(result, ensure_ascii=False), "returncode": int("error" in result),
                    "exception_info": "", "archive": str(self.context.store.path)}
        self.output_dir.mkdir(parents=True, exist_ok=True)
        call_id = uuid4().hex
        script_path = (self.output_dir / f"tool-{call_id}.ps1").resolve()
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            "$ProgressPreference = 'SilentlyContinue'\n"
            "$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
            + action["command"]
            + "\nif ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }"
        )
        # Windows PowerShell 5.1 needs a BOM for UTF-8 source; -File avoids its EncodedCommand CLIXML errors.
        script_path.write_text(script, encoding="utf-8-sig")
        output = super().execute(
            action
            | {
                "command": [
                    self.shell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-OutputFormat",
                    "Text",
                    "-File",
                    str(script_path),
                ]
            },
            cwd=cwd,
            timeout=timeout,
        )
        archive = self.output_dir / f"tool-{call_id}.json"
        archive.write_text(
            json.dumps({"command": action["command"], "script": str(script_path), **output}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        ref = self.context.store.offload(output["output"]) if self.context else None
        return output | {
            "output": preview(output["output"]),
            "archive": str(archive.resolve()),
            "truncated": len(output["output"].encode("utf-8")) > 6000,
            **({"ref": ref} if ref else {}),
        }

    def _check_finished(self, output: dict) -> None:
        # This console ends a turn on a final model answer, not a shell marker.
        pass
