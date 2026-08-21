from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
VERSION = "0.1.0"
INCLUDES = (
    "edgelink_agent",
    "deploy",
    "scripts",
    "docs",
    "config.example.json",
    "README.md",
    "pyproject.toml",
)


def main() -> int:
    DIST.mkdir(exist_ok=True)
    artifact = DIST / f"edgelink-agent-{VERSION}.tar.gz"
    with tarfile.open(artifact, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        build_info = json.dumps(
            {"version": VERSION, "commit": _git_commit()},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        info = tarfile.TarInfo(f"edgelink-agent-{VERSION}/BUILD_INFO.json")
        info.size = len(build_info)
        info.mode = 0o644
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        archive.addfile(info, io.BytesIO(build_info))
        for relative in INCLUDES:
            source = ROOT / relative
            if source.exists():
                archive.add(
                    source,
                    arcname=f"edgelink-agent-{VERSION}/{relative}",
                    filter=_bundle_filter,
                )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    checksum = DIST / "SHA256SUMS"
    checksum.write_text(f"{digest}  {artifact.name}\n", encoding="utf-8")
    print(f"{artifact}\nSHA-256: {digest}")
    return 0


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and len(commit) == 40 else "uncommitted"


def _bundle_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(info.name).parts
    if "__pycache__" in parts or info.name.endswith((".pyc", ".pyo")):
        return None
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    executable = (
        "/scripts/" in info.name
        or info.name.endswith("/deploy/run_agent.py")
        or info.name.endswith("/deploy/edgelinkctl")
        or info.name.endswith("/deploy/90-edgelink-agent")
    )
    info.mode = 0o755 if info.isdir() or executable else 0o644
    return info


if __name__ == "__main__":
    raise SystemExit(main())
