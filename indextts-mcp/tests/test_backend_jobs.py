"""Exercise the shared job flow with a deterministic, CPU-only model double."""
import importlib.util
import inspect
import io
import json
from pathlib import Path
import sys
import threading
import time
import wave
from types import SimpleNamespace

import pytest
from starlette.datastructures import UploadFile

from indextts.utils.mcp_results import ResultStore
from indextts.utils.server_helpers import cap_prompt_files


def wave_bytes():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(22050)
        out.writeframes(b"\x00\x00" * 2205)
    return stream.getvalue()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    root = Path(__file__).resolve().parents[2]
    data = tmp_path_factory.mktemp("backend")
    patch = pytest.MonkeyPatch()
    patch.setenv("INDEXTTS_DATA_DIR", str(data))
    patch.setattr(sys, "argv", [str(root / "server.py"), "--data_dir", str(data)])
    spec = importlib.util.spec_from_file_location("indextts_test_server", root / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Model:
        gr_progress = None
        mel_tokens_capped = False
        text_normalize_fallback = None
        duration = 0.03
        fail = False

        def normalize_emo_vec(self, values, apply_bias=True):
            assert module._INFER_LOCK.locked()
            return values

        def infer(self, **kwargs):
            assert Path(kwargs["spk_audio_prompt"]).is_file()
            for i in range(3):
                time.sleep(self.duration)
                self.gr_progress(i / 3, "test")
            if self.fail:
                raise RuntimeError("simulated model error")
            Path(kwargs["output_path"]).write_bytes(wave_bytes())
            return kwargs["output_path"]

    model = Model()

    class TTS:
        loaded = True

        def ensure_loaded(self):
            assert module._INFER_LOCK.locked()
            assert module._JOBS
            return model

    patch.setattr(module, "tts", TTS())
    # Recycle behavior has its own Windows smoke check; this fixture owns all files.
    patch.setattr(module, "recycle_path", lambda p: Path(p).unlink())
    module.test_model = model
    yield module
    for handler in list(module.logger.handlers):
        handler.close()
        module.logger.removeHandler(handler)
    patch.undo()


def submit(server, **overrides):
    values = {name: p.default.default for name, p in inspect.signature(server.do_tts).parameters.items()}
    values.update(text="测试。", spk_audio=UploadFile(io.BytesIO(wave_bytes()), filename="voice.wav"), response_mode="job")
    values.update(overrides)
    response = server.do_tts(**values)
    return json.loads(response.body)


def wait_job(server, job_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = server.jobs_detail(job_id)["job"]
        if job["state"] in {"done", "error", "stopped", "cancelled"} and job.get("finished_at"):
            return job
        time.sleep(0.02)
    pytest.fail("job did not finish")


def test_job_delivery_survives_history_and_snapshot_expiry(server):
    submitted = submit(server, emo_mode="2", vec1=0.2)
    result = wait_job(server, submitted["job_id"])
    assert result["state"] == "done"
    artifact = Path(result["result"]["audio_path"])
    assert artifact.is_file()
    server.history_remove(result["result"]["history_id"])
    assert artifact.is_file()
    with server._JOBS_LOCK:
        server._JOBS[submitted["job_id"]]["finished_at"] = time.time() - 301
    recovered = server.jobs_detail(submitted["job_id"])["job"]
    assert recovered["result"]["available"] is True
    assert recovered["result"]["audio_path"] == str(artifact)


def test_queue_cancel_and_stop_leave_next_generation_usable(server):
    server.test_model.duration = 0.15
    first = submit(server)
    second = submit(server)
    server.tts_stop(second["job_id"], keep_model=True)
    server.tts_stop(first["job_id"], keep_model=True)
    server._prune_finished_jobs()
    assert wait_job(server, second["job_id"])["state"] == "cancelled"
    assert wait_job(server, first["job_id"])["state"] in {"stopped", "cancelled"}
    server.test_model.duration = 0.01
    assert wait_job(server, submit(server)["job_id"])["state"] == "done"
    assert not server._PROMPT_REFS


def test_model_failure_has_terminal_error_and_releases_references(server):
    server.test_model.fail = True
    try:
        job = wait_job(server, submit(server)["job_id"])
        assert job["state"] == "error"
        assert "simulated" in job["error"]
    finally:
        server.test_model.fail = False
    assert not server._PROMPT_REFS


def test_invalid_audio_and_mode_do_not_register_jobs(server):
    before = set(server._JOBS)
    with pytest.raises(server.HTTPException):
        submit(server, spk_audio=UploadFile(io.BytesIO(b"not audio"), filename="fake.wav"))
    with pytest.raises(server.HTTPException):
        submit(server, emo_mode="1")
    with pytest.raises(server.HTTPException):
        submit(server, temperature=float("nan"))
    assert set(server._JOBS) == before
    assert not server._PROMPT_REFS


def test_active_prompt_survives_cache_eviction(tmp_path):
    active = tmp_path / "active.wav"
    active.write_bytes(wave_bytes())
    for i in range(31):
        (tmp_path / f"{i}.wav").write_bytes(wave_bytes())
    cap_prompt_files(str(tmp_path), keep=2, protected=[str(active)])
    assert active.is_file()
    assert len(list(tmp_path.glob("*.wav"))) == 2


def test_result_store_restart_and_path_boundary(tmp_path):
    store = ResultStore(tmp_path)
    job_id = "a" * 12
    store.save_job({"job_id": job_id, "instance_id": "old", "state": "running", "client_id": "mcp-example"})
    assert store.load_job(job_id, "new")["state"] == "interrupted"
    assert store.find_client("mcp-example", "new")["job_id"] == job_id
    with pytest.raises(ValueError):
        store.audio_path("../escape")


def test_model_reload_remains_pending_until_running_flags_match(server, monkeypatch):
    model = server.LazyTTS("unused", "unused", use_fp16=True, use_s2mel_fp16=True)
    model._tts = SimpleNamespace(use_fp16=True, use_s2mel_fp16=True, use_w2v_fp16=True, use_qwen_fp16=True)
    monkeypatch.setattr(model, "_apply_backend_flags", lambda: None)
    monkeypatch.setattr(server, "tts", model)
    assert server.model_config({"fp16": False})["reload_required"]
    assert server.model_config({"fp16": False})["reload_required"]
    assert server.model_config({"diffusion_steps": 20})["reload_required"]
    assert not server.model_config({"fp16": True})["reload_required"]
