"""CLI boundary checks using the same registered tool schemas and functions."""
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli
import mcp_server


def run(*arguments):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = asyncio.run(cli.main(list(arguments)))
    return code, stdout.getvalue(), stderr.getvalue()


class CliTests(unittest.TestCase):
    def test_tools_and_offline_status_do_not_start_backend(self):
        with patch.object(mcp_server.backend, "ensure_service") as start:
            code, stdout, _ = run("tools")
            self.assertEqual(len(json.loads(stdout)), 26)
            self.assertEqual(code, 0)
            with patch.object(mcp_server.backend, "get_status", return_value={"online": False}):
                self.assertEqual(json.loads(run("get-status")[1]), {"online": False})
            start.assert_not_called()

    def test_file_input_overrides_and_nested_types(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "参数.json").write_text(json.dumps({
                "text": "文件正文", "example_name": "voice_01.wav", "use_random": True,
                "emotion_vector": {"happy": 0.2}, "advanced": {"top_k": 0},
            }, ensure_ascii=False), encoding="utf-8")
            with patch.object(mcp_server.backend, "synthesize", return_value={"job_id": "sample"}) as generate:
                code, stdout, stderr = run("synthesize", "--args-file", str(directory / "参数.json"),
                                           "--text", "命令行正文", "--no-use-random")
                self.assertEqual(code, 0, stderr)
                values = generate.call_args.args[4]
                self.assertEqual(generate.call_args.args[0], "命令行正文")
                self.assertEqual(values["top_k"], 0)
                self.assertEqual(values["vec1"], 0.2)
                self.assertIs(values["use_random"], False)
                self.assertEqual(json.loads(stdout)["job_id"], "sample")

    def test_invalid_inputs_do_not_submit(self):
        with patch.object(mcp_server.backend, "synthesize") as generate:
            for arguments in [
                ("--text", "测试", "--example-name", "voice_01.wav", "--emotion-weight", "2"),
                ("--text", "测试", "--example-name", "voice_01.wav", "--wait", "--timeout", "nan"),
                ("--text", "测试", "--text-file", "unused.txt"),
            ]:
                code, stdout, stderr = run("synthesize", *arguments)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertFalse(json.loads(stderr)["ok"])
            generate.assert_not_called()

    def test_unknown_json_field_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "args.json"
            path.write_text('{"surprise": true}', encoding="utf-8")
            with patch.object(mcp_server.backend, "get_status") as status:
                self.assertEqual(run("get-status", "--args-file", str(path))[0], 1)
                status.assert_not_called()

    def test_utf8_text_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "正文.txt"
            path.write_text("你好，中文\n带换行、& 和百分号 %。", encoding="utf-8-sig")
            with patch.object(mcp_server.backend, "synthesize", return_value={"job_id": "sample"}) as generate:
                code, _, stderr = run("synthesize", "--text-file", str(path), "--example-name", "voice_01.wav")
                self.assertEqual(code, 0, stderr)
                self.assertEqual(generate.call_args.args[0], path.read_text(encoding="utf-8-sig"))

    def test_wait_only_polls_and_handles_terminal_states(self):
        for state, expected in [("done", 0), ("error", 1), ("cancelled", 1), ("stopped", 1), ("interrupted", 1)]:
            response = {"ok": True, "job": {"job_id": "sample", "state": state}}
            with patch.object(cli, "invoke", AsyncMock(return_value=response)) as invoke:
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = asyncio.run(cli.wait_task({"job_id": "sample"}, 10))
                self.assertEqual(code, expected)
                invoke.assert_awaited_once_with("get_task", {"job_id": "sample"})
                if expected:
                    self.assertIn('"ok": false', stderr.getvalue())
                else:
                    self.assertEqual(json.loads(stdout.getvalue()), response)

    def test_wait_timeout_returns_job_without_resubmission(self):
        clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 2]))
        with patch.object(cli, "time", clock), patch.object(cli, "invoke") as invoke:
            with redirect_stderr(io.StringIO()) as stderr:
                code = asyncio.run(cli.wait_task({"job_id": "sample"}, 1))
            self.assertEqual(code, 3)
            self.assertIn("sample", stderr.getvalue())
            invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
