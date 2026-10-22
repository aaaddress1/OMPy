"""Audit exactly the publishable source set; print filenames, never matched secret values."""

import argparse
import ipaddress
import re
import subprocess
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from dotenv import dotenv_values

from package_source import ROOT, source_files

PATTERNS = {
    "personal Windows path": re.compile(rb"[A-Za-z]:[\\/]+Users[\\/]+[^\s\"'\\/]+", re.I),
    "personal Unix path": re.compile(rb"/(?:Users|home)/(?!\.{1,3}/)[A-Za-z0-9_.-]+/"),
    "credential format": re.compile(rb"(?:sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{25,}|AKIA[0-9A-Z]{16})"),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "OAuth JWT": re.compile(rb"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}"),
}
PRIVATE_NETWORKS = [ipaddress.ip_network((int.from_bytes(bytes(octets), "big"), prefix))
                    for octets, prefix in (((10, 0, 0, 0), 8), ((172, 16, 0, 0), 12), ((192, 168, 0, 0), 16))]
IPV4 = re.compile(rb"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
PRIVATE_DIRS = {".git", ".tools", ".runtime", ".runs", ".config", ".bootstrap", "node_modules", ".venv"}


def findings(name: str, data: bytes, secrets: list[bytes]) -> list[str]:
    path = PurePosixPath(name)
    errors = [label for label, pattern in PATTERNS.items() if pattern.search(data)]
    for match in IPV4.findall(data):
        parts = [int(part) for part in match.split(b".")]
        if all(part <= 255 for part in parts) and any(ipaddress.ip_address(match.decode()) in net for net in PRIVATE_NETWORKS):
            errors.append("private network address")
            break
    if any(secret in data for secret in secrets):
        errors.append("current local credential")
    if PRIVATE_DIRS.intersection(path.parts) or path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
        errors.append("private runtime/config path")
    return sorted(set(errors))


def check(root: Path, archive: Path | None = None) -> None:
    secrets = []
    for file in (root / ".env", root / "mini-swe-agent/.env", root / "mini-swe-agent/.config/.env"):
        if file.exists():
            secrets += [value.encode() for key, value in dotenv_values(file, encoding="utf-8-sig").items()
                        if value and len(value) >= 8 and not value.startswith("replace-")
                        and any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))]
    if archive:
        with ZipFile(archive) as bundle:
            entries = [(name, bundle.read(name)) for name in bundle.namelist() if not name.endswith("/")]
    else:
        entries = [(file.relative_to(root).as_posix(), file.read_bytes()) for file in source_files(root)]
    errors = [(name, labels) for name, data in entries if (labels := findings(name, data, secrets))]
    if (root / ".git").exists():
        tracked = subprocess.check_output(["git", "-C", str(root), "ls-files", "--stage", "-z"], encoding="utf-8")
        for record in filter(None, tracked.split("\0")):
            metadata, name = record.split("\t", 1)
            labels = findings(name, b"", [])
            if metadata.startswith("160000 "):
                labels.append("embedded Git repository; publish the unpacked source ZIP instead")
            file = root / name
            if not labels and file.is_file():
                labels = findings(name, file.read_bytes(), secrets)
            if labels:
                errors.append((name, labels))
    for name, labels in errors:
        print(f"FAIL: {name}: {', '.join(labels)}")
    if errors:
        raise SystemExit(f"Release audit failed: {len(errors)} files. No matched values were printed.")
    print(f"Release audit passed: {len(entries)} files; no detected local credentials, personal paths or private hosts.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--zip", type=Path)
    args = parser.parse_args()
    check(args.root.resolve(), args.zip)
