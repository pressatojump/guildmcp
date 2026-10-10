# guildmcp

Read-only [Model Context Protocol](https://modelcontextprotocol.io) server for the local [guildai](https://pypi.org/project/guildai/) command-line tool (`guild`). It lists remotes, checks an install, and reads runs. It does not start, stop, delete, or push runs.

This is not the hosted documentation server at `docs.guild.ai`, and it is not an install of another project named guild.

## Requirements

- Python 3.9 or newer for this server. The server uses only the standard library.
- A `guild` executable on `PATH`, or `GUILD_BIN` pointing at one.
- guildai 0.9.0 imports the removed `imp` module, so that release needs Python 3.11. Python 3.12 cannot import it.

## Tools

| Tool | Guild command |
|---|---|
| `guild_version` | `guild --version` |
| `guild_remotes` | `guild remotes` |
| `guild_remote_status` | `guild remote status REMOTE` |
| `guild_check` | `guild check --offline --env` (both flags optional) |
| `guild_runs` | local: `guild runs --json`, then the first LIMIT rows (1–100, default 20). remote: `guild runs -n LIMIT -r REMOTE` (text) |
| `guild_run_info` | `guild runs info [RUN] --json` |

`guild_check` accepts `version` (a packaging specifier such as `==0.9.0`) and `remote`. `guild_runs` and `guild_run_info` accept `remote`. Omit `run` to select the latest run.

guildai 0.9.0 ignores `-n` when local `guild runs --json` is used, and it warns if both are passed. This server omits `-n` on that path and slices the JSON list. A remote `guild runs -r` drops `--json` and honors `-n`, so a remote list is Guild's text table. When the local list is longer than the limit, the result adds `total` and `truncated_to` and rewrites `stdout` to the sliced JSON. `total` is also set when the list already fits. Output longer than 100000 characters is clipped before that slice.

Each tool returns JSON with `ok`, `exit_code`, `argv`, `stdout`, and `stderr`. JSON stdout is also parsed into `json`. Remote names, run ids, and version specs are checked before `guild` is started. The server passes an argument vector and does not use a shell.

`guild runs info --env` is not exposed. That flag prints the run environment.

## Run

```bash
PYTHONPATH=src python3 -m guildmcp
```

The process speaks MCP over stdio (`Content-Length` frames). It echoes a `YYYY-MM-DD` client protocol version, otherwise `2024-11-05`. It looks for `guild` on `PATH`, then `~/.local/bin/guild`. `GUILD_BIN` overrides that. `GUILD_HOME` is passed through to Guild. `GUILDMCP_TIMEOUT_SEC` bounds each Guild command (default 90, clamped to 1–110). Each call is one `guild` process. The server keeps no session.

An MCP client can launch it like this. Replace the path with the checkout:

```json
{
  "command": "python3",
  "args": ["-m", "guildmcp"],
  "env": {
    "PYTHONPATH": "/path/to/guildmcp/src"
  }
}
```

Version specs are a separate argument. This server does not quote them for a shell. guildai's own SSH client may still hand that argument to a remote login shell. On zsh, an unquoted `=` is a command, so a stock guildai 0.9.0 client can fail a remote `--version '==0.9.0'` check until that client quotes `=`.

## Test

```bash
python3 -m unittest discover -s tests -v
```

The tests use a fake `guild` binary. They do not contact a network or a real Guild install.
