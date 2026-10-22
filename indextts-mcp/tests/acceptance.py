"""Real stdio clients and HTTP backend in an isolated data directory; --gpu adds inference."""
import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time
import uuid
import wave

from mcp import Client
from mcp.client.stdio import StdioServerParameters
import requests


ROOT = Path(__file__).resolve().parents[2]
MCP_ROOT = ROOT / "indextts-mcp"
sys.path.insert(0, str(MCP_ROOT))


def create_wave(path):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
        audio.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / 22050)))
                                  for i in range(26460)))


async def main(gpu):
    data = MCP_ROOT / "tests" / ".tmp" / ("acceptance-" + uuid.uuid4().hex[:8])
    data.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    environment = {**os.environ, "INDEXTTS_PORT": str(port), "INDEXTTS_DATA_DIR": str(data),
                   "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    params = StdioServerParameters(command=sys.executable, args=["-B", str(MCP_ROOT / "mcp_server.py")], env=environment)
    evidence = {"gpu": gpu, "checks": [], "audio": [], "tools": []}
    pid = None
    started_at = time.time()
    verification = MCP_ROOT / "verification"
    verification.mkdir(exist_ok=True)

    def passed(name):
        evidence["checks"].append(name)
        print("PASS " + name, flush=True)

    async def call(client, tool, arguments=None, error=False):
        result = await client.call_tool(tool, arguments or {}, read_timeout_seconds=240)
        if error:
            assert result.is_error, (tool, result)
            return result
        assert not result.is_error, (tool, result)
        return result.structured_content

    async def wait_task(client, job_id):
        deadline = time.monotonic() + 420
        last_stage = None
        while time.monotonic() < deadline:
            job = (await call(client, "get_task", {"job_id": job_id}))["job"]
            stage = job.get("stage_desc", job["state"])
            if stage != last_stage:
                print("TASK " + job_id + " " + str(stage), flush=True)
                last_stage = stage
            if job["state"] in {"done", "error", "cancelled", "stopped", "interrupted"} and job.get("finished_at"):
                return job
            await asyncio.sleep(1)
        raise AssertionError("inference exceeded acceptance timeout")

    try:
        async with Client(params, mode="legacy", read_timeout_seconds=240) as first, \
                   Client(params, read_timeout_seconds=240) as second:
            tools = await first.list_tools()
            evidence["tools"] = [tool.name for tool in tools.tools]
            assert len(evidence["tools"]) == 26
            assert (await call(first, "get_status"))["online"] is False
            passed("stdio discovery, legacy/auto clients, offline status")
            startup = await asyncio.gather(call(first, "ensure_service"), call(second, "ensure_service"))
            assert startup[0]["pid"] == startup[1]["pid"]
            assert sum(bool(item["started"]) for item in startup) == 1
            pid = startup[0]["pid"]
            passed("two independent MCP clients start exactly one backend")
            status = await call(first, "get_status")
            assert status["model_loaded"] is False and status["online"] is True
            await call(first, "synthesize", {"text": "", "example_name": "voice_01.wav"}, error=True)
            await call(first, "synthesize", {"text": "测试", "reference_audio_path": "relative.wav"}, error=True)
            await call(first, "synthesize", {"text": "测试", "example_name": "voice_01.wav", "advanced": {"unknown": 1}}, error=True)
            empty_emotion = await call(first, "synthesize", {"text": "测试", "example_name": "voice_01.wav", "emotion_text": " "}, error=True)
            assert "emotion_text 不能是空白" in empty_emotion.content[0].text
            passed("invalid arguments fail before inference")
            examples = await call(first, "list_examples")
            assert "voice_01.wav" in examples["voice_files"]
            voice = str(ROOT / "examples" / "voice_01.wav")
            await call(first, "save_preset", {"name": "MCP验收", "reference_audio_path": voice, "emotion_weight": 0.4})
            await call(first, "save_preset", {"name": "MCP验收", "emotion_weight": 0.3}, error=True)
            await call(first, "save_preset", {"name": "MCP验收", "emotion_weight": 0.7, "overwrite": True})
            preset = await call(first, "get_preset", {"name": "MCP验收"})
            assert preset["prompt_audio_url"] and preset["emo_weight"] == 0.7
            await call(first, "save_preset", {"name": "alias voice", "reference_audio_path": voice, "emotion_weight": 0.3})
            await call(first, "save_preset", {"name": "alias voice", "overwrite": True})
            assert (await call(first, "get_preset", {"name": "alias_voice"}))["emo_weight"] == 0.3
            await call(first, "duplicate_preset", {"name": "MCP验收", "new_name": "MCP副本"})
            await call(first, "rename_preset", {"name": "MCP副本", "new_name": "MCP改名"})
            await call(first, "delete_preset", {"name": "MCP改名"})
            assert "MCP改名" not in (await call(first, "list_presets"))["presets"]
            passed("preset create, conflict, update retaining audio, copy, rename, native recycle")
            await call(first, "set_pronunciation", {"term": "MCP/测试", "reading_en": "M C P"})
            glossary = await call(first, "get_glossary")
            assert "MCP/测试" in glossary["glossary"]
            await call(first, "set_glossary_cascade", {"enabled": True})
            await call(first, "delete_pronunciation", {"term": "MCP/测试"})
            preview = await call(first, "preview_text", {"text": "你好，这是中文与 English 的分段测试。"})
            assert preview["segments"]
            passed("glossary persistence, slash term deletion, cascade and text preview")
            await call(first, "configure_model", {"settings": {"diffusion_steps": 25}})
            await call(first, "set_history_auto_clean", {"enabled": True})
            fixture = data / "剪辑输入.wav"
            create_wave(fixture)
            edited = await call(first, "edit_audio", {"audio_path": str(fixture), "start_seconds": 0.1,
                                                       "end_seconds": 1.1, "rate": 1.25, "gain_db": 6})
            assert abs(edited["duration_seconds"] - 0.8) < 0.002
            assert fixture.is_file() and Path(edited["audio_path"]).is_file()
            exported = data / "导出结果.wav"
            await call(first, "export_audio", {"audio_path": edited["audio_path"], "destination_path": str(exported)})
            digest = hashlib.sha256(exported.read_bytes()).hexdigest()
            await call(first, "export_audio", {"audio_path": str(fixture), "destination_path": str(exported)}, error=True)
            assert hashlib.sha256(exported.read_bytes()).hexdigest() == digest
            passed("trim, tempo, gain, original preserved and export overwrite guard")

            if gpu:
                jobs = []
                for label, arguments in [
                    ("speaker", {"example_name": "voice_01.wav"}),
                    ("audio", {"preset_name": "MCP验收", "emotion_audio_path": str(ROOT / "examples" / "emo_sad.wav")}),
                    ("vector", {"reference_audio_path": voice, "emotion_vector": {"happy": 0.25}}),
                    ("text", {"preset_name": "MCP验收", "emotion_text": "平静地介绍一个好消息"}),
                ]:
                    submitted = await call(first, "synthesize", {"text": "你好，这是通过 MCP 生成的语音。", **arguments})
                    job = await wait_task(second, submitted["job_id"])
                    assert job["state"] == "done", job
                    artifact = Path(job["result"]["audio_path"])
                    with wave.open(str(artifact), "rb") as audio:
                        assert audio.getnframes() > 0 and audio.getframerate() == 22050
                    saved = verification / (label + ".wav")
                    shutil.copy2(artifact, saved)
                    evidence["audio"].append({"mode": label, "file": saved.name,
                                               "duration_seconds": job["result"]["duration_seconds"],
                                               "elapsed": job["result"]["elapsed"],
                                               "mel_capped": job["result"]["mel_capped"]})
                    jobs.append(job)
                    passed("real GPU inference: " + label)

                long = await call(first, "synthesize", {"text": "这是需要停止的长文本测试。" * 15, "example_name": "voice_01.wav"})
                queued = await call(second, "synthesize", {"text": "这条排队任务会取消。", "example_name": "voice_02.wav"})
                await call(second, "stop_task", {"job_id": queued["job_id"]})
                await asyncio.sleep(1)
                await call(first, "unload_model", error=True)
                await call(first, "stop_task", {"job_id": long["job_id"]})
                assert (await wait_task(second, long["job_id"]))["state"] in {"stopped", "cancelled"}
                assert (await wait_task(second, queued["job_id"]))["state"] == "cancelled"
                passed("real running stop, queued cancel and busy model protection")

                def web_sse():
                    with requests.Session() as session:
                        session.trust_env = False
                        with (ROOT / "examples" / "voice_01.wav").open("rb") as stream:
                            with session.post(f"http://127.0.0.1:{port}/api/v1/tts", data={"text": "停止后继续生成。"},
                                              files={"spk_audio": stream}, stream=True, timeout=(3, 240)) as response:
                                response.raise_for_status()
                                events = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith(b"data: ")]
                    assert events[-1]["type"] == "done", events[-1]
                    return events
                events = await asyncio.to_thread(web_sse)
                assert any(event["type"] == "progress" for event in events)
                passed("original WebUI SSE flow and generation after stop")
                await call(first, "delete_history_item", {"item_id": jobs[0]["result"]["history_id"]})
                assert Path(jobs[0]["result"]["audio_path"]).is_file()
                passed("history deletion preserves MCP delivery")
                await call(first, "unload_model")
                assert (await call(first, "get_status"))["loaded"] is False
                passed("model unload")

        with requests.Session() as session:
            session.trust_env = False
            assert session.get(f"http://127.0.0.1:{port}/api/v1/health", timeout=5).json()["pid"] == pid
        passed("shared backend survives both MCP client exits")
        evidence["passed"] = True
    finally:
        if pid is not None:
            cleanup = (
                "import pathlib,sys,psutil; "
                "sys.exit(0) if not psutil.pid_exists(int(sys.argv[1])) else None; "
                "p=psutil.Process(int(sys.argv[1])); c=p.cmdline(); "
                "expected=pathlib.Path(sys.argv[2]).resolve(); "
                "assert '--data_dir' in c and pathlib.Path(c[c.index('--data_dir')+1]).resolve()==expected; "
                "assert p.create_time()>=float(sys.argv[3])-2; p.terminate(); p.wait(20)"
            )
            cleaned = subprocess.run([str(ROOT / ".venv" / "Scripts" / "python.exe"), "-B", "-c", cleanup, str(pid), str(data), str(started_at)])
            evidence["cleanup_ok"] = cleaned.returncode == 0
        report = verification / ("acceptance-gpu.json" if gpu else "acceptance.json")
        report.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print("REPORT " + str(report), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpu", action="store_true")
    asyncio.run(main(parser.parse_args().gpu))
