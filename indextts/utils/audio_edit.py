"""In-memory WAV editing for the local MCP."""

from __future__ import annotations

from io import BytesIO
import math


_WSOLA_FRAME = 1024


def _stretch_mono(samples, sample_rate: int, rate: float):
    import numpy as np
    from audiotsm import wsola
    from audiotsm.io.array import ArrayReader, ArrayWriter

    n = samples.shape[0]
    wanted = int(round(n / rate))
    if wanted < 1:
        raise ValueError("变速后的音频过短，无法生成有效结果")
    source = np.ascontiguousarray(samples.reshape(1, -1), dtype="float32")
    source = np.concatenate((source, np.zeros((1, _WSOLA_FRAME), dtype="float32")), axis=1)
    writer = ArrayWriter(1)
    wsola(channels=1, speed=rate, frame_length=_WSOLA_FRAME).run(ArrayReader(source), writer)
    output = writer.data[0]
    if output.shape[0] < wanted:
        output = np.pad(output, (0, wanted - output.shape[0]))
    return output[:wanted]


def stretch_wav(in_path: str, rate: float) -> bytes:
    """Compatibility helper for the existing tempo route."""
    import numpy as np
    import soundfile as sf

    if not math.isfinite(rate) or not 0.5 <= rate <= 2.0:
        raise ValueError("变速倍率必须在 0.5 到 2.0 之间")
    samples, sample_rate = sf.read(in_path, dtype="float32")
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    if not samples.size:
        raise ValueError("音频没有可处理的采样点")
    if not np.isfinite(samples).all():
        raise ValueError("音频包含非有限采样值")
    output = _stretch_mono(np.asarray(samples), sample_rate, rate)
    result = BytesIO()
    sf.write(result, output, sample_rate, subtype="PCM_16", format="WAV")
    return result.getvalue()


def edit_wav(
    data: bytes,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
    rate: float = 1.0,
    gain_db: float = 0.0,
) -> tuple[bytes, dict]:
    """Trim, WSOLA-stretch, and apply bounded gain to WAV bytes."""
    import numpy as np
    import soundfile as sf

    for name, value in (("start_seconds", start_seconds), ("rate", rate), ("gain_db", gain_db)):
        if not math.isfinite(value):
            raise ValueError(f"参数 {name} 必须是有限数值")
    if end_seconds is not None and not math.isfinite(end_seconds):
        raise ValueError("参数 end_seconds 必须是有限数值")
    if start_seconds < 0 or not 0.5 <= rate <= 2.0 or not -24 <= gain_db <= 24:
        raise ValueError("裁剪起点、变速倍率或增益超出允许范围")

    try:
        source = BytesIO(data)
        container = sf.info(source).format
        if container not in {"WAV", "WAVEX", "RF64"}:
            raise ValueError("仅支持 WAV 音频")
        source.seek(0)
        samples, sample_rate = sf.read(source, dtype="float32", always_2d=True)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("无效的 WAV 音频") from exc
    if not samples.size or sample_rate <= 0 or not np.isfinite(samples).all():
        raise ValueError("音频没有有效采样点")

    duration = samples.shape[0] / sample_rate
    end_seconds = duration if end_seconds is None else end_seconds
    if end_seconds <= start_seconds or end_seconds > duration:
        raise ValueError("裁剪范围必须位于音频时长内，且结束时间晚于起始时间")
    start = int(start_seconds * sample_rate)
    end = int(end_seconds * sample_rate)
    samples = samples[start:end]
    if not samples.size:
        raise ValueError("裁剪范围内没有采样点")

    if rate != 1.0:
        samples = _stretch_mono(samples.mean(axis=1), sample_rate, rate)[:, None]

    peak = float(np.max(np.abs(samples)))
    factor = 10 ** (gain_db / 20)
    output = samples * factor
    clipped = bool(np.any(np.abs(output) > 1.0))
    output = np.clip(output, -1.0, 1.0)
    safe_gain_db = 24.0 if peak == 0 else max(-24.0, min(24.0, math.floor(20 * math.log10(1 / peak) * 2) / 2))
    warnings = ["增益导致音频削波"] if clipped else []

    result = BytesIO()
    sf.write(result, output, sample_rate, subtype="PCM_16", format="WAV")
    wav = result.getvalue()
    return wav, {
        "duration_seconds": output.shape[0] / sample_rate,
        "sample_rate": sample_rate,
        "channels": output.shape[1],
        "size_bytes": len(wav),
        "clipped": clipped,
        "safe_gain_db": safe_gain_db,
        "warnings": warnings,
    }
