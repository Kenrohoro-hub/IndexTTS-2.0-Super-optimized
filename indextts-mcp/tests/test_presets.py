from pathlib import Path
import shutil

import pytest

from indextts.utils import presets


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(presets, "_project_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def mock_recycle_bin(monkeypatch):
    monkeypatch.setattr("indextts.utils.recycle.recycle_path", lambda path: shutil.rmtree(path))


def _audio(path: Path, content: bytes = b"audio") -> Path:
    path.write_bytes(content)
    return path


def test_data_directory_environment_variable_overrides_project_root(tmp_path, monkeypatch):
    monkeypatch.setenv("INDEXTTS_DATA_DIR", str(tmp_path))

    assert presets.get_presets_dir() == tmp_path / "outputs" / "presets"


def test_save_replaces_preset_and_preserves_unspecified_audio(data_root, tmp_path):
    prompt = _audio(tmp_path / "prompt.wav", b"prompt")
    emo = _audio(tmp_path / "emo.wav", b"emotion")
    presets.save_preset("voice", {"text": "old"}, str(prompt), str(emo))

    assert presets.save_preset("voice", {"text": "new"}) == []

    saved = presets.load_preset("voice")
    assert saved["text"] == "new"
    assert Path(saved["prompt_audio"]).read_bytes() == b"prompt"
    assert Path(saved["emo_audio"]).read_bytes() == b"emotion"


def test_recycle_failure_keeps_new_preset_and_reports_hidden_backup(data_root, monkeypatch):
    presets.save_preset("voice", {"text": "old"})

    def fail_recycle(path):
        raise OSError("recycle failed")

    monkeypatch.setattr("indextts.utils.recycle.recycle_path", fail_recycle)
    warnings = presets.save_preset("voice", {"text": "new"})
    backups = list(presets.get_presets_dir().glob(".voice.backup-*"))

    assert len(backups) == 1
    assert warnings and str(backups[0]) in warnings[0]
    assert presets.load_preset("voice")["text"] == "new"
    assert presets.list_presets() == ["voice"]


def test_save_rejects_existing_name_when_overwrite_disabled(data_root):
    presets.save_preset("voice", {"text": "old"})

    with pytest.raises(FileExistsError):
        presets.save_preset("voice", {"text": "new"}, overwrite=False)

    assert presets.load_preset("voice")["text"] == "old"


def test_missing_supplied_audio_fails_without_changing_existing_preset(data_root, tmp_path):
    presets.save_preset("voice", {"text": "old"})

    with pytest.raises(FileNotFoundError):
        presets.save_preset("voice", {"text": "new"}, str(tmp_path / "missing.wav"))

    assert presets.load_preset("voice")["text"] == "old"


def test_json_write_failure_keeps_existing_preset(data_root, monkeypatch):
    presets.save_preset("voice", {"text": "old"})
    original_dump = presets.json.dump

    def fail_dump(*args, **kwargs):
        raise OSError("write failed")

    monkeypatch.setattr(presets.json, "dump", fail_dump)
    with pytest.raises(OSError, match="write failed"):
        presets.save_preset("voice", {"text": "new"})
    monkeypatch.setattr(presets.json, "dump", original_dump)

    assert presets.load_preset("voice")["text"] == "old"
    assert not list(presets.get_presets_dir().glob("*.tmp-*"))


def test_directory_switch_failure_restores_existing_preset(data_root, monkeypatch):
    presets.save_preset("voice", {"text": "old"})
    original_rename = Path.rename
    preset_dir = presets._preset_dir("voice")

    def fail_stage_switch(path, target):
        if path.name.startswith(".voice.tmp-") and Path(target) == preset_dir:
            raise OSError("replace failed")
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_stage_switch)
    with pytest.raises(OSError, match="replace failed"):
        presets.save_preset("voice", {"text": "new"})

    assert presets.load_preset("voice")["text"] == "old"


def test_delete_failure_leaves_preset_in_place(data_root, monkeypatch):
    presets.save_preset("voice", {"text": "old"})

    def fail_recycle(path):
        raise OSError("recycle failed")

    monkeypatch.setattr("indextts.utils.recycle.recycle_path", fail_recycle)
    with pytest.raises(OSError, match="recycle failed"):
        presets.delete_preset("voice")

    assert presets.load_preset("voice")["text"] == "old"


def test_delete_uses_recycle_bin(data_root, monkeypatch):
    presets.save_preset("voice", {"text": "old"})
    received = []

    monkeypatch.setattr("indextts.utils.recycle.recycle_path", received.append)
    assert presets.delete_preset("voice") is True

    assert received == [presets._preset_dir("voice")]
    assert Path(received[0]).is_dir()
