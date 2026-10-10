"""Protocol and argv tests for guildmcp. No network and no real guild binary."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from guildmcp.server import TOOLS, TOOL_NAME_RE, call_tool  # noqa: E402


FAKE_GUILD = """#!/usr/bin/env python3
import json, os, sys, time
delay = os.environ.get("GUILD_FAKE_SLEEP", "")
if delay:
    time.sleep(float(delay))
print(json.dumps({"argv": sys.argv[1:]}))
"""


def frame(obj: dict) -> bytes:
    body = json.dumps(obj).encode()
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


def read_frame(stream) -> dict:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            raise EOFError("server closed stdout")
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode().partition(":")
        headers[key.strip().lower()] = value.strip()
    n = int(headers["content-length"])
    return json.loads(stream.read(n))


class ArgvTests(unittest.TestCase):
    def test_tool_names_are_legal(self) -> None:
        names = [tool["name"] for tool in TOOLS]
        self.assertEqual(
            names,
            [
                "guild_version",
                "guild_remotes",
                "guild_remote_status",
                "guild_check",
                "guild_runs",
                "guild_run_info",
            ],
        )
        for name in names:
            self.assertIsNotNone(TOOL_NAME_RE.fullmatch(name))

    def test_commands_are_argv_not_a_shell(self) -> None:
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))

            class Proc:
                returncode = 0
                stdout = "guild 0.9.0\n"
                stderr = ""

            return Proc()

        with patch.dict(os.environ, {"GUILD_BIN": "/bin/echo"}):
            with patch("guildmcp.server.subprocess.run", fake_run):
                payload, is_error = call_tool("guild_version", {})
        self.assertFalse(is_error)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(calls[0][0], ["/bin/echo", "--version"])
        self.assertFalse(calls[0][1]["shell"])
        self.assertNotIn("json", payload)

    def test_check_runs_and_info_argv(self) -> None:
        seen = []

        def fake_run(argv, **kwargs):
            seen.append(argv[1:])

            class Proc:
                returncode = 0
                stdout = "[]"
                stderr = ""

            return Proc()

        with patch.dict(os.environ, {"GUILD_BIN": "/bin/echo"}):
            with patch("guildmcp.server.subprocess.run", fake_run):
                call_tool("guild_check", {})
                call_tool(
                    "guild_check",
                    {
                        "offline": False,
                        "env": False,
                        "version": "==0.9.0",
                        "remote": "remote-a",
                    },
                )
                call_tool("guild_runs", {})
                call_tool("guild_runs", {"limit": 3, "remote": "remote-b"})
                call_tool("guild_run_info", {})
                call_tool("guild_run_info", {"run": "abc123", "remote": "remote-c"})
                payload, is_error = call_tool("guild_remotes", {})
        self.assertFalse(is_error)
        self.assertEqual(payload["json"], [])
        self.assertEqual(
            seen,
            [
                ["check", "--offline", "--env"],
                ["check", "--no-offline", "--version", "==0.9.0", "-r", "remote-a"],
                ["runs", "--json"],
                ["runs", "-n", "3", "-r", "remote-b"],
                ["runs", "info", "--json"],
                ["runs", "info", "abc123", "--json", "-r", "remote-c"],
                ["remotes"],
            ],
        )
        for argv in seen:
            self.assertNotIn("--private-attrs", argv)
            self.assertNotIn("--tests", argv)
            if argv[:2] == ["runs", "info"]:
                self.assertNotIn("--env", argv)

    def test_rejects_shell_metacharacters_without_invoking_guild(self) -> None:
        def boom(*_args, **_kwargs):
            raise AssertionError("guild was invoked")

        with patch("guildmcp.server.subprocess.run", boom):
            for name, args in (
                ("guild_remote_status", {"remote": "remote-a;rm"}),
                ("guild_remote_status", {"remote": "a..b"}),
                ("guild_remote_status", {}),
                ("guild_check", {"version": "==0.9.0;id"}),
                ("guild_check", {"version": "$(id)"}),
                ("guild_check", {"offline": "yes"}),
                ("guild_runs", {"limit": 0}),
                ("guild_runs", {"limit": 101}),
                ("guild_runs", {"limit": True}),
                ("guild_runs", {"remote": "../x"}),
                ("guild_run_info", {"run": "abc def"}),
                ("not_a_tool", {}),
            ):
                payload, is_error = call_tool(name, args)
                self.assertTrue(is_error, args)
                self.assertFalse(payload["ok"])

    def test_local_runs_json_is_sliced_to_limit(self) -> None:
        def fake_run(argv, **kwargs):
            class Proc:
                returncode = 0
                stdout = json.dumps([{"id": i} for i in range(5)])
                stderr = ""

            return Proc()

        with patch.dict(os.environ, {"GUILD_BIN": "/bin/echo"}):
            with patch("guildmcp.server.subprocess.run", fake_run):
                payload, is_error = call_tool("guild_runs", {"limit": 2})
        self.assertFalse(is_error)
        self.assertEqual(payload["argv"], ["runs", "--json"])
        self.assertEqual(payload["total"], 5)
        self.assertEqual(payload["truncated_to"], 2)
        self.assertEqual(payload["json"], [{"id": 0}, {"id": 1}])
        self.assertEqual(json.loads(payload["stdout"]), [{"id": 0}, {"id": 1}])

    def test_local_runs_at_limit_keep_stdout(self) -> None:
        def fake_run(argv, **kwargs):
            class Proc:
                returncode = 0
                stdout = "[]\n"
                stderr = ""

            return Proc()

        with patch.dict(os.environ, {"GUILD_BIN": "/bin/echo"}):
            with patch("guildmcp.server.subprocess.run", fake_run):
                payload, is_error = call_tool("guild_runs", {"limit": 2})
        self.assertFalse(is_error)
        self.assertEqual(payload["total"], 0)
        self.assertNotIn("truncated_to", payload)
        self.assertEqual(payload["stdout"], "[]\n")
        self.assertEqual(payload["json"], [])

    def test_nonzero_exit_is_a_tool_error(self) -> None:
        def fake_run(argv, **kwargs):
            class Proc:
                returncode = 2
                stdout = ""
                stderr = "nope"

            return Proc()

        with patch.dict(os.environ, {"GUILD_BIN": "/bin/echo"}):
            with patch("guildmcp.server.subprocess.run", fake_run):
                payload, is_error = call_tool(
                    "guild_remote_status", {"remote": "remote-a"}
                )
        self.assertTrue(is_error)
        self.assertEqual(payload["exit_code"], 2)
        self.assertEqual(payload["argv"], ["remote", "status", "remote-a"])

    def test_missing_binary(self) -> None:
        with patch.dict(os.environ, {"GUILD_BIN": "/no/such/guild-binary"}):
            payload, is_error = call_tool("guild_version", {})
        self.assertTrue(is_error)
        self.assertIn("not a file", payload["error"])

    def test_truncates_large_stdout(self) -> None:
        def fake_run(argv, **kwargs):
            class Proc:
                returncode = 0
                stdout = "x" * 100_001
                stderr = ""

            return Proc()

        with patch.dict(os.environ, {"GUILD_BIN": "/bin/echo"}):
            with patch("guildmcp.server.subprocess.run", fake_run):
                payload, is_error = call_tool("guild_version", {})
        self.assertFalse(is_error)
        self.assertTrue(payload["stdout_truncated"])
        self.assertNotIn("json", payload)
        self.assertTrue(payload["stdout"].endswith("\n...[truncated]"))


class ProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = Path(self.tmp.name) / "guild"
        self.fake.write_text(FAKE_GUILD)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)

    def _server(self, extra_env: dict | None = None) -> subprocess.Popen:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env["GUILD_BIN"] = str(self.fake)
        env.pop("GUILD_FAKE_SLEEP", None)
        env["GUILDMCP_TIMEOUT_SEC"] = "5"
        if extra_env:
            env.update(extra_env)
        return subprocess.Popen(
            [sys.executable, "-m", "guildmcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(ROOT),
        )

    def test_initialize_list_and_call_skip_notifications(self) -> None:
        proc = self._server()
        assert proc.stdin and proc.stdout and proc.stderr
        proc.stdin.write(
            frame(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "0"},
                    },
                }
            )
        )
        proc.stdin.write(
            frame({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )
        proc.stdin.write(frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        proc.stdin.write(
            frame(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "guild_runs",
                        "arguments": {"limit": 2, "remote": "remote-a"},
                    },
                }
            )
        )
        proc.stdin.write(
            frame(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "guild_check", "arguments": {"version": "==0.9.0 && true"}},
                }
            )
        )
        proc.stdin.close()
        init = read_frame(proc.stdout)
        listed = read_frame(proc.stdout)
        called = read_frame(proc.stdout)
        rejected = read_frame(proc.stdout)
        self.assertEqual(proc.wait(timeout=5), 0)
        err = proc.stderr.read().decode()
        proc.stdout.close()
        proc.stderr.close()
        self.assertEqual(err, "")
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(init["result"]["serverInfo"]["name"], "guildmcp")
        self.assertEqual(
            [tool["name"] for tool in listed["result"]["tools"]],
            [tool["name"] for tool in TOOLS],
        )
        self.assertFalse(called["result"]["isError"])
        body = json.loads(called["result"]["content"][0]["text"])
        self.assertEqual(body["json"]["argv"], ["runs", "-n", "2", "-r", "remote-a"])
        self.assertTrue(rejected["result"]["isError"])
        self.assertIn("version", json.loads(rejected["result"]["content"][0]["text"])["error"])

    def test_bad_protocol_version_falls_back(self) -> None:
        proc = self._server()
        assert proc.stdin and proc.stdout
        proc.stdin.write(
            frame(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "not-a-date"},
                }
            )
        )
        proc.stdin.close()
        init = read_frame(proc.stdout)
        self.assertEqual(proc.wait(timeout=5), 0)
        proc.stdout.close()
        assert proc.stderr is not None
        proc.stderr.close()
        self.assertEqual(init["result"]["protocolVersion"], "2024-11-05")

    def test_timeout_is_reported(self) -> None:
        proc = self._server(
            {"GUILD_FAKE_SLEEP": "3", "GUILDMCP_TIMEOUT_SEC": "1"}
        )
        assert proc.stdin and proc.stdout
        proc.stdin.write(
            frame(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"},
                }
            )
        )
        proc.stdin.write(
            frame(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "guild_version", "arguments": {}},
                }
            )
        )
        proc.stdin.close()
        read_frame(proc.stdout)
        called = read_frame(proc.stdout)
        self.assertEqual(proc.wait(timeout=5), 0)
        proc.stdout.close()
        assert proc.stderr is not None
        proc.stderr.close()
        self.assertTrue(called["result"]["isError"])
        body = json.loads(called["result"]["content"][0]["text"])
        self.assertIn("timed out", body["error"])
        self.assertIsNone(body["exit_code"])


if __name__ == "__main__":
    unittest.main()
