"""
思考过程收集器模块 (v1.1)

用于收集和管理AI的思考过程、工具调用信息，
支持实时展示给用户。

v1.1 更新 (2025-10-18):
- 🐛 修复工具结果中包含markdown表格的问题
- ✨ 清理工具结果中的表格、加粗等markdown格式，确保<think>标签内容纯文本

作者: Claude Code
日期: 2025-10-18
版本: v1.1
"""

import logging
import time
import re
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


def _clean_markdown_for_thinking(text: str) -> str:
    """清理markdown表格，保留其他格式

    只移除表格行，保留加粗、emoji等其他markdown格式。

    Args:
        text: 原始文本

    Returns:
        str: 清理后的文本（移除表格，保留加粗、emoji等）
    """
    if not text:
        return text

    # 分行处理
    lines = text.split('\n')
    cleaned_lines = []

    for line in lines:
        # 只跳过表格行（包含 | 符号的行）
        if '|' in line:
            stripped = line.strip()
            # 检查是否是表格行（以|开头，或包含|且格式像表格）
            if stripped.startswith('|') or re.match(r'^.*\|.*\|.*$', stripped):
                # 检查是否是分隔行（全是-和|和空格）
                if re.match(r'^[\|\-\s]+$', stripped):
                    continue  # 跳过分隔行
                else:
                    # 数据行，提取所有单元格的文本
                    cells = [cell.strip() for cell in stripped.split('|') if cell.strip()]
                    if cells:
                        # 将单元格内容拼接成一行（保留加粗等格式）
                        line = ' '.join(cells)
                    else:
                        continue

        # 保留有内容的行（不去除加粗、emoji等）
        if line.strip():
            cleaned_lines.append(line)

    # 重新组合
    result = '\n'.join(cleaned_lines)

    # 去除多余的连续空行
    result = re.sub(r'\n{3,}', '\n\n', result)

    return result.strip()


@dataclass
class ThinkingStep:
    """单个思考步骤

    Attributes:
        type: 步骤类型 (start, tool_call, tool_result, generating, end)
        content: 步骤内容
        timestamp: 时间戳
        metadata: 额外元数据
    """
    type: str
    content: str
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_markdown(self) -> str:
        """转换为 Markdown 格式的文本（v1.1: 只清理表格）

        Returns:
            str: Markdown 格式的步骤描述（移除表格，保留加粗、emoji等）
        """
        if self.type == "start":
            return f"🤔 {self.content}"
        elif self.type == "tool_call":
            tool_name = self.metadata.get("tool_name", "工具")
            params = self.metadata.get("params", {})
            # v2.0: 优化参数显示，只显示非None的参数
            params_filtered = {k: v for k, v in params.items() if v is not None}
            if params_filtered:
                params_str = ", ".join([f"{k}={v}" for k, v in params_filtered.items()])
                return f"🔧 **{tool_name}**({params_str})"
            else:
                return f"🔧 **{tool_name}**()"
        elif self.type == "tool_result":
            tool_name = self.metadata.get("tool_name", "工具")
            success = self.metadata.get("success", True)
            elapsed = self.metadata.get("elapsed", 0)
            if success:
                # v1.1: 只清理表格，保留加粗、emoji等其他格式
                cleaned_content = _clean_markdown_for_thinking(self.content)

                # v2.0: 只显示结果预览，不显示完整内容
                result_preview = cleaned_content[:100]  # v1.1: 缩短预览长度
                if len(cleaned_content) > 100:
                    result_preview += "..."

                return f"✅ **{tool_name}** (耗时{elapsed:.2f}s)\n{result_preview}"
            else:
                # 失败时也清理表格
                cleaned_error = _clean_markdown_for_thinking(self.content)
                return f"❌ **{tool_name}** 执行失败 (耗时{elapsed:.2f}s): {cleaned_error}"
        elif self.type == "generating":
            # 累积的思考内容可能很长，只显示最后200字符作为进度摘要
            content = self.content
            if len(content) > 200:
                content = "..." + content[-200:]
            return f"💭 {content}"
        elif self.type == "end":
            return f"✨ {self.content}"
        else:
            return self.content


class ThinkingCollector:
    """思考过程收集器

    收集AI的思考过程、工具调用信息，并支持多种格式输出。

    Example:
        >>> collector = ThinkingCollector()
        >>> collector.add_start("正在理解您的问题...")
        >>> collector.add_tool_call("query_stock", {"goods_id": 123})
        >>> collector.add_tool_result("query_stock", "库存: 100", True, 0.5)
        >>> collector.add_generating("正在生成回复...")
        >>> thinking_text = collector.to_markdown()
        >>> print(thinking_text)
    """

    def __init__(self):
        """初始化思考过程收集器"""
        self.steps: List[ThinkingStep] = []
        self.enabled = True  # 是否启用收集
        self.start_time = time.time()

    def add_start(self, content: str):
        """添加开始思考步骤

        Args:
            content: 思考内容（如"正在理解您的问题..."）
        """
        if not self.enabled:
            return

        step = ThinkingStep(
            type="start",
            content=content
        )
        self.steps.append(step)
        logger.debug(f"[ThinkingCollector] 添加开始步骤: {content}")

    def add_tool_call(self, tool_name: str, params: Dict[str, Any]):
        """添加工具调用步骤

        Args:
            tool_name: 工具名称
            params: 工具参数
        """
        if not self.enabled:
            return

        step = ThinkingStep(
            type="tool_call",
            content=f"调用工具: {tool_name}",
            metadata={
                "tool_name": tool_name,
                "params": params
            }
        )
        self.steps.append(step)
        logger.debug(f"[ThinkingCollector] 添加工具调用: {tool_name}, params={params}")

    def add_tool_result(
        self,
        tool_name: str,
        result: str,
        success: bool,
        elapsed: float
    ):
        """添加工具执行结果步骤

        Args:
            tool_name: 工具名称
            result: 工具返回结果或错误信息
            success: 是否成功
            elapsed: 执行耗时（秒）
        """
        if not self.enabled:
            return

        step = ThinkingStep(
            type="tool_result",
            content=result,
            metadata={
                "tool_name": tool_name,
                "success": success,
                "elapsed": elapsed
            }
        )
        self.steps.append(step)
        logger.debug(
            f"[ThinkingCollector] 添加工具结果: {tool_name}, "
            f"success={success}, elapsed={elapsed:.2f}s"
        )

    def add_generating(self, content: str):
        """添加生成回复步骤

        如果上一个步骤也是generating类型，则追加到同一步骤中，
        避免流式chunk导致每隔几个字就换行。

        Args:
            content: 生成状态描述（如"正在生成回复..."）
        """
        if not self.enabled:
            return

        # 如果上一个步骤也是generating，追加到同一步骤（流式累积）
        if self.steps and self.steps[-1].type == "generating":
            self.steps[-1].content += content
            self.steps[-1].timestamp = time.time()
            logger.debug(f"[ThinkingCollector] 追加生成内容: {content[:30]}")
            return

        step = ThinkingStep(
            type="generating",
            content=content
        )
        self.steps.append(step)
        logger.debug(f"[ThinkingCollector] 添加生成步骤: {content}")

    def add_end(self, content: str = "思考完成"):
        """添加结束步骤

        Args:
            content: 结束消息
        """
        if not self.enabled:
            return

        total_elapsed = time.time() - self.start_time
        step = ThinkingStep(
            type="end",
            content=f"{content}（总耗时{total_elapsed:.2f}s）"
        )
        self.steps.append(step)
        logger.debug(f"[ThinkingCollector] 添加结束步骤: {content}")

    def to_markdown(self) -> str:
        """转换为 Markdown 格式文本

        Returns:
            str: Markdown 格式的思考过程
        """
        if not self.steps:
            return ""

        lines = []
        for step in self.steps:
            lines.append(step.to_markdown())

        return "\n".join(lines)

    def to_think_block(self) -> str:
        """转换为 <think> 标签包裹的文本

        Returns:
            str: <think>...</think> 格式的文本
        """
        markdown_text = self.to_markdown()
        if not markdown_text:
            return ""

        return f"<think>\n{markdown_text}\n</think>"

    def get_step_count(self) -> int:
        """获取步骤数量

        Returns:
            int: 步骤总数
        """
        return len(self.steps)

    def clear(self):
        """清空所有步骤"""
        self.steps.clear()
        self.start_time = time.time()
        logger.debug("[ThinkingCollector] 已清空所有步骤")

    def disable(self):
        """禁用收集"""
        self.enabled = False
        logger.debug("[ThinkingCollector] 已禁用收集")

    def enable(self):
        """启用收集"""
        self.enabled = True
        logger.debug("[ThinkingCollector] 已启用收集")

    def extract_tool_calls(self) -> List[Dict[str, Any]]:
        """提取所有工具调用记录（用于保存到会话历史）

        Returns:
            List[Dict]: 工具调用记录列表
                格式: [
                    {
                        "tool_name": "query_goods",
                        "params": {"keyword": "蓝澈"},
                        "result": "找到3个商品",
                        "success": True,
                        "elapsed": 0.5
                    },
                    ...
                ]

        Example:
            >>> collector = ThinkingCollector()
            >>> collector.add_tool_call("query_goods", {"keyword": "蓝澈"})
            >>> collector.add_tool_result("query_goods", "找到3个商品", True, 0.5)
            >>> tool_calls = collector.extract_tool_calls()
            >>> print(tool_calls)
            [{"tool_name": "query_goods", "params": {...}, ...}]
        """
        tool_calls = []
        pending_call = None

        for step in self.steps:
            if step.type == "tool_call":
                # 记录工具调用
                pending_call = {
                    "tool_name": step.metadata.get("tool_name"),
                    "params": step.metadata.get("params", {}),
                    "result": None,
                    "success": None,
                    "elapsed": None
                }
            elif step.type == "tool_result" and pending_call:
                # 补充工具结果
                if pending_call["tool_name"] == step.metadata.get("tool_name"):
                    pending_call["result"] = step.content
                    pending_call["success"] = step.metadata.get("success")
                    pending_call["elapsed"] = step.metadata.get("elapsed")
                    tool_calls.append(pending_call)
                    pending_call = None

        # 如果有未完成的调用（没有结果），也加入
        if pending_call:
            tool_calls.append(pending_call)

        return tool_calls
