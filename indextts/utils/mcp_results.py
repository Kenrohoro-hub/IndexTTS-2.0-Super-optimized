"""Local MCP deliverables, independent of the WebUI history retention policy."""
import json
import os
import re
import shutil
import uuid
import wave
from pathlib import Path


TERMINAL_STATES = {"done", "error", "cancelled", "stopped", "interrupted"}


def audio_info(path):
    path = Path(path)
    with wave.open(str(path), "rb") as audio:
        return {
            "audio_path": str(path.resolve()),
            "duration_seconds": round(audio.getnframes() / audio.getframerate(), 4),
            "sample_rate": audio.getframerate(),
            "channels": audio.getnchannels(),
            "size_bytes": path.stat().st_size,
        }


class ResultStore:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()

    def _path(self, item_id, suffix):
        if not re.fullmatch(r"[0-9a-f]{12,32}", item_id):
            raise ValueError("无效的任务或音频编号")
        return self.directory / (item_id + suffix)

    def save_audio(self, source, item_id):
        target = self._path(item_id, ".wav")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            if isinstance(source, bytes):
                temporary.write_bytes(source)
            else:
                shutil.copyfile(source, temporary)
            audio_info(temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return {"artifact_id": item_id, **audio_info(target)}

    def save_job(self, job):
        target = self._path(job["job_id"], ".json")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(job, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def load_job(self, job_id, instance_id):
        target = self._path(job_id, ".json")
        if not target.is_file():
            return None
        job = json.loads(target.read_text(encoding="utf-8"))
        if job["state"] not in TERMINAL_STATES and job.get("instance_id") != instance_id:
            job = {**job, "state": "interrupted", "error": "后台已重启，未完成的任务不会自动重跑"}
        result = job.get("result")
        if result and result.get("artifact_id"):
            path = self._path(result["artifact_id"], ".wav")
            result["audio_path"] = str(path)
            result["available"] = path.is_file()
        return job

    def audio_path(self, artifact_id):
        path = self._path(artifact_id, ".wav")
        if not path.is_file():
            raise FileNotFoundError("交付音频不存在或已被移走")
        return path

    def find_client(self, client_id, instance_id):
        candidates = []
        for path in self.directory.glob("*.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if job.get("client_id") == client_id:
                candidates.append(job)
        latest = max(candidates, key=lambda item: item.get("created_at", 0), default=None)
        return self.load_job(latest["job_id"], instance_id) if latest else None
