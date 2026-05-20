"""MCP server exposing remote filesystem and shell operations over SSH.

Each tool resolves its target host in this order:
1. The ``host`` argument on the tool call (if provided).
2. The session-default set via ``remote_set_host``.
3. The ``CLAUDE_REMOTE_HOST`` environment variable.

The transport shells out to the local ``ssh``/``scp`` so the user's existing
SSH config, agent, keys, and jump hosts work without extra plumbing.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import ssh

server = Server("claude-remote-access")

_session_host: str | None = None


def _resolve(host_arg: str | None) -> str:
    return ssh.resolve_host(host_arg or _session_host)


def _format_lines(text: str, *, offset: int = 0, limit: int | None = None) -> str:
    lines = text.splitlines()
    end = len(lines) if limit is None else min(len(lines), offset + limit)
    out: list[str] = []
    for i in range(offset, end):
        out.append(f"{i + 1:6}\t{lines[i]}")
    return "\n".join(out)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="remote_set_host",
            description=(
                "Set the default SSH host for this session. Accepts user@host, "
                "a Host alias from ~/.ssh/config, or host:port. Persists for the "
                "lifetime of the MCP server process."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "ssh target (user@host or config alias)"},
                },
                "required": ["host"],
            },
        ),
        Tool(
            name="remote_info",
            description="Probe the remote host: uname, architecture, distro, disk free.",
            inputSchema={
                "type": "object",
                "properties": {"host": {"type": "string"}},
            },
        ),
        Tool(
            name="remote_bash",
            description=(
                "Run a shell command on the remote host via ssh and return stdout, "
                "stderr, and exit code. Use this for builds, tests, package installs, "
                "and anything you would normally run in a terminal on the device."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "shell command to run"},
                    "host": {"type": "string"},
                    "cwd": {"type": "string", "description": "remote working directory"},
                    "timeout": {"type": "number", "description": "seconds (default 120)", "default": 120},
                    "env": {
                        "type": "object",
                        "description": "extra environment variables for the command",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["command"],
            },
        ),
        Tool(
            name="remote_read",
            description="Read a remote file with cat-n style line numbers. Supports offset+limit for large files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "absolute remote path"},
                    "host": {"type": "string"},
                    "offset": {"type": "integer", "description": "first line index (0-based)", "default": 0},
                    "limit": {"type": "integer", "description": "max lines to return"},
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="remote_write",
            description="Write (overwrite) a file on the remote host. Creates parent directories.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "host": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        ),
        Tool(
            name="remote_edit",
            description=(
                "Exact-string replace inside a remote file. By default the match must "
                "be unique; pass replace_all=true to replace every occurrence."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean", "default": False},
                    "host": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        ),
        Tool(
            name="remote_ls",
            description="List a remote directory (`ls -lA`).",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "host": {"type": "string"},
                },
            },
        ),
        Tool(
            name="remote_glob",
            description=(
                "Find files matching a glob pattern, like `**/*.py`. Returns paths sorted "
                "by modification time (newest first), capped at 200 entries."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "cwd": {"type": "string", "description": "directory to search from", "default": "."},
                    "host": {"type": "string"},
                },
                "required": ["pattern"],
            },
        ),
        Tool(
            name="remote_grep",
            description=(
                "Recursive grep on the remote host (uses ripgrep if present, else grep -rn). "
                "Returns matching lines with file:line prefixes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "glob": {"type": "string", "description": "optional filename filter, e.g. *.py"},
                    "case_insensitive": {"type": "boolean", "default": False},
                    "max_results": {"type": "integer", "default": 200},
                    "host": {"type": "string"},
                },
                "required": ["pattern"],
            },
        ),
        Tool(
            name="remote_put",
            description="Copy a local file or directory to the remote host (scp).",
            inputSchema={
                "type": "object",
                "properties": {
                    "local_path": {"type": "string"},
                    "remote_path": {"type": "string"},
                    "recursive": {"type": "boolean", "default": False},
                    "host": {"type": "string"},
                },
                "required": ["local_path", "remote_path"],
            },
        ),
        Tool(
            name="remote_get",
            description="Copy a file or directory from the remote host to the local machine (scp).",
            inputSchema={
                "type": "object",
                "properties": {
                    "remote_path": {"type": "string"},
                    "local_path": {"type": "string"},
                    "recursive": {"type": "boolean", "default": False},
                    "host": {"type": "string"},
                },
                "required": ["remote_path", "local_path"],
            },
        ),
        Tool(
            name="remote_disconnect",
            description="Close the persistent SSH control socket for a host. Subsequent calls re-open it.",
            inputSchema={
                "type": "object",
                "properties": {"host": {"type": "string"}},
            },
        ),
    ]


def _text(s: str) -> list[TextContent]:
    return [TextContent(type="text", text=s)]


def _result_text(result: ssh.Result) -> str:
    parts = [f"exit: {result.returncode}"]
    if result.stdout:
        parts.append(f"stdout:\n{result.stdout.rstrip()}")
    if result.stderr:
        parts.append(f"stderr:\n{result.stderr.rstrip()}")
    return "\n".join(parts)


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    global _session_host
    try:
        if name == "remote_set_host":
            _session_host = arguments["host"].strip() or None
            return _text(f"default host set to {_session_host}")

        if name == "remote_info":
            host = _resolve(arguments.get("host"))
            cmd = (
                "echo === uname ===; uname -a; "
                "echo === arch ===; uname -m; "
                "echo === os-release ===; "
                "(cat /etc/os-release 2>/dev/null || true); "
                "echo === disk ===; df -h / 2>/dev/null | tail -n +1; "
                "echo === python ===; (python3 --version 2>/dev/null || python --version 2>/dev/null || echo none); "
                "echo === ripgrep ===; (command -v rg >/dev/null && rg --version | head -1 || echo absent)"
            )
            result = await ssh.run_command(cmd, host=host)
            return _text(f"host: {host}\n{_result_text(result)}")

        if name == "remote_bash":
            host = _resolve(arguments.get("host"))
            result = await ssh.run_command(
                arguments["command"],
                host=host,
                cwd=arguments.get("cwd"),
                env=arguments.get("env"),
                timeout=float(arguments.get("timeout", 120)),
            )
            return _text(_result_text(result))

        if name == "remote_read":
            host = _resolve(arguments.get("host"))
            content = await ssh.read_file(arguments["path"], host=host)
            offset = int(arguments.get("offset", 0))
            limit = arguments.get("limit")
            limit_int = int(limit) if limit is not None else None
            return _text(_format_lines(content, offset=offset, limit=limit_int))

        if name == "remote_write":
            host = _resolve(arguments.get("host"))
            await ssh.write_file(arguments["path"], arguments["content"], host=host)
            byte_count = len(arguments["content"].encode("utf-8"))
            return _text(f"wrote {byte_count} bytes to {arguments['path']} on {host}")

        if name == "remote_edit":
            host = _resolve(arguments.get("host"))
            path = arguments["path"]
            old = arguments["old_string"]
            new = arguments["new_string"]
            replace_all = bool(arguments.get("replace_all", False))
            content = await ssh.read_file(path, host=host)
            count = content.count(old)
            if count == 0:
                raise ValueError(f"old_string not found in {path}")
            if count > 1 and not replace_all:
                raise ValueError(
                    f"old_string matches {count} times in {path}; pass replace_all=true "
                    f"or extend old_string with surrounding context to make it unique"
                )
            updated = content.replace(old, new) if replace_all else content.replace(old, new, 1)
            await ssh.write_file(path, updated, host=host)
            return _text(f"edited {path} on {host} ({count} replacement{'s' if count != 1 else ''})")

        if name == "remote_ls":
            host = _resolve(arguments.get("host"))
            path = arguments.get("path", ".")
            result = await ssh.run_command(
                f"ls -lA -- {shlex.quote(path)}",
                host=host,
            )
            return _text(_result_text(result))

        if name == "remote_glob":
            host = _resolve(arguments.get("host"))
            pattern = arguments["pattern"]
            cwd = arguments.get("cwd", ".")
            # Use find with -path to honor a glob-ish pattern, then sort by mtime desc.
            # ** in input is mapped to a recursive find. For simplicity we shell-glob it.
            cmd = (
                f"cd {shlex.quote(cwd)} && "
                f"sh -c 'shopt -s globstar nullglob 2>/dev/null; "
                f"for f in {pattern}; do "
                f"[ -e \"$f\" ] && printf \"%s\\t%s\\n\" \"$(stat -c %Y \"$f\" 2>/dev/null || stat -f %m \"$f\")\" \"$f\"; "
                f"done' 2>/dev/null | sort -rn | head -n 200 | cut -f2-"
            )
            # bash needed for globstar; fall back gracefully
            result = await ssh.run_command(f"bash -c {shlex.quote(cmd)}", host=host)
            return _text(_result_text(result))

        if name == "remote_grep":
            host = _resolve(arguments.get("host"))
            pattern = arguments["pattern"]
            path = arguments.get("path", ".")
            glob = arguments.get("glob")
            ci = bool(arguments.get("case_insensitive", False))
            max_results = int(arguments.get("max_results", 200))
            ci_flag_rg = "-i " if ci else ""
            ci_flag_grep = "-i " if ci else ""
            glob_rg = f"-g {shlex.quote(glob)} " if glob else ""
            glob_grep = f"--include={shlex.quote(glob)} " if glob else ""
            cmd = (
                f"if command -v rg >/dev/null 2>&1; then "
                f"  rg --no-heading -n {ci_flag_rg}{glob_rg}-- {shlex.quote(pattern)} {shlex.quote(path)} | head -n {max_results}; "
                f"else "
                f"  grep -rn {ci_flag_grep}{glob_grep}-- {shlex.quote(pattern)} {shlex.quote(path)} 2>/dev/null | head -n {max_results}; "
                f"fi"
            )
            result = await ssh.run_command(cmd, host=host)
            return _text(_result_text(result))

        if name == "remote_put":
            host = _resolve(arguments.get("host"))
            await ssh.scp_put(
                arguments["local_path"],
                arguments["remote_path"],
                host=host,
                recursive=bool(arguments.get("recursive", False)),
            )
            return _text(f"copied {arguments['local_path']} to {host}:{arguments['remote_path']}")

        if name == "remote_get":
            host = _resolve(arguments.get("host"))
            await ssh.scp_get(
                arguments["remote_path"],
                arguments["local_path"],
                host=host,
                recursive=bool(arguments.get("recursive", False)),
            )
            return _text(f"copied {host}:{arguments['remote_path']} to {arguments['local_path']}")

        if name == "remote_disconnect":
            host = _resolve(arguments.get("host"))
            await ssh.close_master(host=host)
            return _text(f"closed control socket for {host}")

        raise ValueError(f"unknown tool: {name}")

    except ssh.RemoteError as e:
        return _text(f"error (exit {e.returncode}): {e.stderr.strip() or e.stdout.strip() or str(e)}")
    except Exception as e:
        return _text(f"error: {type(e).__name__}: {e}")


async def _serve() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
