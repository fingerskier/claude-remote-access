"""Bootstrap launcher for the claude-remote-access MCP server.

Claude Code installs plugins by extracting files into a cache directory but does
not run any install step. This launcher creates an isolated venv under
${CLAUDE_PLUGIN_DATA}, installs the plugin into it on first run (and on every
plugin update, detected via a content hash), and then execs the real server.

All progress / error output goes to stderr so it cannot corrupt the MCP stdio
JSON-RPC stream on stdout.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import venv
from pathlib import Path


def log(msg: str) -> None:
    print(f"[claude-remote-access launcher] {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> "None":
    log(f"ERROR: {msg}")
    sys.exit(code)


def resolve_env_dirs() -> tuple[Path, Path]:
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if not root:
        die("CLAUDE_PLUGIN_ROOT is not set; launcher must be invoked by Claude Code's MCP host.")
    if not data:
        die("CLAUDE_PLUGIN_DATA is not set; launcher must be invoked by Claude Code's MCP host.")
    return Path(root).resolve(), Path(data).resolve()


def venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def venv_entrypoint(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "claude-remote-access.exe"
    return venv_dir / "bin" / "claude-remote-access"


def ensure_venv(venv_dir: Path) -> None:
    if venv_python(venv_dir).exists():
        return
    log(f"creating venv at {venv_dir}")
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    venv.EnvBuilder(with_pip=True, clear=False, upgrade=False).create(venv_dir)


def compute_source_hash(root: Path) -> str:
    h = hashlib.sha256()
    pyproject = root / "pyproject.toml"
    h.update(pyproject.read_bytes())
    src = root / "src"
    if src.is_dir():
        for path in sorted(src.rglob("*")):
            if path.is_file():
                rel = path.relative_to(root).as_posix().encode()
                h.update(b"\0")
                h.update(rel)
                h.update(b"\0")
                h.update(path.read_bytes())
    return h.hexdigest()


def install_if_stale(root: Path, data: Path, venv_dir: Path) -> None:
    hash_file = data / "installed.hash"
    current = compute_source_hash(root)
    previous = hash_file.read_text().strip() if hash_file.exists() else ""
    if current == previous and venv_entrypoint(venv_dir).exists():
        return
    log("installing plugin into venv (first run or plugin updated)")
    try:
        subprocess.check_call(
            [str(venv_python(venv_dir)), "-m", "pip", "install", "--quiet",
             "--upgrade", "--disable-pip-version-check", str(root)],
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
    except subprocess.CalledProcessError as e:
        die(f"pip install failed with exit code {e.returncode}")
    hash_file.write_text(current)
    log("install complete")


def main() -> None:
    root, data = resolve_env_dirs()
    data.mkdir(parents=True, exist_ok=True)
    venv_dir = data / "venv"
    ensure_venv(venv_dir)
    install_if_stale(root, data, venv_dir)

    entry = venv_entrypoint(venv_dir)
    if not entry.exists():
        die(f"server entrypoint not found at {entry} after install")

    argv = [str(entry), *sys.argv[1:]]
    if os.name == "nt":
        result = subprocess.run(argv)
        sys.exit(result.returncode)
    os.execv(str(entry), argv)


if __name__ == "__main__":
    main()
