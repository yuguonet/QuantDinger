# -*- coding: utf-8 -*-
"""WorkspaceTool — 工作区（脚本执行/文件操作/代码审查/后台任务）。"""
from app.agent.tools.base import Tool


class WorkspaceTool(Tool):

    @property
    def name(self) -> str: return "workspace"
    @property
    def description(self) -> str: return "工作区：Shell执行、脚本保存/加载/执行、文件读写编辑、代码审查、后台任务"
    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": [
                    "shell_exec", "save_script", "load_script", "list_scripts",
                    "write_file", "read_file", "edit_file", "code_review",
                    "exec_script", "run_background", "poll_task"
                ], "description": "操作类型"},
                "command": {"type": "string", "description": "Shell命令（shell_exec用）"},
                "script_name": {"type": "string", "description": "脚本名称"},
                "content": {"type": "string", "description": "文件/脚本内容"},
                "file_path": {"type": "string", "description": "文件路径"},
                "old_text": {"type": "string", "description": "编辑旧文本（edit_file用）"},
                "new_text": {"type": "string", "description": "编辑新文本（edit_file用）"},
                "task_id": {"type": "string", "description": "任务ID（poll_task用）"},
                "timeout": {"type": "integer", "description": "超时秒数", "default": 30},
            },
            "required": ["action"],
        }

    async def execute(self, action: str, **kwargs) -> str:
        import json
        from app.agent.tools.code_workspace_tools import (
            shell_exec, workspace_save_script, workspace_load_script, workspace_list,
            workspace_write_file, workspace_read_file, workspace_edit_file,
            workspace_code_review, workspace_exec_script, run_background, poll_task,
        )

        try:
            if action == "shell_exec":
                return json.dumps(shell_exec(kwargs["command"], timeout=kwargs.get("timeout", 30)), ensure_ascii=False)
            elif action == "save_script":
                return json.dumps(workspace_save_script(kwargs["script_name"], kwargs.get("content", "")), ensure_ascii=False)
            elif action == "load_script":
                return json.dumps(workspace_load_script(kwargs["script_name"]), ensure_ascii=False)
            elif action == "list_scripts":
                return json.dumps(workspace_list(), ensure_ascii=False)
            elif action == "write_file":
                return json.dumps(workspace_write_file(kwargs["file_path"], kwargs.get("content", "")), ensure_ascii=False)
            elif action == "read_file":
                return json.dumps(workspace_read_file(kwargs["file_path"]), ensure_ascii=False)
            elif action == "edit_file":
                return json.dumps(workspace_edit_file(kwargs["file_path"], kwargs["old_text"], kwargs["new_text"]), ensure_ascii=False)
            elif action == "code_review":
                return json.dumps(workspace_code_review(kwargs.get("content", "")), ensure_ascii=False)
            elif action == "exec_script":
                return json.dumps(workspace_exec_script(kwargs["script_name"]), ensure_ascii=False)
            elif action == "run_background":
                return json.dumps(run_background(kwargs["command"]), ensure_ascii=False)
            elif action == "poll_task":
                return json.dumps(poll_task(kwargs["task_id"]), ensure_ascii=False)
            else:
                return json.dumps({"error": f"未知 action: {action}"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
