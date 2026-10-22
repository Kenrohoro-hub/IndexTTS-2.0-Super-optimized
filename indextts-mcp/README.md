# IndexTTS2 本机 MCP 与 CLI

通过 MCP 客户端在本机调用 IndexTTS2 配音、编辑音频和管理常用配置。服务使用 stdio 与 MCP 客户端通信；无需打开网页。MCP 服务连接项目已有的本机后台，不会自行导入语音模型。

也可使用 `indextts.cmd` 命令行入口。CLI 与 MCP 共用全部 26 个工具、参数校验和后台模型，不需要配置 MCP 客户端。CLI 内部直接调用已安装 SDK 的同一套工具函数，没有另起 MCP 协议连接。

运行前提：Windows，以及按[项目快速开始](../README.md#快速开始)准备好的根目录 `.venv/Scripts/python.exe` 和 `checkpoints/` 模型文件。MCP 使用本目录独立的 `.venv`，与配音环境分开；`requirements.lock` 固定 MCP 依赖，首次准备需要联网安装。不要单独移动本目录。

## 启动与连接

首次使用时运行本目录的 `run-app.cmd`。它负责准备 MCP 运行环境并打印客户端配置。将配置复制到 MCP 客户端后，由客户端直接启动 Python：

```text
<PROJECT_ROOT>/indextts-mcp/.venv/Scripts/python.exe -B <PROJECT_ROOT>/indextts-mcp/mcp_server.py
```

客户端配置中的 `command` 和 `args` 对应如下；把 `<PROJECT_ROOT>` 换成 IndexTTS2 项目根目录：

```json
{
  "mcpServers": {
    "indextts2": {
      "command": "<PROJECT_ROOT>/indextts-mcp/.venv/Scripts/python.exe",
      "args": [
        "-B",
        "<PROJECT_ROOT>/indextts-mcp/mcp_server.py"
      ]
    }
  }
}
```

重启 MCP 客户端会重新连接服务。通常先调用 `get_status` 查看后台；需要时调用 `ensure_service`，或直接提交配音，业务工具会按需启动本机后台。

脚本也会生成当前机器可直接使用的 `client-config.json`。支持导入 `mcpServers` JSON 的客户端可复制该条目；使用表单的客户端选择 **stdio**，分别填入 `command` 和 `args`。此脚本仅生成连接配置，不会自行修改 Agent 的配置。项目位置改变后，重新运行脚本生成路径配置；若旧虚拟环境因移动失效，移走 `.venv` 后重新准备。

后台仅监听本机回环地址，默认端口与原网页一致。需要沿用非默认端口时，在客户端环境变量中设置 `INDEXTTS_PORT` 为实际端口；不同客户端使用相同端口。服务会核对项目身份和数据目录，端口被其他程序占用时返回错误，不替换现有进程。正常连接无需设置环境变量。后台尚未就绪时最多等待约 55 秒，失败详情见 `logs/mcp-backend.log`。

## 最短配音流程

1. 调用 `list_presets` 查看音色预设，或调用 `list_examples` 选择内置音色文件。
2. 调用 `synthesize`，在 `preset_name`、`reference_audio_path`、`example_name` 中恰好选择一个音色来源。
3. 工具立即返回 `job_id` 后，使用 `get_task(job_id=...)` 查询进度，直到任务结束。成功时从 `result.audio_path` 取得已保存的本地 WAV。
4. 需要裁剪、变速或调音量时调用 `edit_audio`；需要复制到指定位置时调用 `export_audio`。

任务状态可能为 `queued`、`running`、`done`、`error`、`stopped`、`cancelled` 或 `interrupted`。`get_task` 只接受 `job_id` 或 `submission_id` 其中一个。若提交请求超时或连接中断，提交结果可能不确定；使用错误信息中的 `submission_id` 查询，不要直接再次提交，以免重复生成。后台进程重启后，未完成任务会显示为 `interrupted`，不会自动重新生成。

## CLI 使用方式

首次环境准备仍使用 `run-app.cmd`。在本目录打开 PowerShell 7 后，可直接运行：

```powershell
.\indextts.cmd --help
.\indextts.cmd get-status
.\indextts.cmd list-presets
.\indextts.cmd list-examples
.\indextts.cmd synthesize --text "你好，这是命令行配音。" --example-name voice_01.wav --wait
```

全部 MCP 工具都能调用：子命令把工具名的下划线换成连字符，例如 `get_task` 对应 `get-task`，参数 `preset_name` 对应 `--preset-name`。子命令也接受原工具名。用 `tools` 查看所有工具的完整 JSON 参数说明，用 `synthesize --help` 查看该命令的选项。

默认提交配音后立即返回任务编号；加 `--wait` 才等待完成，完成结果在 `job.result.audio_path`。已有任务可以用 `get-task --job-id <job_id> --wait` 继续等待；停止用 `stop-task --job-id <job_id>`，随后查询确认终态。`--timeout` 设置等待上限秒数，默认 600；超时或 Ctrl+C 只退出命令，不自动取消后台任务。

长文、换行或包含命令行特殊字符的正文，建议保存为 UTF-8 文件：

```powershell
.\indextts.cmd synthesize --text-file ".\正文.txt" --example-name voice_01.wav --wait
```

复杂参数可保存为 UTF-8 JSON 文件，字段名称与 MCP 一致：

```json
{
  "text": "你好，这是一段平静的配音。",
  "example_name": "voice_01.wav",
  "emotion_vector": {"calm": 0.3},
  "advanced": {"top_k": 30}
}
```

```powershell
.\indextts.cmd synthesize --args-file ".\参数.json" --wait
```

命令行明确提供的选项覆盖文件中的同名字段；`--text-file` 可覆盖参数文件的 `text`，但不能同时使用 `--text`。布尔值使用 `--overwrite` / `--no-overwrite` 这类开关；未提供的参数继续沿用原默认值或预设。音频输入和导出目标仍使用绝对路径。JSON 文件避免了 Windows 终端对引号、百分号等字符的再次解释。

成功结果写到 stdout，进度和错误写到 stderr。退出码 `0` 表示调用成功，`1` 表示工具失败或等待到失败终态，`2` 表示命令行格式错误，`3` 表示等待超时，`130` 表示 Ctrl+C。未加 `--wait` 的 `synthesize` 退出码 `0` 只表示提交成功。直接 `get-task` 查询到失败任务仍是查询成功，应检查返回的 `job.state`；加 `--wait` 才以任务结果决定退出码。

从其他目录使用启动脚本的绝对路径即可，正文和参数文件相对于当前终端目录解析。脚本不会修改系统 PATH，也不会重复加载一份模型。Agent 或程序调用时，也可直接执行 `<PROJECT_ROOT>/indextts-mcp/.venv/Scripts/python.exe -B <PROJECT_ROOT>/indextts-mcp/cli.py` 并逐项传入参数。

## CLI 与 MCP 如何选择

| 比较项 | MCP | CLI |
| --- | --- | --- |
| 日常 Agent 调用 | 客户端可发现工具说明和参数，适合 Agent 自行选工具。 | Agent 需具备终端执行能力，通过帮助或参数文件使用。 |
| 固定批量流程 | 需要由 MCP 客户端组织多次调用。 | 便于写循环、串联其他程序和检查退出码。 |
| 接入和排错 | 先配置客户端连接，检查工具调用记录。 | 直接运行命令，方便保存和复现参数。 |
| 参数维护 | 使用同一套工具定义。 | 自动复用同一套定义，能力与校验一致。 |
| 配音效果与资源 | 与 CLI 共用现有后台和 GPU 模型。 | 同样的参数使用同样的生成逻辑；入口变化不提高音质。 |

本项目推荐 MCP 作为不同 Agent 的日常入口，CLI 作为批处理和故障排查入口。无需选定其中一种，两者可以混合调用同一任务。上下文用量取决于 Agent 是否加载全部工具说明以及输出长度，本项目没有做 Token 消耗对比测试。

## 配音与音频处理

`synthesize` 支持 1–10,000 字文本；超过 2,000 字会提示。参考音频使用本地绝对路径，文件不超过 20 MB，最多使用前 15 秒。情感输入在一次配音中最多选择一种：

| `emotion_mode` | 含义 | 对应输入 |
| --- | --- | --- |
| `speaker` | 使用参考音色自带情感 | 无需额外情感输入 |
| `audio` | 参考一段情感音频 | `emotion_audio_path`，或预设中已有情感音频 |
| `vector` | 用八维数值控制情感 | `emotion_vector`：`happy`、`angry`、`sad`、`afraid`、`disgusted`、`melancholic`、`surprised`、`calm`；每项 0–1 |
| `text` | 用文字描述情绪 | `emotion_text`，最多 500 字，显式提供时不得为空白 |

只提供一种情感输入时会自动选择对应模式。`emotion_weight` 为 0–1。未指定的生成参数沿用预设或后台默认值。可选 `advanced` 项为：`do_sample`、`top_p`（0–1）、`top_k`（0–100，0 表示不限制）、`temperature`（0.1–2）、`length_penalty`（-2–2）、`num_beams`（1–10）、`repetition_penalty`（0.1–20）、`max_mel_tokens`（50–1815）。`pause_ms` 范围为 10–500，默认 200；`file_naming` 可选 `title_time`、`title` 或 `default`。

`edit_audio` 只接受不超过 20 MB 的本地 WAV，按源音频秒数依次裁剪、保音高变速、调音量；`start_seconds` 不小于 0，`end_seconds` 留空表示音频结尾，且结束点必须晚于起点并处于音频时长内；`rate` 为 0.5–2，`gain_db` 为 -24–24。变速会将多声道混为单声道；不变速时保留声道。编辑另存结果，不改源文件。返回的 `clipped` 表示发生削波，`safe_gain_db` 是建议的增益上限。

`export_audio(audio_path, destination_path, overwrite=false)` 将 WAV 复制到明确指定的本地绝对路径，默认拒绝覆盖；只有明确传入 `overwrite=true` 才会替换已有目标。源文件保持不变。

## 可用工具

### 状态、音色与配音

| 工具 | 参数 | 用途 |
| --- | --- | --- |
| `get_status` | 无 | 查看后台、模型、CPU/显存和生成状态；不启动后台。 |
| `ensure_service` | 无 | 连接或按需启动项目后台；不预加载模型，也不打开网页。 |
| `list_presets` | 无 | 列出已保存的音色预设。 |
| `get_preset` | `name` | 查看预设情绪、生成参数和参考音频信息。 |
| `list_examples` | 无 | 列出内置示例及可用于配音的音色文件名。 |
| `preview_text` | `text`, `segment_tokens=120`（20–600） | 查看术语替换、文本规范化和分段；不加载完整语音模型。 |
| `synthesize` | `text`, 一个音色来源参数，及可选情感、生成参数、停顿和命名选项 | 提交配音并返回 `job_id`。 |
| `get_task` | `job_id` 或 `submission_id` | 查询任务进度、终态和结果路径；二选一。 |
| `stop_task` | `job_id` | 取消排队任务或请求停止正在生成的任务；之后继续用 `get_task` 确认终态。 |

### 编辑与预设

| 工具 | 参数 | 用途 |
| --- | --- | --- |
| `edit_audio` | `audio_path`, `start_seconds=0`, `end_seconds=null`, `rate=1`, `gain_db=0` | 编辑本地 WAV 并保存独立结果。 |
| `export_audio` | `audio_path`, `destination_path`, `overwrite=false` | 将 WAV 复制到指定路径。 |
| `save_preset` | `name`，可选参考音频、情感、生成参数、`overwrite=false` | 保存音色和参数；同名默认拒绝覆盖。预设不保存段间停顿。 |
| `rename_preset` | `name`, `new_name` | 重命名预设；目标名称已存在时拒绝。 |
| `duplicate_preset` | `name`, `new_name` | 复制预设、参考音频和参数；拒绝覆盖已有预设。 |
| `delete_preset` | `name` | 将预设及其参考音频移入 Windows 回收站。 |

### 术语、历史与模型

| 工具 | 参数 | 用途 |
| --- | --- | --- |
| `get_glossary` | 无 | 查看术语读音表和级联替换设置。 |
| `set_pronunciation` | `term`, `reading_zh=""`, `reading_en=""` | 新增或更新术语的中文或英文读法，至少提供一种。 |
| `delete_pronunciation` | `term` | 删除指定术语的读音规则。 |
| `set_glossary_cascade` | `enabled` | 设置术语级联替换。 |
| `list_history` | 无 | 查看当前后台运行期间最多 20 条生成历史。 |
| `delete_history_item` | `item_id` | 删除指定历史记录。 |
| `set_history_auto_clean` | `enabled` | 设置删除或淘汰历史时是否同时清理后台原音频；后台重启后恢复默认值。 |
| `configure_model` | `settings` | 修改共享模型配置：`fp16`、`s2mel_fp16`、`w2v_fp16`、`qwen_fp16`、`cudnn_benchmark`、`diffusion_steps`（1–50）、`inference_cfg_rate`（0–1）。半精度设置变化需要重载模型。 |
| `load_model` | 无 | 提前加载模型；通常首次配音会自动加载，后台忙碌时拒绝此操作。 |
| `unload_model` | `force=false` | 卸载模型释放显存；`force=true` 会中断其他客户端正在生成的任务。 |
| `restart_model` | 无 | 按当前配置重新加载模型；生成期间拒绝，不会重启后台服务。 |

## 共享状态与文件清理

所有 MCP 客户端和网页共用同一个本机后台、一个语音模型和一条生成队列，任务会依次使用模型。模型配置、音色预设、术语表和历史自动清理设置也与网页共用；在 MCP 中的修改会影响网页，反之亦然。任务排队时可以继续查询或取消；停止正在生成的任务后，要通过 `get_task` 确认状态变为终态。

配音结果会另存为 MCP 交付 WAV。删除或自动清理网页/后台历史时，该交付副本仍保留。`delete_history_item` 在自动清理开启时会把后台原输出移入回收站，但不会删除交付副本。MCP 没有删除交付文件的工具；不再需要时，请根据返回的 `audio_path` 用文件管理器手动删除。导出前若目标已存在，默认保留目标并返回错误。

客户端退出后，共享后台继续运行；可通过 `unload_model` 释放模型占用的显存。后台重启不会恢复未完成的配音，已经交付的文件仍可凭原 `job_id` 查询。`configure_model` 和 `get_status` 的 `reload_required` 为真时，说明已保存的半精度配置与运行中模型不同，需要在空闲时调用 `restart_model` 才生效。

若客户端允许设置工具超时，模型加载和重载建议留出至少 180 秒。普通配音会先返回任务编号，长文本通过轮询等待完成；停止模型加载期间的任务，也需等当前加载步骤结束后才能确认终态。

默认数据位置如下，均相对于 `<PROJECT_ROOT>`：

| 位置 | 内容 |
| --- | --- |
| `exports/mcp/` | 交付的 WAV 和任务 JSON 回执；不自动清理，可手动移入回收站；同编号 WAV/JSON 建议一并清理。 |
| `outputs/presets/` | 与网页共享的预设、参考音频。 |
| `checkpoints/glossary.yaml`、`checkpoints/glossary_settings.yaml` | 共享术语和级联设置。 |
| `logs/` | 后台启动和运行日志。 |
| `indextts-mcp/.state/` | 本机启动锁与诊断记录，不是跨设备共享状态。 |

## 实现位置

调用链：Agent → stdio MCP → 本机 HTTP 后台 → 共用推理队列 → 原 IndexTTS2 模型。MCP 和网页的生成入口最终调用同一套模型逻辑。

| 文件 | 职责 |
| --- | --- |
| `indextts-mcp/mcp_server.py` | 26 个 MCP 工具、参数约束、可读错误和结构化结果。 |
| `indextts-mcp/cli.py`、`indextts-mcp/indextts.cmd` | 从同一工具定义生成 CLI 子命令；参数文件、等待终态和退出码。 |
| `indextts-mcp/backend.py` | HTTP 调用、后台身份核对、Windows 隐藏启动、文件导出；通过 Windows WMI 启动，避免客户端退出时连带杀死共享后台。 |
| `server.py` | 原网页和新增任务式生成接口、队列、进度、取消/停止、预设与模型管理。 |
| `indextts/utils/mcp_results.py` | 交付音频和任务回执持久化。 |
| `indextts/utils/audio_edit.py` | 裁剪、变速、增益的共用处理。 |
| `indextts/utils/presets.py`、`recycle.py` | 预设完整写入后替换、回收站删除。 |
| `indextts/utils/front.py` | 使用模型字节加载分词器，支持中文绝对路径。 |

## 验证状态

本源码副本的启动入口使用项目根目录的 `.venv`。全新环境下的 MCP 连接、后台启动与显卡配音尚待验证；本机客户端配置、测试音频、日志和备份不随源码分发。客户端需能读取返回的本机文件路径。
