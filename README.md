# claude-remote-access

Develop on a remote host (e.g. an armv7l Raspberry Pi) from a Claude Code
session running on your desktop. Exposes a small MCP server that proxies
filesystem and shell operations over plain SSH — no Claude Code binaries,
VS Code Server, or other agents need to run on the target.

Built for embedded targets where the official Claude Code CLI and Microsoft
Remote-SSH have dropped support (32-bit ARM, older glibc, etc).

## Install

```bash
claude plugin marketplace add fingerskier/claude-plugins
claude plugin install remote-access@fingerskier-plugins
```

## Configure

The target host can come from a per-call argument, a session default set by
`remote_set_host`, or the `CLAUDE_REMOTE_HOST` environment variable:

```bash
export CLAUDE_REMOTE_HOST=pi@pi.local
```

Anything that works for plain `ssh` works here — host aliases from
`~/.ssh/config`, `user@host`, agent forwarding, jump hosts, key files. The
plugin shells out to your local `ssh`/`scp`.

## Tools

| Tool | Purpose |
|---|---|
| `remote_set_host` | Set the default host for this session |
| `remote_info` | uname, arch, distro, disk free, tooling probe |
| `remote_bash` | Run a shell command (cwd, env, timeout supported) |
| `remote_read` | Read a file with line numbers (offset/limit for large files) |
| `remote_write` | Write or overwrite a file (creates parent dirs) |
| `remote_edit` | Exact string replace inside a remote file |
| `remote_ls` | `ls -lA` on a directory |
| `remote_glob` | Find files matching a glob pattern, newest first |
| `remote_grep` | Recursive grep (ripgrep if present, else `grep -rn`) |
| `remote_put` | scp a local file/dir to the remote |
| `remote_get` | scp a file/dir from the remote |
| `remote_disconnect` | Close the persistent SSH control socket |

## How it works

- Shells out to the user's local `ssh` and `scp` so the existing SSH config,
  agent, keys, and jump hosts are honored.
- Uses an `ssh -o ControlMaster=auto` socket (kept under
  `${CLAUDE_PLUGIN_DATA}/controlmasters`) so per-call overhead drops to a
  few milliseconds after the first call has opened the master.
- All progress and error output goes to stderr to keep the MCP stdio
  JSON-RPC stream on stdout clean.

## Requirements

- Local machine: `ssh` and `scp` on `PATH`. Python 3.10+ (Claude Code uses
  it automatically via the plugin launcher).
- Remote host: `sshd`, a POSIX shell, and standard coreutils. `bash` is
  used for `remote_glob` (for `globstar`); `rg` is used by `remote_grep`
  if available, otherwise `grep -rn`.

## License

[MIT](./LICENSE)
