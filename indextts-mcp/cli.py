"""Command-line access to the existing tools, without a stdio MCP connection."""
import argparse
import asyncio
import json
import math
from pathlib import Path
import sys
import time

from mcp.server.mcpserver.exceptions import ToolError
from mcp_server import mcp


def output(value, stream=None):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), file=stream or sys.stdout, flush=True)


def build_parser(tools):
    parser = argparse.ArgumentParser(description="本机配音命令行；与 MCP 和网页共用后台、模型及数据。")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("tools", help="输出全部工具及参数的 JSON 说明")
    for tool in tools:
        name = tool.name.replace("_", "-")
        command = commands.add_parser(name, aliases=[tool.name] if tool.name != name else [],
                                      help=tool.description.splitlines()[0], description=tool.description)
        properties = tool.input_schema.get("properties", {})
        command.set_defaults(tool_name=tool.name, properties=properties)
        command.add_argument("--args-file", type=Path, help="UTF-8 JSON 参数文件；命令行选项覆盖同名字段")
        for field, schema in properties.items():
            schema = next((item for item in schema.get("anyOf", []) if item.get("type") != "null"), schema)
            options = {"dest": field, "default": argparse.SUPPRESS,
                       "help": schema.get("description", "参数见 tools 输出；复合参数使用 JSON")}
            if schema.get("type") == "boolean":
                options["action"] = argparse.BooleanOptionalAction
            else:
                options["type"] = str if schema.get("type") == "string" else json.loads
                if "enum" in schema:
                    options["choices"] = schema["enum"]
            command.add_argument("--" + field.replace("_", "-"), **options)
        if tool.name == "synthesize":
            command.add_argument("--text-file", type=Path, help="从 UTF-8 文本文件读取配音正文，不能与 --text 同用")
        if tool.name in ("synthesize", "get_task"):
            command.add_argument("--wait", action="store_true", help="等待任务终态，进度写入 stderr")
            command.add_argument("--timeout", type=float, default=600, help="等待上限秒数，默认 600；超时不停止后台任务")
    return parser


async def invoke(name, arguments):
    result = await mcp.call_tool(name, arguments)
    if result.is_error:
        raise ToolError("\n".join(item.text for item in result.content if item.type == "text"))
    return result.structured_content


async def wait_task(result, timeout):
    job_id = result.get("job_id") or (result.get("job") or {}).get("job_id")
    if not job_id:
        raise ValueError("未找到任务，不能等待；请核对 job_id 或 submission_id")
    output({"waiting_for": job_id, "message": "停止等待或超时不会停止后台配音；可用 get-task 继续查询。"}, sys.stderr)
    deadline = time.monotonic() + timeout
    last_stage = None
    while True:
        job = result.get("job")
        if job:
            stage = (job["state"], job.get("stage_desc"))
            if stage != last_stage:
                output({"job_id": job_id, "state": stage[0], "stage": stage[1]}, sys.stderr)
                last_stage = stage
            if job["state"] == "done":
                output(result)
                return 0
            if job["state"] in {"error", "cancelled", "stopped", "interrupted"}:
                output({**result, "ok": False}, sys.stderr)
                return 1
        if time.monotonic() >= deadline:
            output({"ok": False, "job_id": job_id, "error": "等待超时，后台任务仍可能运行；请查询，勿重复提交。"}, sys.stderr)
            return 3
        result = await invoke("get_task", {"job_id": job_id})
        if (result.get("job") or {}).get("state") not in {"done", "error", "cancelled", "stopped", "interrupted"}:
            await asyncio.sleep(1)


async def main(argv=None):
    tools = await mcp.list_tools()
    args = build_parser(tools).parse_args(argv)
    if args.command == "tools":
        output([tool.model_dump(mode="json", exclude_none=True, by_alias=True) for tool in tools])
        return 0
    try:
        arguments = json.loads(args.args_file.read_text(encoding="utf-8-sig")) if args.args_file else {}
        if not isinstance(arguments, dict):
            raise ValueError("参数文件的根节点必须是 JSON 对象")
        unknown = arguments.keys() - args.properties.keys()
        if unknown:
            raise ValueError("未知参数：" + ", ".join(sorted(unknown)))
        arguments.update({key: value for key, value in vars(args).items() if key in args.properties})
        if getattr(args, "text_file", None):
            if hasattr(args, "text"):
                raise ValueError("--text 与 --text-file 只能选择一个")
            arguments["text"] = args.text_file.read_text(encoding="utf-8-sig")
        if hasattr(args, "timeout") and (not math.isfinite(args.timeout) or args.timeout <= 0):
            raise ValueError("--timeout 必须是大于 0 的有限秒数")
        result = await invoke(args.tool_name, arguments)
        if getattr(args, "wait", False):
            return await wait_task(result, args.timeout)
        output(result)
        return 0
    except (ToolError, ValueError, OSError) as error:
        output({"ok": False, "error": str(error)}, sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        output({"ok": False, "error": "已退出命令；已提交的配音不会自动停止，请查询任务或使用 stop-task。"}, sys.stderr)
        raise SystemExit(130)
