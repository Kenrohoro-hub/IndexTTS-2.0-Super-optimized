"""Real CLI subprocesses and MCP access sharing an isolated GPU backend."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid

from mcp import Client
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "indextts-mcp"


def main():
    data = ADAPTER / "tests/.tmp" / ("cli-" + uuid.uuid4().hex[:8])
    data.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "INDEXTTS_PORT": str(port), "INDEXTTS_DATA_DIR": str(data), "PYTHONUTF8": "1"}
    report = {"checks": [], "passed": False}
    pid, started_at = None, time.time()

    def call(*args, expected=0):
        result = subprocess.run([sys.executable, "-B", str(ADAPTER / "cli.py"), *args],
                                cwd=Path.home(), env=env, capture_output=True, encoding="utf-8", timeout=300)
        assert result.returncode == expected, (args, result.stdout, result.stderr)
        return json.loads(result.stdout if expected == 0 else result.stderr)

    def passed(name):
        report["checks"].append(name)
        print("PASS " + name, flush=True)

    try:
        assert call("get-status")["online"] is False
        assert len(call("tools")) == 26
        pid = call("ensure-service")["pid"]
        assert call("get-status")["pid"] == pid
        passed("CLI discovery, offline status, startup and backend survival between commands")
        text = data / "配音正文.txt"
        text.write_text("你好，这是命令行生成的语音。", encoding="utf-8-sig")
        result = call("synthesize", "--text-file", str(text), "--example-name", "voice_01.wav", "--wait")
        job = result["job"]
        assert job["state"] == "done" and Path(job["result"]["audio_path"]).is_file()
        shutil.copy2(job["result"]["audio_path"], ADAPTER / "verification/cli.wav")
        report["audio"] = {"file": "cli.wav", "duration_seconds": job["result"]["duration_seconds"]}
        passed("UTF-8 text file, real GPU generation, --wait and JSON output")

        async def mcp_query():
            params = StdioServerParameters(command=sys.executable, args=["-B", str(ADAPTER / "mcp_server.py")], env=env)
            async with Client(params, mode="legacy") as client:
                status = await client.call_tool("get_status", {})
                assert status.structured_content["pid"] == pid
                query = await client.call_tool("get_task", {"job_id": job["job_id"]})
                assert query.structured_content["job"]["result"]["audio_path"] == job["result"]["audio_path"]
        asyncio.run(mcp_query())
        passed("MCP sees the same CLI job, output and backend")
        edited = call("edit-audio", "--audio-path", job["result"]["audio_path"], "--rate", "1.2", "--gain-db", "-3")
        assert Path(job["result"]["audio_path"]).is_file() and Path(edited["audio_path"]).is_file()
        target = data / "导出结果.wav"
        call("export-audio", "--audio-path", edited["audio_path"], "--destination-path", str(target))
        assert call("export-audio", "--audio-path", edited["audio_path"], "--destination-path", str(target), expected=1)["ok"] is False
        assert call("synthesize", "--text", "测试", "--example-name", "voice_01.wav", "--emotion-weight", "2", expected=1)["ok"] is False
        passed("CLI editing, export overwrite guard, validation and nonzero error exit")
        call("unload-model")
        assert call("get-status")["loaded"] is False
        passed("CLI model unload")
        report["passed"] = True
    finally:
        if pid:
            script = (
                "import pathlib,sys,psutil; p=psutil.Process(int(sys.argv[1])); c=p.cmdline(); "
                "assert '--data_dir' in c and pathlib.Path(c[c.index('--data_dir')+1]).resolve()==pathlib.Path(sys.argv[2]).resolve(); "
                "assert p.create_time()>=float(sys.argv[3])-2; p.terminate(); p.wait(20)"
            )
            cleaned = subprocess.run([str(ROOT / ".venv/Scripts/python.exe"), "-B", "-c", script, str(pid), str(data), str(started_at)])
            report["cleanup_ok"] = cleaned.returncode == 0
        (ADAPTER / "verification/cli-acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
