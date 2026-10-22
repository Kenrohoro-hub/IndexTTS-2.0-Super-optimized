"""Loopback access to the existing IndexTTS process; never imports its model."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import quote
import uuid
import wave

import requests


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from indextts.utils.presets import safe_preset_name

AUDIO_MAX_BYTES = 20 * 1024 * 1024
_START_LOCK = threading.Lock()


def read_audio(path):
    source = Path(path).expanduser()
    if not source.is_absolute() or not source.is_file():
        raise ValueError("请提供存在的本地音频绝对路径")
    if source.stat().st_size > AUDIO_MAX_BYTES:
        raise ValueError("参考或编辑音频不能超过 20 MB")
    with source.open("rb") as stream:
        data = stream.read(AUDIO_MAX_BYTES + 1)
    if not data or len(data) > AUDIO_MAX_BYTES:
        raise ValueError("音频为空或超过 20 MB")
    valid = len(data) >= 12 and (
        (data[:4] == b"RIFF" and data[8:12] == b"WAVE")
        or data[:4] in (b"OggS", b"fLaC") or data.startswith(b"ID3")
        or (data[0] == 255 and data[1] & 224 == 224) or data[4:8] == b"ftyp"
    )
    if not valid:
        raise ValueError("文件不是支持的音频（WAV/MP3/FLAC/OGG/M4A）")
    return source.name, data, "application/octet-stream"


def form_values(values):
    return {k: str(v).lower() if isinstance(v, bool) else v for k, v in values.items() if v is not None}


@contextmanager
def startup_lock(path):
    import msvcrt
    path.parent.mkdir(parents=True, exist_ok=True)
    with _START_LOCK, path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + 65
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("其他客户端正在启动后台，请稍后查询状态")
                time.sleep(0.2)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


class Backend:
    def __init__(self):
        self.root = PROJECT_ROOT
        self.port = int(os.environ.get("INDEXTTS_PORT", "7860"))
        if not 1024 <= self.port <= 65535:
            raise ValueError("INDEXTTS_PORT 必须在 1024 到 65535 之间")
        self.data_dir = Path(os.environ.get("INDEXTTS_DATA_DIR", str(self.root))).resolve()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.project_id = hashlib.sha256(os.path.normcase(str(self.root)).encode("utf-8")).hexdigest()[:16]

    def _http(self, method, path, *, timeout=120, **kwargs):
        with requests.Session() as session:
            session.trust_env = False
            response = session.request(method, self.base_url + path, timeout=(3, timeout),
                                       allow_redirects=False, **kwargs)
        if response.status_code >= 300:
            try:
                detail = response.json().get("detail", response.text[:500])
            except ValueError:
                detail = response.text[:500]
            raise RuntimeError(f"后台返回 {response.status_code}：{detail}")
        return response

    def health(self):
        try:
            data = self._http("GET", "/api/v1/health", timeout=3).json()
        except requests.ConnectionError:
            return None
        except (requests.Timeout, ValueError) as e:
            raise RuntimeError("端口有响应但无法确认 IndexTTS 后台身份") from e
        if data.get("service") != "indextts2" or data.get("project_id") != self.project_id:
            raise RuntimeError("该端口并非此项目的 IndexTTS 后台，请检查端口；不会替换或停止现有进程")
        if data.get("mcp_api_version") != 1:
            raise RuntimeError("后台尚未加载 MCP 接口版本，请正常关闭旧后台后重新启动")
        if Path(data.get("data_dir", "")).resolve() != self.data_dir:
            raise RuntimeError("后台使用的数据目录与 MCP 配置不一致")
        return data

    def ensure_service(self):
        health = self.health()
        if health:
            return {"ok": True, "started": False, **health}
        lock = self.root / "indextts-mcp" / ".state" / f"start-{self.port}.lock"
        with startup_lock(lock):
            health = self.health()
            if health:
                return {"ok": True, "started": False, **health}
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", self.port)) == 0:
                    raise RuntimeError("端口已被其他服务占用；未启动或停止任何进程")
            logs = self.data_dir / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            log_path = logs / "mcp-backend.log"
            # MCP clients may kill their entire Windows Job Object on disconnect.
            # WMI starts the shared backend outside that client-owned process tree.
            import pythoncom
            from win32com.client import GetObject
            command = subprocess.list2cmdline([
                str(self.root / ".venv" / "Scripts" / "python.exe"), "-X", "utf8", "-B", "-u", str(Path(__file__)),
                "--port", str(self.port), "--data_dir", str(self.data_dir),
            ])
            pythoncom.CoInitialize()
            service = startup = process_class = parameters = result = None
            try:
                service = GetObject(r"winmgmts:root\cimv2")
                startup = service.Get("Win32_ProcessStartup").SpawnInstance_()
                startup.ShowWindow = 0
                process_class = service.Get("Win32_Process")
                parameters = process_class.Methods_("Create").InParameters.SpawnInstance_()
                parameters.CommandLine = command
                parameters.CurrentDirectory = str(self.root)
                parameters.ProcessStartupInformation = startup
                result = process_class.ExecMethod_("Create", parameters)
                pid, return_code = result.ProcessId, result.ReturnValue
            except Exception as error:
                raise RuntimeError(f"Windows 后台启动失败：{error}；可先用原启动脚本打开配音后台后重试") from error
            finally:
                result = parameters = process_class = startup = service = None
                pythoncom.CoUninitialize()
            if return_code:
                raise RuntimeError(f"Windows 后台启动失败（WMI 返回 {return_code}）")
            # This receipt is diagnostic only; it never authorizes killing a PID.
            (lock.parent / f"backend-{self.port}.json").write_text(
                json.dumps({"pid": pid, "project_id": self.project_id, "log": str(log_path)}),
                encoding="utf-8",
            )
            deadline = time.monotonic() + 55
            while time.monotonic() < deadline:
                health = self.health()
                if health:
                    return {"ok": True, "started": True, **health}
                time.sleep(0.25)
            raise RuntimeError(f"后台尚未就绪；进程未被强制终止，请稍后查询。日志：{log_path}")

    def request(self, method, path, **kwargs):
        self.ensure_service()
        response = self._http(method, "/api/v1" + path, **kwargs)
        data = response.json()
        if data.get("ok") is False:
            raise RuntimeError(data.get("error") or "后台操作失败")
        return data

    def get_status(self):
        health = self.health()
        if health is None:
            return {"online": False, "message": "后台未启动，可调用 ensure_service 或直接提交配音"}
        return {"online": True, **health, **self._http("GET", "/api/v1/status").json()}

    def preset(self, name):
        return self.request("GET", "/presets/" + quote(name, safe=""))

    def _preset_audio(self, name, kind):
        self.ensure_service()
        response = self._http("GET", "/api/v1/presets/" + quote(name, safe="") + "/audio/" + kind)
        return kind + ".wav", response.content, "audio/wav"

    def prepare_voice(self, preset_name=None, reference_audio_path=None, example_name=None):
        if sum(bool(v) for v in (preset_name, reference_audio_path, example_name)) != 1:
            raise ValueError("preset_name、reference_audio_path、example_name 必须且只能提供一个")
        values, files = {}, {}
        if preset_name:
            preset = self.preset(preset_name)
            values.update(preset.get("advanced_params") or {})
            values.update({"emo_mode": preset.get("emo_control_method", 0),
                           "emo_weight": preset.get("emo_weight", 0.65),
                           "emo_text": preset.get("emo_text", ""),
                           "use_random": preset.get("emo_random", False)})
            values.update({f"vec{i}": value for i, value in enumerate(preset.get("emo_vector") or [0] * 8, 1)})
            if not preset.get("prompt_audio_url"):
                raise ValueError("该预设没有音色参考音频，请先保存参考音频")
            files["spk_audio"] = self._preset_audio(preset_name, "prompt")
            if preset.get("emo_audio_url"):
                files["emo_audio"] = self._preset_audio(preset_name, "emo_ref")
        elif reference_audio_path:
            files["spk_audio"] = read_audio(reference_audio_path)
        else:
            if Path(example_name).name != example_name or "/" in example_name or "\\" in example_name:
                raise ValueError("example_name 必须是 list_examples 返回的音频文件名")
            source = self.root / "examples" / example_name
            files["spk_audio"] = read_audio(str(source))
        return values, files

    def synthesize(self, text, preset_name, reference_audio_path, example_name, overrides, emotion_audio_path):
        values, files = self.prepare_voice(preset_name, reference_audio_path, example_name)
        values.update(overrides)
        if emotion_audio_path:
            files["emo_audio"] = read_audio(emotion_audio_path)
        if int(values.get("emo_mode", 0)) == 1 and "emo_audio" not in files:
            raise ValueError("情感音频模式需要 emotion_audio_path 或预设中的情感音频")
        submission_id = "mcp-" + uuid.uuid4().hex
        values.update(text=text, client_id=submission_id, response_mode="job")
        try:
            result = self.request("POST", "/tts", data=form_values(values), files=files, timeout=40)
        except (requests.Timeout, requests.ConnectionError) as e:
            raise RuntimeError(f"提交结果不确定，禁止直接重复提交。请调用 get_task(submission_id='{submission_id}') 查询") from e
        result["submission_id"] = submission_id
        result["next_action"] = "使用 get_task(job_id) 查询；生成完成后返回本地 audio_path"
        result.setdefault("warnings", []).append("参考音频最多使用前 15 秒")
        return result

    def get_task(self, job_id=None, submission_id=None):
        if bool(job_id) == bool(submission_id):
            raise ValueError("job_id 与 submission_id 必须且只能提供一个")
        if job_id:
            return self.request("GET", "/jobs/" + quote(job_id, safe=""))
        return self.request("GET", "/jobs/current", params={"client_id": submission_id})

    def edit_audio(self, audio_path, **values):
        return self.request("POST", "/audio/edit", files={"file": read_audio(audio_path)},
                            data=form_values(values))

    def export_audio(self, audio_path, destination_path, overwrite=False):
        import shutil
        source = Path(audio_path).resolve(strict=True)
        with wave.open(str(source), "rb") as audio:
            info = {"duration_seconds": audio.getnframes() / audio.getframerate(),
                    "sample_rate": audio.getframerate(), "channels": audio.getnchannels()}
        target = Path(destination_path).expanduser()
        if not target.is_absolute() or target.suffix.lower() != ".wav":
            raise ValueError("destination_path 必须是以 .wav 结尾的绝对文件路径")
        target = target.resolve()
        if source == target:
            raise ValueError("导出目标必须与源文件不同，原音频保持不变")
        if target.exists() and not overwrite:
            raise FileExistsError("目标文件已存在；需要替换时显式设置 overwrite=true")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name("." + uuid.uuid4().hex + ".tmp")
        try:
            shutil.copyfile(source, temporary)
            if overwrite:
                os.replace(temporary, target)
            else:
                # Windows rename refuses an existing destination even after a concurrent create.
                temporary.rename(target)
        finally:
            temporary.unlink(missing_ok=True)
        return {"ok": True, "audio_path": str(target), "size_bytes": target.stat().st_size, **info}


if __name__ == "__main__":
    import argparse
    import runpy
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--data_dir", required=True)
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve()
    os.environ["INDEXTTS_DATA_DIR"] = str(data_dir)
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / "mcp-backend.log").open("a", encoding="utf-8", buffering=1) as log:
        sys.stdout = sys.stderr = log
        sys.argv = [str(PROJECT_ROOT / "server.py"), "--host", "127.0.0.1", "--port", args.port,
                    "--data_dir", str(data_dir)]
        try:
            runpy.run_path(sys.argv[0], run_name="__main__")
        except BaseException:
            import traceback
            traceback.print_exc()
            raise SystemExit(1)
