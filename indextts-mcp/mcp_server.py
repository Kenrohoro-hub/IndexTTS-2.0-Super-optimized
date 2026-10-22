"""IndexTTS2 local MCP server. stdout is reserved for the MCP transport."""
import json
import logging
import sys
import wave
from functools import wraps
from typing import Annotated, Any, Literal
from urllib.parse import quote

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field
from requests import RequestException

from backend import Backend, form_values, read_audio, safe_preset_name


logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
backend = Backend()
mcp = MCPServer(
    "IndexTTS2", version="1.0.0", log_level="WARNING",
    instructions=("本机配音工具。先选择音色或预设，synthesize 返回 job_id 后用 get_task 查询。"
                  "完成时 audio_path 是已保存的本地 WAV。不得因调用超时直接重复提交。"
                  "停止任务后须查询到 stopped/cancelled 才算停止。模型和术语表与网页共享。"
                  "无需打开网页；get_status 不会启动后台，其他业务工具按需启动后台。"),
)
READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
CHANGE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def tool(*, annotations):
    def register(fn):
        @wraps(fn)
        def invoke(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (ValueError, RuntimeError, OSError, RequestException, wave.Error) as error:
                raise ToolError(str(error)) from error
        return mcp.tool(annotations=annotations)(invoke)
    return register


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class GenerationOptions(Options):
    do_sample: bool | None = None
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = Field(default=None, ge=0, le=100, description="0 表示不限制 top_k")
    temperature: float | None = Field(default=None, ge=0.1, le=2)
    length_penalty: float | None = Field(default=None, ge=-2, le=2)
    num_beams: int | None = Field(default=None, ge=1, le=10)
    repetition_penalty: float | None = Field(default=None, ge=0.1, le=20)
    max_mel_tokens: int | None = Field(default=None, ge=50, le=1815)


class EmotionVector(Options):
    happy: float = Field(default=0, ge=0, le=1)
    angry: float = Field(default=0, ge=0, le=1)
    sad: float = Field(default=0, ge=0, le=1)
    afraid: float = Field(default=0, ge=0, le=1)
    disgusted: float = Field(default=0, ge=0, le=1)
    melancholic: float = Field(default=0, ge=0, le=1)
    surprised: float = Field(default=0, ge=0, le=1)
    calm: float = Field(default=0, ge=0, le=1)


class ModelOptions(Options):
    fp16: bool | None = None
    s2mel_fp16: bool | None = None
    w2v_fp16: bool | None = None
    qwen_fp16: bool | None = None
    cudnn_benchmark: bool | None = None
    diffusion_steps: int | None = Field(default=None, ge=1, le=50)
    inference_cfg_rate: float | None = Field(default=None, ge=0, le=1)


EmotionMode = Literal["speaker", "audio", "vector", "text"]
Text = Annotated[str, Field(min_length=1, max_length=10000)]
Name = Annotated[str, Field(min_length=1, max_length=120)]
Weight = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
SegmentTokens = Annotated[int, Field(ge=20, le=600)]
Pause = Annotated[int, Field(ge=10, le=500)]


def generation_overrides(emotion_mode, emotion_weight, emotion_text, emotion_vector,
                         use_random, segment_tokens, advanced, emotion_audio_path=None):
    if emotion_text is not None and not emotion_text.strip():
        raise ValueError("emotion_text 不能是空白；使用音色自带情感时请省略该参数")
    values = advanced.model_dump(exclude_none=True) if advanced else {}
    provided = {"audio": bool(emotion_audio_path), "text": emotion_text is not None,
                "vector": emotion_vector is not None}
    active = [mode for mode, present in provided.items() if present]
    if len(active) > 1:
        raise ValueError("情感音频、情感文字、情感向量只能选择一种")
    if active:
        if emotion_mode and emotion_mode != active[0]:
            raise ValueError("emotion_mode 与提供的情感输入不一致")
        emotion_mode = active[0]
    if emotion_mode:
        values["emo_mode"] = {"speaker": 0, "audio": 1, "vector": 2, "text": 3}[emotion_mode]
    if emotion_weight is not None:
        values["emo_weight"] = emotion_weight
    if emotion_text is not None:
        if len(emotion_text) > 500:
            raise ValueError("情感描述不能超过 500 字")
        values["emo_text"] = emotion_text
    if emotion_vector is not None:
        values.update({f"vec{i}": v for i, v in enumerate(emotion_vector.model_dump().values(), 1)})
    if use_random is not None:
        values["use_random"] = use_random
    if segment_tokens is not None:
        values["max_text_tokens_per_segment"] = segment_tokens
    return values


@tool(annotations=READ)
def get_status() -> dict[str, Any]:
    """查看后台、模型、CPU/显存和生成状态；后台未启动时只报告离线。"""
    return backend.get_status()


@tool(annotations=WRITE)
def ensure_service() -> dict[str, Any]:
    """连接当前项目的后台，必要时在本机启动；不会预加载模型或打开浏览器。"""
    return backend.ensure_service()


@tool(annotations=READ)
def list_presets() -> dict[str, Any]:
    """列出已保存的音色预设名称；可用 get_preset 查看参考音频和参数。"""
    return backend.request("GET", "/presets")


@tool(annotations=READ)
def get_preset(name: Name) -> dict[str, Any]:
    """读取一个预设的情绪、生成参数和参考音频信息。"""
    return backend.preset(name)


@tool(annotations=READ)
def list_examples() -> dict[str, Any]:
    """列出内置示例；voice_files 中的文件名可直接用作 synthesize 的 example_name。"""
    result = backend.request("GET", "/examples", params={"include_experimental": "true"})
    result["voice_files"] = sorted(p.name for p in (backend.root / "examples").glob("*.wav"))
    return result


@tool(annotations=READ)
def preview_text(text: Text, segment_tokens: SegmentTokens = 120) -> dict[str, Any]:
    """查看术语替换、文本规范化后的分段；会按需加载分词组件，不加载完整语音模型。"""
    return backend.request("POST", "/segments", data={"text": text, "max_text_tokens_per_segment": segment_tokens})


@tool(annotations=WRITE)
def synthesize(
    text: Text, preset_name: str | None = None, reference_audio_path: str | None = None,
    example_name: str | None = None, emotion_mode: EmotionMode | None = None,
    emotion_audio_path: str | None = None, emotion_text: str | None = None,
    emotion_vector: EmotionVector | None = None, emotion_weight: Weight | None = None,
    use_random: bool | None = None, segment_tokens: SegmentTokens | None = None,
    pause_ms: Pause = 200, advanced: GenerationOptions | None = None,
    file_naming: Literal["title_time", "title", "default"] = "title_time",
) -> dict[str, Any]:
    """提交配音，立即返回 job_id，再调用 get_task。三种音色来源必须选一个。

    本次参数覆盖预设，未指定参数沿用预设或后台默认。音频使用本地绝对路径，最多20MB，
    参考音频取前15秒。speaker跟随音色，audio参考情感音频，vector八维情感，text文字情绪。
    只提供一种情感输入时自动选择对应模式。长文超过2000字会提示，10000字为硬上限。
    """
    if not text.strip():
        raise ValueError("配音文本不能为空白")
    values = generation_overrides(emotion_mode, emotion_weight, emotion_text, emotion_vector,
                                  use_random, segment_tokens, advanced, emotion_audio_path)
    values.update(interval_silence=pause_ms, file_naming=file_naming)
    return backend.synthesize(text, preset_name, reference_audio_path, example_name, values, emotion_audio_path)


@tool(annotations=READ)
def get_task(job_id: str | None = None, submission_id: str | None = None) -> dict[str, Any]:
    """查询任务进度或结果。完成状态为done，result.audio_path是独立保存的本地WAV。

    通常传job_id；提交响应丢失时可传错误消息中的submission_id，二者只能选一个。
    mel_capped=true表示音频可能截断；normalize_fallback表示读音规范化曾回退。
    """
    return backend.get_task(job_id, submission_id)


@tool(annotations=CHANGE)
def stop_task(job_id: str) -> dict[str, Any]:
    """取消指定排队任务或请求停止正在生成的任务，保留模型；继续查询到终态才算停止。"""
    return backend.request("POST", "/tts/" + quote(job_id, safe="") + "/stop", params={"keep_model": "true"})


@tool(annotations=WRITE)
def edit_audio(
    audio_path: str,
    start_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0,
    end_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None,
    rate: Annotated[float, Field(ge=0.5, le=2, allow_inf_nan=False)] = 1,
    gain_db: Annotated[float, Field(ge=-24, le=24, allow_inf_nan=False)] = 0,
) -> dict[str, Any]:
    """按源音频秒数裁剪→保音高变速→调音量，另存新WAV，保留原文件。

    输入为不超过20MB的本地WAV。clipped表示发生削波，safe_gain_db是建议增益上限。
    变速沿用网页引擎，会将多声道混为单声道；不变速时保留声道。
    """
    return backend.edit_audio(audio_path, start_seconds=start_seconds, end_seconds=end_seconds, rate=rate, gain_db=gain_db)


@tool(annotations=CHANGE)
def export_audio(audio_path: str, destination_path: str, overwrite: bool = False) -> dict[str, Any]:
    """将本地WAV复制到明确指定的绝对文件路径；默认拒绝覆盖，源文件保持不变。"""
    return backend.export_audio(audio_path, destination_path, overwrite)


@tool(annotations=CHANGE)
def save_preset(
    name: Name, reference_audio_path: str | None = None, emotion_audio_path: str | None = None,
    emotion_mode: EmotionMode | None = None, emotion_text: str | None = None,
    emotion_vector: EmotionVector | None = None, emotion_weight: Weight | None = None,
    use_random: bool | None = None, segment_tokens: SegmentTokens | None = None,
    advanced: GenerationOptions | None = None, overwrite: bool = False,
) -> dict[str, Any]:
    """保存音色和参数；同名默认拒绝覆盖。覆盖时保留未提供的新音频和未修改参数。

    预设不保存段间停顿。文件与参数全部写入成功后才替换旧预设。
    """
    name = safe_preset_name(name)
    values = {}
    if overwrite and name in backend.request("GET", "/presets")["presets"]:
        preset = backend.preset(name)
        values.update(preset.get("advanced_params") or {})
        values.update(emo_mode=preset.get("emo_control_method", 0), emo_weight=preset.get("emo_weight", 0.65),
                      emo_text=preset.get("emo_text", ""), use_random=preset.get("emo_random", False))
        values.update({f"vec{i}": v for i, v in enumerate(preset.get("emo_vector") or [0] * 8, 1)})
    values.update(generation_overrides(emotion_mode, emotion_weight, emotion_text, emotion_vector,
                                       use_random, segment_tokens, advanced, emotion_audio_path))
    values["emo_control_method"] = values.pop("emo_mode", 0)
    values.update(name=name, overwrite=overwrite)
    files = {}
    if reference_audio_path:
        files["prompt_audio"] = read_audio(reference_audio_path)
    if emotion_audio_path:
        files["emo_audio"] = read_audio(emotion_audio_path)
    return backend.request("POST", "/presets", data=form_values(values), files=files or None)


@tool(annotations=CHANGE)
def rename_preset(name: Name, new_name: Name) -> dict[str, Any]:
    """重命名预设，保留音频与参数；目标名称已存在时拒绝。"""
    return backend.request("POST", "/presets/" + quote(name, safe="") + "/rename", json={"name": new_name})


@tool(annotations=WRITE)
def duplicate_preset(name: Name, new_name: Name) -> dict[str, Any]:
    """复制音色预设及参考音频到新名称，拒绝覆盖已有预设。"""
    return backend.request("POST", "/presets/" + quote(name, safe="") + "/duplicate", json={"name": new_name})


@tool(annotations=CHANGE)
def delete_preset(name: Name) -> dict[str, Any]:
    """将指定预设连同其中的参考音频移入Windows回收站；影响网页共用的预设库。"""
    return backend.request("DELETE", "/presets/" + quote(name, safe=""))


@tool(annotations=READ)
def get_glossary() -> dict[str, Any]:
    """读取网页与MCP共用的术语读音表及级联替换开关。"""
    return backend.request("GET", "/glossary")


@tool(annotations=CHANGE)
def set_pronunciation(term: Name, reading_zh: str = "", reading_en: str = "") -> dict[str, Any]:
    """新增或更新一个术语的中文/英文读法，至少提供一种读法；保存到磁盘。"""
    return backend.request("POST", "/glossary", data={"term": term, "reading_zh": reading_zh, "reading_en": reading_en})


@tool(annotations=CHANGE)
def delete_pronunciation(term: Name) -> dict[str, Any]:
    """删除指定术语的读音规则；不会清空其他术语。"""
    return backend.request("DELETE", "/glossary/" + quote(term, safe=""))


@tool(annotations=CHANGE)
def set_glossary_cascade(enabled: bool) -> dict[str, Any]:
    """设置共享词汇表是否级联替换；立即生效并保存设置。"""
    return backend.request("POST", "/glossary/cascade", data=form_values({"cascade": enabled}))


@tool(annotations=READ)
def list_history() -> dict[str, Any]:
    """读取本次后台运行的生成历史，最多20条；已交付MCP音频独立保存。"""
    return backend.request("GET", "/history")


@tool(annotations=CHANGE)
def delete_history_item(item_id: str) -> dict[str, Any]:
    """删除一条历史；自动清理开启时会将原输出移入回收站，MCP交付副本保留。"""
    return backend.request("DELETE", "/history/" + quote(item_id, safe=""))


@tool(annotations=CHANGE)
def set_history_auto_clean(enabled: bool) -> dict[str, Any]:
    """设置历史淘汰/删除是否同时清理原音频；影响网页，重启恢复原默认值。"""
    return backend.request("POST", "/history/config", data=form_values({"auto_clean": enabled}))


@tool(annotations=CHANGE)
def configure_model(settings: ModelOptions) -> dict[str, Any]:
    """修改所有客户端共用的模型配置；半精度更改需重载模型，不会自动打断生成。"""
    values = settings.model_dump(exclude_none=True)
    if not values:
        raise ValueError("请至少提供一个模型配置项")
    return backend.request("POST", "/model/config", json=values)


@tool(annotations=WRITE)
def load_model() -> dict[str, Any]:
    """提前加载共用语音模型；通常首次生成会自动加载，忙碌时拒绝此操作。"""
    return backend.request("POST", "/model/load", timeout=180)


@tool(annotations=CHANGE)
def unload_model(force: bool = False) -> dict[str, Any]:
    """卸载共用模型释放显存。force=true会中断其他客户端正在生成的任务，须明确需要。"""
    return backend.request("POST", "/model/unload", params=form_values({"force": force}), timeout=180)


@tool(annotations=CHANGE)
def restart_model() -> dict[str, Any]:
    """用当前配置重新加载共用模型，生成中拒绝；不会重启后台服务或自动停止任务。"""
    return backend.request("POST", "/model/restart", timeout=180)


if __name__ == "__main__":
    if sys.argv[1:] == ["--print-config"]:
        print(json.dumps({"mcpServers": {"indextts2": {
            "command": sys.executable, "args": ["-B", str(backend.root / "indextts-mcp" / "mcp_server.py")],
        }}}, ensure_ascii=False, indent=2))
    else:
        mcp.run(transport="stdio")
