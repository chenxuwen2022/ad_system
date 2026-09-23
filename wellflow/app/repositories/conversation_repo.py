"""会话 & 消息数据访问层。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func, desc
from sqlalchemy.orm import Session

from wellflow.app.models.task_models import Conversation, ChatMessage


class ConversationRepo:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def _short_of(full_id: str) -> str:
        """把完整 conversation_id 归一化为 12 位短 ID。

        规则：去掉 '-' 后取前 12 位。这样不管前端传的是标准 UUID
        还是后端原生 short_id，最终存到 conversation_id_short 的值一致。
        """
        return full_id.replace("-", "")[:12]

    def get(self, conversation_id: str) -> Conversation | None:
        return self.db.get(Conversation, conversation_id)

    def resolve(self, any_id: str) -> Conversation | None:
        """兼容查询：既能用完整 conversation_id，也能用 short_id。

        查不到时不额外做 short_id 转换，先查完整主键，再查 short_id 唯一索引。
        前端刷新/恢复时 URL 里可能携带 short_id（12 位）或完整 UUID（36 位），
        两种格式都能命中。
        """
        # 1) 先按完整主键查（最高命中率，走 PK）
        obj = self.db.get(Conversation, any_id)
        if obj:
            return obj
        # 2) 回退：按 short_id 唯一索引查
        short = self._short_of(any_id)
        stmt = select(Conversation).where(Conversation.conversation_id_short == short)
        return self.db.execute(stmt).scalar_one_or_none()

    def create(
        self,
        conversation_id: str | None = None,
        title: str = "新对话",
        current_task_id: str | None = None,
        title_hint: dict[str, Any] | None = None,
        sku_id: int | None = None,
    ) -> Conversation:
        cid = conversation_id or uuid.uuid4().hex[:12]
        # 起名钩子：只有当 title 是占位值 且 提供了 hint 时才生成真实 title
        _effective_title = title[:128] if title else "新对话"
        if _effective_title in ("新对话", "") and title_hint:
            _effective_title = _compose_title_from_hint(title_hint)[:128] or "新对话"
        obj = Conversation(
            conversation_id=cid,
            conversation_id_short=self._short_of(cid),
            title=_effective_title,
            sku_id=sku_id,
            current_task_id=current_task_id,
        )
        self.db.add(obj)
        self.db.commit()
        return obj

    def update_title(self, conversation_id: str, title: str) -> None:
        obj = self.get(conversation_id)
        if obj:
            obj.title = title[:128]
            obj.updated_at = datetime.now(timezone.utc)
            self.db.commit()

    def update_current_task(self, conversation_id: str, task_id: str | None) -> None:
        obj = self.get(conversation_id)
        if obj:
            obj.current_task_id = task_id
            obj.updated_at = datetime.now(timezone.utc)
            self.db.commit()

    def touch(self, conversation_id: str) -> None:
        """更新 updated_at（用于列表排序）。"""
        obj = self.get(conversation_id)
        if obj:
            obj.updated_at = datetime.now(timezone.utc)
            self.db.commit()

    def delete(self, conversation_id: str) -> bool:
        obj = self.get(conversation_id)
        if not obj:
            return False
        self.db.delete(obj)
        self.db.commit()
        return True

    # ------------------------------------------------------------------
    # 列表查询
    # ------------------------------------------------------------------

    def list_conversations(
        self,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Conversation], int]:
        """返回 (items, total_count)。按 updated_at 倒序。"""
        stmt = select(Conversation)
        count_stmt = select(func.count(Conversation.conversation_id))
        total = self.db.execute(count_stmt).scalar() or 0
        stmt = stmt.order_by(desc(Conversation.updated_at))
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        items = list(self.db.execute(stmt).scalars().all())
        return items, total


class ChatMessageRepo:
    def __init__(self, db: Session):
        self.db = db

    def list_by_conversation(self, conversation_id: str) -> list[ChatMessage]:
        """按 session_index 升序返回该 conversation 的全部消息。"""
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.session_index, ChatMessage.message_id)
        )
        return list(self.db.execute(stmt).scalars().all())

    def next_session_index(self, conversation_id: str) -> int:
        """取当前最大 session_index + 1（同一 conversation 内自增）。"""
        stmt = (
            select(func.max(ChatMessage.session_index))
            .where(ChatMessage.conversation_id == conversation_id)
        )
        result = self.db.execute(stmt).scalar()
        return (result or 0) + 1

    def create(
        self,
        conversation_id: str,
        role: str,
        text: str = "",
        images_json: list[dict[str, Any]] | None = None,
        intent: str | None = None,
        task_id: str | None = None,
        session_index: int | None = None,
    ) -> ChatMessage:
        if session_index is None:
            session_index = self.next_session_index(conversation_id)
        obj = ChatMessage(
            conversation_id=conversation_id,
            role=role,
            text=text or "",
            images_json=images_json or [],
            intent=intent,
            task_id=task_id,
            session_index=session_index,
        )
        self.db.add(obj)
        self.db.commit()
        return obj

    def count_for_conversation(self, conversation_id: str) -> int:
        stmt = (
            select(func.count(ChatMessage.message_id))
            .where(ChatMessage.conversation_id == conversation_id)
        )
        return self.db.execute(stmt).scalar() or 0


# ------------------------------------------------------------------
# conversation title 生成 —— 两条路径共用的纯函数
# ------------------------------------------------------------------

_CHAT_REFINE_TARGET_LABEL: dict[str, str] = {
    "node1": "洞察报告",
    "node2": "商拍方案",
    "node3": "生图提示词",
    "node4": "图片",
}


def _truncate(text: str, n: int) -> str:
    """按字符截前 n 个，空串兜底。"""
    s = (text or "").strip()
    return (s[:n] or "").strip()


def _compose_title_from_hint(hint: dict[str, Any]) -> str:
    """根据 hint['kind'] 分派到不同的起名规则。

    所有子函数都返回最多 ~30 字的可读 title，不要超过 repo 的 128 字符上限。
    """
    kind = hint.get("kind", "")
    if kind == "chat_start_task":
        return _compose_title_chat(hint)
    if kind == "chat_edit":
        return _compose_title_chat_edit(hint)
    if kind == "button_create_task":
        return _compose_title_button(hint)
    # 兜底：message 有就截一下，没有就占位
    msg = hint.get("message") or ""
    return _truncate(str(msg), 20) or "新对话"


def _compose_title_chat(hint: dict[str, Any]) -> str:
    """路径 A：对话入口 start_task 意图。"""
    msg = str(hint.get("message") or "")
    # 优先用用户输入的前 20 字（这是用户明确表达的创作意图）
    head = _truncate(msg, 20)
    if not head:
        return "商品分析任务"
    # 已经很短的话就直接用，不加后缀
    return head if len(head) <= 15 else f"{head} 商品分析"


def _compose_title_chat_edit(hint: dict[str, Any]) -> str:
    """路径 A：对话入口 edit / refine 意图。"""
    msg = str(hint.get("message") or "")
    refine = str(hint.get("refine_target") or "")
    label = _CHAT_REFINE_TARGET_LABEL.get(refine, "")
    head = _truncate(msg, 12)
    if label and head:
        return f"修改{label} · {head}"
    if label:
        return f"修改{label}"
    return _truncate(msg, 20) or "新对话"


def _compose_title_button(hint: dict[str, Any]) -> str:
    """路径 B：按钮直跑 create_task 入口。"""
    description = str(hint.get("description") or "")
    product_link = str(hint.get("product_link") or "")
    filenames = hint.get("image_filenames") or []
    platform = str(hint.get("platform") or "")

    # 1) 用户填了 description → 首选
    if description.strip():
        d = _truncate(description, 20)
        return d if len(d) <= 15 else f"{d} 商品分析"

    # 2) 从商品链接抽 SKU/品牌（简单规则：取最后一段 path 里的非 hash 串）
    if product_link:
        tail = product_link.rstrip("/").rsplit("/", 1)[-1]
        if tail and not tail.startswith("?"):
            # 去掉常见扩展名和 hash 串
            sku = tail.split("?")[0].split("#")[0]
            sku = sku.replace(".html", "").replace(".htm", "")
            sku = _truncate(sku, 16)
            if sku and len(sku) >= 3:
                return f"{sku} 商品分析"

    # 3) 从图片 filename 抽主名
    if filenames:
        # 取第一张的主文件名，去扩展名
        name = str(filenames[0]).rsplit(".", 1)[0] if filenames else ""
        name = _truncate(name, 16)
        if name:
            return f"{name} 商品分析"

    # 4) 兜底：平台 + 商拍任务
    p_label = {"taobao": "淘宝", "jd": "京东", "douyin": "抖音"}.get(platform, platform)
    if p_label:
        return f"{p_label}商拍任务"
    return "商拍任务"
