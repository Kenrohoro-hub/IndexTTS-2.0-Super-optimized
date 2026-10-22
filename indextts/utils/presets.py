"""
Preset management utilities for the IndexTTS WebUI.

A preset stores a reusable configuration including the voice reference audio,
emotion control method/settings, and advanced generation parameters.
Each preset lives in its own directory under ``outputs/presets/<name>/``.
"""

import json
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional


PRESET_VERSION = "1.0"
PRESETS_DIRNAME = "outputs/presets"
_PRESET_LOCK = threading.RLock()


def _project_root() -> Path:
    """Return the project root directory."""
    return Path(os.environ.get("INDEXTTS_DATA_DIR", Path(__file__).parent.parent.parent)).resolve()


def get_presets_dir() -> Path:
    """Return the directory where presets are stored."""
    path = _project_root() / PRESETS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_preset_name(name: str) -> str:
    """
    Sanitize a preset name so it can be used as a directory name.

    Strips surrounding whitespace, replaces filesystem-unfriendly characters
    with underscores, and prevents empty or purely-special names.

    Two Windows-specific hazards, both verified against real inference and a
    real ``mkdir``:

    - Reserved device names. A preset directory is created with
      ``outputs/presets/<name>/``, and CON / PRN / AUX / NUL / COM1-9 /
      LPT1-9 all fail there -- ``mkdir`` raises NotADirectoryError (WinError
      267) or FileNotFoundError, so the preset simply cannot be saved.
    - Invisible format characters. U+202E makes Explorer display a directory
      as a different name, and U+200B / U+FEFF create two directories that
      look identical but are impossible to tell apart.
    """
    from indextts.utils.server_helpers import escape_win_reserved, strip_invisible

    name = strip_invisible(str(name or "")).strip()
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", "_", name)
    name = name.strip("._")
    return escape_win_reserved(name) or "untitled"


def list_presets() -> List[str]:
    """Return a sorted list of existing preset names."""
    with _PRESET_LOCK:
        presets_dir = get_presets_dir()
        if not presets_dir.exists():
            return []
        names = [
            p.name
            for p in presets_dir.iterdir()
            if not p.name.startswith(".") and p.is_dir() and (p / "preset.json").is_file()
        ]
        return sorted(names)


def _preset_dir(name: str) -> Path:
    return get_presets_dir() / safe_preset_name(name)


def _copy_audio(src: Optional[str], dst_dir: Path, dst_name: str) -> Optional[str]:
    """
    Copy an uploaded audio file into the preset directory.

    Returns the relative filename written, or None if *src* is empty.
    """
    if not src:
        return None
    src_path = Path(src)
    if not src_path.is_file():
        raise FileNotFoundError(src_path)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_path = dst_dir / dst_name
    shutil.copy2(src_path, dst_path)
    return dst_name


def save_preset(
    name: str,
    data: Dict,
    prompt_audio: Optional[str] = None,
    emo_audio: Optional[str] = None,
    overwrite: bool = True,
) -> List[str]:
    """
    Save a preset to disk.

    Parameters
    ----------
    name:
        Preset display name.
    data:
        Dictionary containing all non-audio preset fields.  See ``load_preset``
        for the expected structure.
    prompt_audio:
        Path to the uploaded voice reference audio, if any.
    emo_audio:
        Path to the uploaded emotion reference audio, if any.
    overwrite:
        Replace an existing preset when True; reject it when False.

    Returns warning messages when an old backup cannot be moved to the Recycle Bin.
    """
    name = safe_preset_name(name)
    with _PRESET_LOCK:
        presets_dir = get_presets_dir()
        preset_dir = presets_dir / name
        if preset_dir.exists() and not overwrite:
            raise FileExistsError(f"预设「{name}」已存在")

        stage_dir = Path(tempfile.mkdtemp(prefix=f".{name}.tmp-", dir=presets_dir))
        backup_dir = None
        warnings = []
        try:
            old_payload = {}
            if preset_dir.is_dir() and (preset_dir / "preset.json").is_file():
                with open(preset_dir / "preset.json", "r", encoding="utf-8") as f:
                    old_payload = json.load(f)

            prompt_rel = _copy_audio(prompt_audio, stage_dir, "prompt.wav")
            emo_rel = _copy_audio(emo_audio, stage_dir, "emo_ref.wav")
            for rel, dst_name in (
                (old_payload.get("prompt_audio"), "prompt.wav"),
                (old_payload.get("emo_audio"), "emo_ref.wav"),
            ):
                if rel and not (prompt_rel if dst_name == "prompt.wav" else emo_rel):
                    old_audio = preset_dir / rel
                    if old_audio.is_file() and old_audio.resolve().parent == preset_dir.resolve():
                        shutil.copy2(old_audio, stage_dir / dst_name)
                        if dst_name == "prompt.wav":
                            prompt_rel = dst_name
                        else:
                            emo_rel = dst_name

            payload = {
                "version": PRESET_VERSION,
                **data,
                "prompt_audio": prompt_rel or "",
                "emo_audio": emo_rel or "",
            }
            with open(stage_dir / "preset.json", "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            if preset_dir.exists():
                backup_dir = Path(tempfile.mkdtemp(prefix=f".{name}.backup-", dir=presets_dir))
                backup_dir.rmdir()
                preset_dir.rename(backup_dir)
            try:
                stage_dir.rename(preset_dir)
            except OSError:
                if backup_dir is not None:
                    backup_dir.rename(preset_dir)
                    backup_dir = None
                raise
            if backup_dir is not None:
                from indextts.utils.recycle import recycle_path

                try:
                    recycle_path(backup_dir)
                except (OSError, NotImplementedError) as exc:
                    warnings.append(f"旧版预设备份保留在 {backup_dir}：{exc}")
            return warnings
        finally:
            if stage_dir.exists():
                shutil.rmtree(stage_dir)


def load_preset(name: str) -> Optional[Dict]:
    """
    Load a preset from disk.

    Returns None if the preset does not exist.
    """
    with _PRESET_LOCK:
        preset_dir = _preset_dir(name)
        preset_file = preset_dir / "preset.json"
        if not preset_file.exists():
            return None

        with open(preset_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Convert relative audio paths to absolute paths.
        if data.get("prompt_audio"):
            data["prompt_audio"] = str(preset_dir / data["prompt_audio"])
        if data.get("emo_audio"):
            data["emo_audio"] = str(preset_dir / data["emo_audio"])

        return data


def delete_preset(name: str) -> bool:
    """
    Delete a preset directory.

    Returns True if the preset existed and was removed, False otherwise.
    """
    from indextts.utils.recycle import recycle_path

    with _PRESET_LOCK:
        preset_dir = _preset_dir(name)
        if not preset_dir.exists():
            return False
        recycle_path(preset_dir)
        return True


def preset_exists(name: str) -> bool:
    """Return whether a preset with the given name already exists."""
    with _PRESET_LOCK:
        return _preset_dir(name).exists()


def rename_preset(old_name: str, new_name: str) -> str:
    """
    Rename a preset directory.

    Returns the sanitized new name.
    Raises ValueError if the source preset is missing or the target name exists.
    """
    with _PRESET_LOCK:
        old_dir = _preset_dir(old_name)
        if not old_dir.is_dir():
            raise ValueError(f"预设「{old_name}」不存在")
        new_safe = safe_preset_name(new_name)
        new_dir = get_presets_dir() / new_safe
        if new_dir == old_dir:
            return new_safe
        if new_dir.exists():
            raise ValueError(f"预设「{new_safe}」已存在")
        old_dir.rename(new_dir)
        return new_safe


def duplicate_preset(src_name: str, dst_name: str) -> str:
    """
    Copy a preset directory under a new name.

    Returns the sanitized destination name.
    Raises ValueError if the source preset is missing or the target name exists.
    """
    with _PRESET_LOCK:
        src_dir = _preset_dir(src_name)
        if not src_dir.is_dir():
            raise ValueError(f"预设「{src_name}」不存在")
        dst_safe = safe_preset_name(dst_name)
        dst_dir = get_presets_dir() / dst_safe
        if dst_dir.exists():
            raise ValueError(f"预设「{dst_safe}」已存在")
        shutil.copytree(src_dir, dst_dir)
        return dst_safe
