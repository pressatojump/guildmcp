#!/usr/bin/env python3
"""stdio MCP for the local guildai CLI. Read-only. No shell."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROTOCOL = "2024-11-05"
PROTOCOL_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
VERSION_RE = re.compile(
    r"^(?:==|!=|<=|>=|~=|<|>)?[0-9][0-9A-Za-z.*+-]*"
    r"(?:,(?:==|!=|<=|>=|~=|<|>)?[0-9][0-9A-Za-z.*+-]*)*$"
)
MAX_OUTPUT = 100_000
MAX_MESSAGE = 1_000_000
DEFAULT_TIMEOUT = 90
DEFAULT_LIMIT = 20
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# Version specs are their own argv element. This process does not use a shell.
# guildai's SSH client may still hand that argv to a remote login shell.


class ToolError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def guild_bin() -> str:
    raw = os.environ.get("GUILD_BIN", "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise ToolError(f"GUILD_BIN is not a file: {raw}")
        return str(path)
    found = shutil.which("guild")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "guild"
    if fallback.is_file():
        return str(fallback)
    raise ToolError("guild not found on PATH and ~/.local/bin/guild is missing")


def timeout_sec() -> float:
    raw = os.environ.get("GUILDMCP_TIMEOUT_SEC", "").strip()
    if not raw:
        return float(DEFAULT_TIMEOUT)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_TIMEOUT)
    if value < 1:
        return 1.0
    if value > 110:
        return 110.0
    return value


def require_name(value: object, field: str) -> str:
    if not isinstance(value, str) or not NAME_RE.fullmatch(value) or ".." in value:
        raise ToolError(
            f"{field} must match {NAME_RE.pattern} and must not contain '..'"
        )
    return value


def optional_name(args: dict, field: str) -> str | None:
    if field not in args or args[field] is None:
        return None
    return require_name(args[field], field)


def optional_version(args: dict) -> str | None:
    if "version" not in args or args["version"] is None:
        return None
    value = args["version"]
    if (
        not isinstance(value, str)
        or len(value) > 64
        or not VERSION_RE.fullmatch(value)
    ):
        raise ToolError("version must be a packaging specifier such as ==0.9.0")
    return value


def optional_bool(args: dict, field: str, default: bool) -> bool:
    if field not in args or args[field] is None:
        return default
    value = args[field]
    if not isinstance(value, bool):
        raise ToolError(f"{field} must be a boolean")
    return value


def optional_limit(args: dict) -> int:
    if "limit" not in args or args["limit"] is None:
        return DEFAULT_LIMIT
    value = args["limit"]
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ToolError("limit must be an integer from 1 to 100")
    return value


def _clip(text: str | bytes | None) -> tuple[str, bool]:
    if text is None:
        return "", False
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if len(text) <= MAX_OUTPUT:
        return text, False
    return text[:MAX_OUTPUT] + "\n...[truncated]", True


def _parse_json(text: str):
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def run_guild(argv: list[str]) -> dict:
    binary = guild_bin()
    timeout = timeout_sec()
    try:
        proc = subprocess.run(
            [binary, *argv],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout, stdout_truncated = _clip(exc.stdout)
        stderr, stderr_truncated = _clip(exc.stderr)
        body: dict = {
            "ok": False,
            "error": f"guild timed out after {timeout:g}s",
            "exit_code": None,
            "argv": argv,
            "stdout": stdout,
            "stderr": stderr,
        }
        if stdout_truncated:
            body["stdout_truncated"] = True
        if stderr_truncated:
            body["stderr_truncated"] = True
        return body
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    out_text, out_trunc = _clip(stdout)
    err_text, err_trunc = _clip(stderr)
    body = {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "argv": argv,
        "stdout": out_text,
        "stderr": err_text,
    }
    if out_trunc:
        body["stdout_truncated"] = True
    elif (parsed := _parse_json(stdout)) is not None:
        body["json"] = parsed
    if err_trunc:
        body["stderr_truncated"] = True
    if proc.returncode != 0:
        body["error"] = f"guild exited {proc.returncode}"
    return body


def _check_argv(args: dict) -> list[str]:
    argv = ["check"]
    if optional_bool(args, "offline", True):
        argv.append("--offline")
    else:
        argv.append("--no-offline")
    if optional_bool(args, "env", True):
        argv.append("--env")
    version = optional_version(args)
    if version:
        argv.extend(["--version", version])
    remote = optional_name(args, "remote")
    if remote:
        argv.extend(["-r", remote])
    return argv


def _runs_argv(args: dict) -> list[str]:
    # guildai 0.9.0: local `runs --json` ignores -n and warns if it is passed.
    # The SSH list path drops --json and honors --limit. Slice the local list here.
    limit = optional_limit(args)
    remote = optional_name(args, "remote")
    if remote:
        return ["runs", "-n", str(limit), "-r", remote]
    return ["runs", "--json"]


def _slice_local_runs(payload: dict, limit: int) -> dict:
    if "--json" not in payload.get("argv", []):
        return payload
    runs = payload.get("json")
    if not isinstance(runs, list):
        return payload
    payload["total"] = len(runs)
    if len(runs) <= limit:
        return payload
    sliced = runs[:limit]
    payload["json"] = sliced
    payload["truncated_to"] = limit
    payload["stdout"] = json.dumps(sliced)
    return payload


def _info_argv(args: dict) -> list[str]:
    argv = ["runs", "info"]
    run = optional_name(args, "run")
    if run:
        argv.append(run)
    argv.append("--json")
    remote = optional_name(args, "remote")
    if remote:
        argv.extend(["-r", remote])
    return argv


def call_tool(name: str, args: dict) -> tuple[dict, bool]:
    try:
        if name == "guild_version":
            payload = run_guild(["--version"])
        elif name == "guild_remotes":
            payload = run_guild(["remotes"])
        elif name == "guild_remote_status":
            payload = run_guild(["remote", "status", require_name(args.get("remote"), "remote")])
        elif name == "guild_check":
            payload = run_guild(_check_argv(args))
        elif name == "guild_runs":
            limit = optional_limit(args)
            payload = _slice_local_runs(run_guild(_runs_argv(args)), limit)
        elif name == "guild_run_info":
            payload = run_guild(_info_argv(args))
        else:
            return {"ok": False, "error": f"unknown tool {name}"}, True
    except ToolError as exc:
        return {"ok": False, "error": exc.message}, True
    return payload, not bool(payload.get("ok"))


TOOLS = [
    {
        "name": "guild_version",
        "description": "Print guild --version.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "guild_remotes",
        "description": "List Guild remotes from the local Guild config.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "guild_remote_status",
        "description": "Show whether one configured Guild remote accepts SSH. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"remote": {"type": "string"}},
            "required": ["remote"],
            "additionalProperties": False,
        },
    },
    {
        "name": "guild_check",
        "description": "Run guild check. Defaults to --offline --env. Optional remote and version spec. Does not run Guild's test suite.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "offline": {"type": "boolean"},
                "env": {"type": "boolean"},
                "version": {"type": "string"},
                "remote": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "guild_runs",
        "description": "List Guild runs. Local JSON is sliced to the limit (1-100, default 20). Remote text honors that limit. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "remote": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "guild_run_info",
        "description": "Show one Guild run as JSON. Omit run for the latest. Optional remote. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": {"type": "string"},
                "remote": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
]


def send(msg: dict) -> None:
    body = json.dumps(msg, separators=(",", ":")).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()


def send_error(mid: object, code: int, message: str) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "id": mid,
            "error": {"code": code, "message": message[:500]},
        }
    )


def read_message():
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        if len(line) > 8192:
            raise ValueError("header too long")
        decoded = line.decode("ascii", "replace")
        key, sep, value = decoded.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    try:
        n = int(headers.get("content-length", "0"))
    except ValueError as exc:
        raise ValueError("invalid content-length") from exc
    if n <= 0:
        return None
    if n > MAX_MESSAGE:
        sys.stdin.buffer.read(n)
        raise ValueError("message too large")
    raw = sys.stdin.buffer.read(n)
    if len(raw) < n:
        return None
    return json.loads(raw.decode())


def _tool_result(payload: dict, is_error: bool) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "isError": is_error,
    }


def handle(msg: dict) -> None:
    method = msg.get("method")
    has_id = "id" in msg and msg.get("id") is not None
    mid = msg.get("id")
    if not isinstance(method, str):
        if has_id:
            send_error(mid, -32600, "invalid request")
        return
    if not has_id or method.startswith("notifications/"):
        return
    if method == "initialize":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        requested = params.get("protocolVersion")
        version = (
            requested
            if isinstance(requested, str) and PROTOCOL_RE.fullmatch(requested)
            else PROTOCOL
        )
        send(
            {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "guildmcp", "version": "0.1.0"},
                },
            }
        )
        return
    if method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        return
    if method == "tools/call":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        name = params.get("name")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if not isinstance(name, str):
            payload, is_error = {"ok": False, "error": "tool name required"}, True
        else:
            payload, is_error = call_tool(name, arguments)
        send(
            {
                "jsonrpc": "2.0",
                "id": mid,
                "result": _tool_result(payload, is_error),
            }
        )
        return
    if method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
        return
    send_error(mid, -32601, method)


def main() -> None:
    for tool in TOOLS:
        if not TOOL_NAME_RE.fullmatch(tool["name"]):
            raise SystemExit(f"invalid tool name {tool['name']}")
    while True:
        try:
            msg = read_message()
        except Exception as exc:
            sys.stderr.write(f"guildmcp: read failed: {exc}\n")
            return
        if msg is None:
            return
        if not isinstance(msg, dict):
            continue
        try:
            handle(msg)
        except BrokenPipeError:
            return
        except Exception as exc:
            sys.stderr.write(f"guildmcp: {exc}\n")
            if "id" in msg and msg.get("id") is not None:
                send_error(msg.get("id"), -32603, "internal error")


if __name__ == "__main__":
    main()
