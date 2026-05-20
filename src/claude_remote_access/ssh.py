"""SSH transport: shells out to the user's local ssh/scp.

Picks up ~/.ssh/config, keys, agent, jump hosts. Uses a ControlMaster socket
under CLAUDE_PLUGIN_DATA to multiplex connections so per-call overhead drops
from ~100ms to a few ms once the first call has opened the master.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Windows OpenSSH does not support AF_UNIX ControlMaster sockets
# (the multiplexer reports "getsockname failed: Not a socket" and every
# call fails). Disable the multiplexer there; users can also force-disable
# it elsewhere via CLAUDE_REMOTE_NO_CONTROLMASTER=1.
_USE_CONTROL_MASTER = (
    sys.platform != "win32"
    and not os.environ.get("CLAUDE_REMOTE_NO_CONTROLMASTER")
)


class RemoteError(RuntimeError):
    """Raised when an SSH/scp invocation returns a non-zero exit code."""

    def __init__(self, returncode: int, stdout: str, stderr: str, cmdline: list[str]):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.cmdline = cmdline
        super().__init__(
            f"remote command failed (exit {returncode}): {stderr.strip() or stdout.strip()}"
        )


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str


def _control_dir() -> Path:
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    base = Path(data) if data else Path.home() / ".cache" / "claude-remote-access"
    d = base / "controlmasters"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _control_path_for(host: str) -> Path:
    digest = hashlib.sha1(host.encode()).hexdigest()[:16]
    return _control_dir() / f"cm-{digest}.sock"


def _common_ssh_opts(host: str) -> list[str]:
    opts: list[str] = []
    if _USE_CONTROL_MASTER:
        cp = str(_control_path_for(host))
        opts += [
            "-o", f"ControlPath={cp}",
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=10m",
        ]
    opts += [
        "-o", "ServerAliveInterval=30",
        "-o", "BatchMode=yes",
    ]
    return opts


def _scp_control_opts(host: str) -> list[str]:
    if not _USE_CONTROL_MASTER:
        return []
    cp = str(_control_path_for(host))
    return [
        "-o", f"ControlPath={cp}",
        "-o", "ControlMaster=auto",
    ]


def resolve_host(host: str | None) -> str:
    if host:
        return host
    env_host = os.environ.get("CLAUDE_REMOTE_HOST")
    if env_host:
        return env_host
    raise RemoteError(
        2,
        "",
        "no host specified and CLAUDE_REMOTE_HOST is not set",
        [],
    )


async def _run(cmdline: list[str], *, stdin: bytes | None = None, timeout: float | None = None) -> Result:
    proc = await asyncio.create_subprocess_exec(
        *cmdline,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=stdin), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RemoteError(
            124,
            "",
            f"timed out after {timeout}s: {' '.join(shlex.quote(a) for a in cmdline)}",
            cmdline,
        )
    return Result(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
    )


async def run_command(
    command: str,
    *,
    host: str | None = None,
    timeout: float | None = 120.0,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> Result:
    """Run a shell command on the remote host via ssh.

    The command is wrapped in `sh -c` on the remote side. cwd and env are
    applied via shell prefixes so they work without any remote helper.
    """
    target = resolve_host(host)
    prefix_parts: list[str] = []
    if env:
        for k, v in env.items():
            prefix_parts.append(f"export {shlex.quote(k)}={shlex.quote(v)};")
    if cwd:
        prefix_parts.append(f"cd {shlex.quote(cwd)} &&")
    remote_cmd = " ".join(prefix_parts + [command])

    cmdline = ["ssh", *_common_ssh_opts(target), target, "--", "sh", "-c", remote_cmd]
    result = await _run(cmdline, timeout=timeout)
    if check and result.returncode != 0:
        raise RemoteError(result.returncode, result.stdout, result.stderr, cmdline)
    return result


async def read_file(path: str, *, host: str | None = None, timeout: float | None = 60.0) -> str:
    target = resolve_host(host)
    cmdline = ["ssh", *_common_ssh_opts(target), target, "--", "cat", "--", path]
    result = await _run(cmdline, timeout=timeout)
    if result.returncode != 0:
        raise RemoteError(result.returncode, result.stdout, result.stderr, cmdline)
    return result.stdout


async def write_file(
    path: str,
    content: str,
    *,
    host: str | None = None,
    timeout: float | None = 120.0,
    make_parents: bool = True,
) -> None:
    target = resolve_host(host)
    parts: list[str] = []
    if make_parents:
        parts.append(f"mkdir -p {shlex.quote(os.path.dirname(path) or '.')} &&")
    parts.append(f"cat > {shlex.quote(path)}")
    remote_cmd = " ".join(parts)
    cmdline = ["ssh", *_common_ssh_opts(target), target, "--", "sh", "-c", remote_cmd]
    result = await _run(cmdline, stdin=content.encode("utf-8"), timeout=timeout)
    if result.returncode != 0:
        raise RemoteError(result.returncode, result.stdout, result.stderr, cmdline)


async def scp_put(
    local_path: str,
    remote_path: str,
    *,
    host: str | None = None,
    recursive: bool = False,
    timeout: float | None = 600.0,
) -> None:
    target = resolve_host(host)
    flags = ["-r"] if recursive else []
    cmdline = [
        "scp",
        *_scp_control_opts(target),
        "-o", "BatchMode=yes",
        *flags,
        local_path,
        f"{target}:{remote_path}",
    ]
    result = await _run(cmdline, timeout=timeout)
    if result.returncode != 0:
        raise RemoteError(result.returncode, result.stdout, result.stderr, cmdline)


async def scp_get(
    remote_path: str,
    local_path: str,
    *,
    host: str | None = None,
    recursive: bool = False,
    timeout: float | None = 600.0,
) -> None:
    target = resolve_host(host)
    flags = ["-r"] if recursive else []
    cmdline = [
        "scp",
        *_scp_control_opts(target),
        "-o", "BatchMode=yes",
        *flags,
        f"{target}:{remote_path}",
        local_path,
    ]
    result = await _run(cmdline, timeout=timeout)
    if result.returncode != 0:
        raise RemoteError(result.returncode, result.stdout, result.stderr, cmdline)


async def close_master(host: str | None = None) -> None:
    """Tear down the persistent control socket for a host."""
    target = resolve_host(host)
    if not _USE_CONTROL_MASTER:
        return
    cmdline = [
        "ssh", "-o", f"ControlPath={_control_path_for(target)}",
        "-O", "exit", target,
    ]
    await _run(cmdline, timeout=5.0)


def quote_iter(parts: Iterable[str]) -> str:
    return " ".join(shlex.quote(p) for p in parts)
