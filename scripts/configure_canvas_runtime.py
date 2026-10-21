"""Enable OMPy's branding and live-thought adapter in its own Canvas installation."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = "# OMPy: live ACP thoughts"
LEGACY_MARKER = "# tiny_harness: live ACP thoughts"
HOOK = "\n" + MARKER + "\nfrom minisweagent.run.openhands_compat import enable_live_thoughts\nenable_live_thoughts()\n"
BRAND = '''<script data-ompy-brand>
window.addEventListener("load", () => {
  const sync = () => { if (document.title !== "Oh My PiPy (OMPy)") document.title = "Oh My PiPy (OMPy)"; };
  new MutationObserver(sync).observe(document.head, {childList: true, subtree: true, characterData: true});
  sync();
});
</script>'''


def configure(canvas: Path) -> None:
    entry = canvas / "node_modules/@openhands/agent-canvas/tools/canvas_ui_tool.py"
    original = entry.read_text(encoding="utf-8")
    source = original.replace(LEGACY_MARKER, MARKER)
    if MARKER not in source:
        source += HOOK
    if source != original:
        entry.write_text(source, encoding="utf-8")
    index = canvas / "node_modules/@openhands/agent-canvas/build/index.html"
    html = index.read_text(encoding="utf-8")
    if "data-ompy-brand" not in html:
        index.write_text(html.replace("</head>", BRAND + "</head>"), encoding="utf-8")


if __name__ == "__main__":
    configure(ROOT / ".tools/canvas-runtime")
