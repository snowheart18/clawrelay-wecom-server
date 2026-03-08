"""
流式思考过程管理器 (v1.0)

管理实时流式思考过程的状态，支持企业微信流式消息刷新机制。

核心特性：
- 实时更新思考步骤
- 支持企业微信自动刷新回调
- 线程安全的状态管理
- 自动累积思考内容

作者: Claude Code
日期: 2025-01-17
版本: v1.0
"""

import logging
import threading
from typing import Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime

from .thinking_collector import ThinkingCollector

logger = logging.getLogger(__name__)


@dataclass
class StreamingThinkingState:
    """流式思考过程状态

    Attributes:
        collector: ThinkingCollector实例
        is_complete: 是否完成
        final_answer: 最终答案（完成后设置）
        lock: 线程锁
        created_at: 创建时间
        updated_at: 最后更新时间
    """
    collector: ThinkingCollector
    is_complete: bool = False
    final_answer: Optional[str] = None
    pending_text: str = ""  # 实时累积的回复文本（SSE 流式生成中）
    pending_card: Optional[dict] = None  # AskUserQuestion 时附带的 vote 卡片
    session_url: Optional[str] = None  # Relay 实时聊天页面 URL
    lock: threading.Lock = field(default_factory=threading.Lock)
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    updated_at: float = field(default_factory=lambda: datetime.now().timestamp())

    def update_timestamp(self):
        """更新最后修改时间"""
        self.updated_at = datetime.now().timestamp()


class StreamingThinkingManager:
    """流式思考过程管理器

    管理多个流式会话的思考过程状态，支持实时更新和查询。

    工作流程：
    1. 用户发送消息 → create_stream_thinking() 创建状态
    2. 首次回复 finish=false，包含初始思考内容
    3. 工具调用时 → add_thinking_step() 添加步骤
    4. 企业微信刷新 → get_current_thinking() 获取最新内容
    5. LLM完成 → mark_complete() 标记完成
    6. 最终刷新 → 返回 finish=true

    Example:
        >>> manager = StreamingThinkingManager()
        >>> # 创建流式思考
        >>> collector = manager.create_stream_thinking("stream_001")
        >>> collector.add_start("正在理解您的问题...")
        >>>
        >>> # 工具调用时添加步骤
        >>> manager.add_tool_call("stream_001", "query_goods", {"keyword": "蓝澈"})
        >>>
        >>> # 企业微信刷新时获取内容
        >>> content, is_complete = manager.get_current_thinking("stream_001")
        >>> print(content)  # <think>🤔 正在理解您的问题...\n🔧 调用工具...</think>
        >>>
        >>> # 完成
        >>> manager.mark_complete("stream_001", "蓝澈防晒霜库存为100件")
    """

    def __init__(self):
        """初始化流式思考管理器"""
        self._states: Dict[str, StreamingThinkingState] = {}
        self._lock = threading.Lock()
        logger.info("[StreamingThinkingManager] 初始化完成")

    def create_stream_thinking(self, stream_id: str) -> ThinkingCollector:
        """创建流式思考过程

        Args:
            stream_id: 流式消息ID

        Returns:
            ThinkingCollector: 思考收集器实例
        """
        with self._lock:
            if stream_id in self._states:
                logger.warning(
                    f"[StreamingThinkingManager] stream_id={stream_id} 已存在，返回现有collector"
                )
                return self._states[stream_id].collector

            collector = ThinkingCollector()
            state = StreamingThinkingState(collector=collector)
            self._states[stream_id] = state

            logger.info(f"[StreamingThinkingManager] 创建流式思考: stream_id={stream_id}")
            return collector

    # 企业微信流式消息 stream.content 最大字节数
    MAX_CONTENT_BYTES = 20480

    def get_current_thinking(
        self,
        stream_id: str,
        include_final_answer: bool = True
    ) -> tuple[str, bool]:
        """获取当前思考内容（用于刷新回调）

        企业微信 stream.content 限制 20,480 字节 (UTF-8)。
        超出时截断 thinking 部分（保留最新内容），确保 final_answer 完整。

        Args:
            stream_id: 流式消息ID
            include_final_answer: 是否包含最终答案（默认True）

        Returns:
            tuple[str, bool]: (思考内容, 是否完成)
        """
        with self._lock:
            if stream_id not in self._states:
                logger.warning(
                    f"[StreamingThinkingManager] stream_id={stream_id} 不存在"
                )
                return "", True  # 不存在则返回空内容，标记完成

            state = self._states[stream_id]

        # 使用状态锁（避免在状态更新时读取）
        with state.lock:
            # 获取思考过程
            thinking_text = state.collector.to_think_block()

            # 如果完成且有最终答案，拼接答案
            if state.is_complete and state.final_answer and include_final_answer:
                full_content = f"{thinking_text}\n\n{state.final_answer}"
            elif state.pending_text:
                # 未完成但有实时累积文本，拼接到思考块之后
                full_content = f"{thinking_text}\n\n{state.pending_text}"
            else:
                full_content = thinking_text

            # 检查字节数，超出限制时截断 thinking 部分
            full_content = self._truncate_to_byte_limit(
                full_content, thinking_text, state
            )

            logger.debug(
                f"[StreamingThinkingManager] 获取思考内容: stream_id={stream_id}, "
                f"is_complete={state.is_complete}, content_len={len(full_content)}, "
                f"content_bytes={len(full_content.encode('utf-8'))}"
            )

            return full_content, state.is_complete

    # 内容被截断时追加的查看链接模板
    _SESSION_URL_SUFFIX_TEMPLATE = "\n\n📎 内容较长已截断，查看完整内容：[链接>>]({url})"

    def _build_session_url_suffix(self, state: StreamingThinkingState) -> str:
        """构建 session URL 后缀（仅在有 URL 时返回）"""
        if state.session_url:
            return self._SESSION_URL_SUFFIX_TEMPLATE.format(url=state.session_url)
        return ""

    def _truncate_to_byte_limit(
        self,
        full_content: str,
        thinking_text: str,
        state: StreamingThinkingState,
    ) -> str:
        """确保内容不超过企业微信 20,480 字节限制

        策略：优先保留 final_answer 完整，截断 thinking 部分（保留尾部最新内容）。
        内容被截断时，追加 Relay 实时聊天页面链接。
        """
        content_bytes = len(full_content.encode('utf-8'))
        if content_bytes <= self.MAX_CONTENT_BYTES:
            return full_content

        logger.warning(
            f"[StreamingThinkingManager] 内容超出字节限制: "
            f"{content_bytes} > {self.MAX_CONTENT_BYTES}，执行截断"
        )

        # 计算 session URL 后缀占用的字节数
        url_suffix = self._build_session_url_suffix(state)
        url_suffix_bytes = len(url_suffix.encode('utf-8'))
        effective_limit = self.MAX_CONTENT_BYTES - url_suffix_bytes

        # 确定文本部分（final_answer 或 pending_text）
        text_part_raw = ""
        if state.is_complete and state.final_answer:
            text_part_raw = state.final_answer
        elif state.pending_text:
            text_part_raw = state.pending_text

        if text_part_raw:
            answer_part = f"\n\n{text_part_raw}"
            answer_bytes = len(answer_part.encode('utf-8'))

            # 文本本身就超限，截断文本并追加链接
            if answer_bytes >= effective_limit:
                truncated = self._truncate_str_to_bytes(
                    text_part_raw, effective_limit
                )
                return f"{truncated}{url_suffix}"

            # 为 thinking 预留的字节数
            thinking_budget = effective_limit - answer_bytes
            truncated_thinking = self._truncate_thinking_block(
                thinking_text, thinking_budget
            )
            return f"{truncated_thinking}{answer_part}{url_suffix}"

        # 无文本部分，直接截断 thinking
        truncated = self._truncate_thinking_block(
            thinking_text, effective_limit
        )
        return f"{truncated}{url_suffix}"

    @staticmethod
    def _truncate_thinking_block(thinking_text: str, max_bytes: int) -> str:
        """截断 <think> 块，保留标签结构和尾部最新内容"""
        # <think>\n 和 \n</think> 的固定开销
        wrapper_prefix = "<think>\n... (思考内容过长，仅显示最新部分) ...\n"
        wrapper_suffix = "\n</think>"
        wrapper_bytes = len(wrapper_prefix.encode('utf-8')) + len(wrapper_suffix.encode('utf-8'))

        if wrapper_bytes >= max_bytes:
            return "<think>\n...\n</think>"

        # 提取 <think> 标签内的原始内容
        inner = thinking_text
        if inner.startswith("<think>\n"):
            inner = inner[len("<think>\n"):]
        if inner.endswith("\n</think>"):
            inner = inner[:-len("\n</think>")]

        # 从尾部截取 inner，保留最新的思考步骤
        budget = max_bytes - wrapper_bytes
        truncated_inner = StreamingThinkingManager._truncate_str_to_bytes_tail(
            inner, budget
        )

        return f"{wrapper_prefix}{truncated_inner}{wrapper_suffix}"

    @staticmethod
    def _truncate_str_to_bytes(text: str, max_bytes: int) -> str:
        """从头部开始截取字符串，确保 UTF-8 字节数不超限"""
        encoded = text.encode('utf-8')
        if len(encoded) <= max_bytes:
            return text
        # 按字节截断，确保不切断多字节字符
        truncated = encoded[:max_bytes].decode('utf-8', errors='ignore')
        return truncated

    @staticmethod
    def _truncate_str_to_bytes_tail(text: str, max_bytes: int) -> str:
        """从尾部保留字符串，确保 UTF-8 字节数不超限"""
        encoded = text.encode('utf-8')
        if len(encoded) <= max_bytes:
            return text
        # 从尾部截取
        truncated = encoded[-max_bytes:].decode('utf-8', errors='ignore')
        return truncated

    def set_session_url(self, stream_id: str, session_url: str):
        """设置 Relay 实时聊天页面 URL

        Args:
            stream_id: 流式消息ID
            session_url: Relay 会话页面 URL
        """
        with self._lock:
            if stream_id not in self._states:
                return
            state = self._states[stream_id]

        with state.lock:
            state.session_url = session_url
            logger.debug(
                f"[StreamingThinkingManager] 设置 session_url: stream_id={stream_id}"
            )

    def get_session_url(self, stream_id: str) -> Optional[str]:
        """获取 Relay 实时聊天页面 URL

        Args:
            stream_id: 流式消息ID

        Returns:
            Optional[str]: session URL，无则返回 None
        """
        with self._lock:
            if stream_id not in self._states:
                return None
            state = self._states[stream_id]

        with state.lock:
            return state.session_url

    def update_pending_text(self, stream_id: str, text: str):
        """更新实时累积的回复文本（SSE 流式生成中）

        Args:
            stream_id: 流式消息ID
            text: 当前已累积的完整回复文本
        """
        with self._lock:
            if stream_id not in self._states:
                return
            state = self._states[stream_id]

        with state.lock:
            state.pending_text = text
            state.update_timestamp()

    def add_tool_call(self, stream_id: str, tool_name: str, params: Dict):
        """添加工具调用步骤

        Args:
            stream_id: 流式消息ID
            tool_name: 工具名称
            params: 工具参数
        """
        with self._lock:
            if stream_id not in self._states:
                logger.warning(
                    f"[StreamingThinkingManager] stream_id={stream_id} 不存在，无法添加工具调用"
                )
                return

            state = self._states[stream_id]

        with state.lock:
            state.collector.add_tool_call(tool_name, params)
            state.update_timestamp()
            logger.debug(
                f"[StreamingThinkingManager] 添加工具调用: stream_id={stream_id}, "
                f"tool={tool_name}"
            )

    def add_tool_result(
        self,
        stream_id: str,
        tool_name: str,
        result: str,
        success: bool,
        elapsed: float
    ):
        """添加工具执行结果

        Args:
            stream_id: 流式消息ID
            tool_name: 工具名称
            result: 执行结果
            success: 是否成功
            elapsed: 执行耗时
        """
        with self._lock:
            if stream_id not in self._states:
                logger.warning(
                    f"[StreamingThinkingManager] stream_id={stream_id} 不存在，无法添加工具结果"
                )
                return

            state = self._states[stream_id]

        with state.lock:
            state.collector.add_tool_result(tool_name, result, success, elapsed)
            state.update_timestamp()
            logger.debug(
                f"[StreamingThinkingManager] 添加工具结果: stream_id={stream_id}, "
                f"tool={tool_name}, success={success}"
            )

    def add_generating(self, stream_id: str, content: str):
        """添加生成步骤

        Args:
            stream_id: 流式消息ID
            content: 生成状态描述
        """
        with self._lock:
            if stream_id not in self._states:
                logger.warning(
                    f"[StreamingThinkingManager] stream_id={stream_id} 不存在，无法添加生成步骤"
                )
                return

            state = self._states[stream_id]

        with state.lock:
            state.collector.add_generating(content)
            state.update_timestamp()
            logger.debug(
                f"[StreamingThinkingManager] 添加生成步骤: stream_id={stream_id}"
            )

    def mark_complete(self, stream_id: str, final_answer: str):
        """标记完成

        Args:
            stream_id: 流式消息ID
            final_answer: 最终答案
        """
        with self._lock:
            if stream_id not in self._states:
                logger.warning(
                    f"[StreamingThinkingManager] stream_id={stream_id} 不存在，无法标记完成"
                )
                return

            state = self._states[stream_id]

        with state.lock:
            state.collector.add_end("回复生成完成")
            state.is_complete = True
            state.final_answer = final_answer
            state.pending_text = ""  # 完成后清除实时文本，由 final_answer 接管
            state.update_timestamp()
            logger.info(
                f"[StreamingThinkingManager] 标记完成: stream_id={stream_id}, "
                f"answer_len={len(final_answer)}"
            )

    def mark_complete_with_card(self, stream_id: str, final_answer: str, card: dict):
        """标记完成并附带模板卡片（AskUserQuestion 场景）

        Args:
            stream_id: 流式消息ID
            final_answer: 最终文本内容
            card: vote_interaction 模板卡片
        """
        with self._lock:
            if stream_id not in self._states:
                logger.warning(
                    f"[StreamingThinkingManager] stream_id={stream_id} 不存在，无法标记完成(with card)"
                )
                return

            state = self._states[stream_id]

        with state.lock:
            state.collector.add_end("等待用户选择")
            state.is_complete = True
            state.final_answer = final_answer
            state.pending_text = ""
            state.pending_card = card
            state.update_timestamp()
            logger.info(
                f"[StreamingThinkingManager] 标记完成(with card): stream_id={stream_id}, "
                f"answer_len={len(final_answer)}, card_type={card.get('card_type', '')}"
            )

    def get_pending_card(self, stream_id: str) -> Optional[dict]:
        """获取附带的模板卡片（取后清除）

        Args:
            stream_id: 流式消息ID

        Returns:
            Optional[dict]: 模板卡片，无则返回 None
        """
        with self._lock:
            if stream_id not in self._states:
                return None
            state = self._states[stream_id]

        with state.lock:
            card = state.pending_card
            state.pending_card = None  # 取后清除
            return card

    def has_stream(self, stream_id: str) -> bool:
        """检查流式思考是否存在

        Args:
            stream_id: 流式消息ID

        Returns:
            bool: 是否存在
        """
        with self._lock:
            return stream_id in self._states

    def clear_stream(self, stream_id: str):
        """清理流式思考状态

        Args:
            stream_id: 流式消息ID
        """
        with self._lock:
            if stream_id in self._states:
                del self._states[stream_id]
                logger.info(
                    f"[StreamingThinkingManager] 清理流式思考: stream_id={stream_id}"
                )

    def cleanup_expired(self, timeout_seconds: float = 300):
        """清理过期的流式思考状态

        Args:
            timeout_seconds: 超时时间（秒），默认5分钟
        """
        import time
        current_time = time.time()

        with self._lock:
            expired_streams = [
                stream_id
                for stream_id, state in self._states.items()
                if current_time - state.updated_at > timeout_seconds
            ]

            for stream_id in expired_streams:
                del self._states[stream_id]
                logger.info(
                    f"[StreamingThinkingManager] 清理过期流式思考: stream_id={stream_id}"
                )

            if expired_streams:
                logger.info(
                    f"[StreamingThinkingManager] 清理了 {len(expired_streams)} 个过期流式思考"
                )


# 全局单例
_global_streaming_thinking_manager: Optional[StreamingThinkingManager] = None
_manager_lock = threading.Lock()


def get_streaming_thinking_manager() -> StreamingThinkingManager:
    """获取全局流式思考管理器单例

    Returns:
        StreamingThinkingManager: 全局管理器实例
    """
    global _global_streaming_thinking_manager

    if _global_streaming_thinking_manager is None:
        with _manager_lock:
            if _global_streaming_thinking_manager is None:
                _global_streaming_thinking_manager = StreamingThinkingManager()

    return _global_streaming_thinking_manager
