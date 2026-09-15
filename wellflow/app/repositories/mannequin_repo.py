"""模特库数据访问层。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import select, func, desc, and_, or_
from sqlalchemy.orm import Session

from wellflow.app.models.mannequin_models import (
    Mannequin, MannequinTag, MannequinGenerateLog,
)


# 维度枚举（硬编码 + DB 可选动态扩展）
MANNEQUIN_DIMENSION_GROUPS: dict[str, dict[str, list[str]]] = {
    "基础身份": {
        "性别": ["女", "男", "儿童", "中性"],
        "年龄": ["18-25", "25-35", "35-45", "45+"],
        "人种/地域特征": ["东亚", "欧美", "东南亚", "混血", "其他"],
        "肤色": ["白皙", "自然", "小麦", "黝黑"],
        "身材": ["纤瘦", "标准", "健身", "丰满"],
        "身高感": ["Petite（娇小）", "标准", "Tall（高挑）", "Athletic（健美）"],
    },
    "面部": {
        "脸型": ["圆脸", "方脸", "鹅蛋脸", "长脸", "心形脸"],
        "五官": ["立体", "平面", "浓颜", "淡颜"],
        "妆容": ["素颜感", "淡妆", "精致妆", "浓妆"],
        "发型": ["长发", "中发", "短发", "盘发", "卷发"],
        "发色": ["黑", "棕", "金", "红", "染浅"],
    },
    "风格": {
        "模特风格": ["极简", "高级", "青春", "运动", "通勤", "轻奢", "生活方式", "潮流", "温暖", "都市", "专业", "性别模糊"],
    },
    "使用场景": {
        "模特使用场景": ["服装展示", "城市通勤", "情绪氛围", "户外", "居家", "运动", "家庭生活", "专业功能展示"],
    },
}


class MannequinRepo:
    def __init__(self, db: Session):
        self.db = db

    # ==================================================================
    # CRUD
    # ==================================================================

    def get(self, mannequin_id: int) -> Mannequin | None:
        return self.db.get(Mannequin, mannequin_id)

    def create(
        self,
        *,
        name: str,
        en_name: str | None = None,
        scope: str = "mine",
        origin: str = "upload",
        cover_storage_uri: str | None = None,
        description: str | None = None,
        tags: list[dict[str, Any]] | None = None,
        generate_model: str | None = None,
        generate_prompt: str | None = None,
        # ── 生成上下文（写 generate_log 用） ──
        input_desc: str | None = None,
        input_refs: list[str] | None = None,
        final_prompt: str | None = None,
        num_output: int = 1,
        # 微调上下文
        fine_tune_from: str | None = None,
        fine_tune_prompt: str | None = None,
    ) -> Mannequin:
        obj = Mannequin(
            mannequin_no=_gen_mannequin_no(self.db),
            name=name.strip(),
            en_name=en_name,
            scope=scope,
            origin=origin,
            cover_storage_uri=cover_storage_uri,
            description=description,
            generate_model=generate_model,
            generate_prompt=generate_prompt or final_prompt,
        )
        self.db.add(obj)
        self.db.flush()

        # 写标签
        if tags:
            self._replace_tags(obj.id, tags)

        # 写生成日志（路径 A 时 input_desc / final_prompt 全为 None，跳过）
        if origin == "ai_generate" or input_desc or final_prompt:
            # 首轮
            first_log = MannequinGenerateLog(
                mannequin_id=obj.id,
                round_type="first_round",
                input_desc=input_desc,
                input_refs=input_refs,
                final_prompt=final_prompt,
                generate_model=generate_model,
                num_output=num_output,
                output_uris=[cover_storage_uri] if cover_storage_uri else None,
                status="success",
            )
            self.db.add(first_log)
            self.db.flush()

            # 如果走了微调 → 再追加一条
            if fine_tune_from:
                tune_log = MannequinGenerateLog(
                    mannequin_id=obj.id,
                    round_type="fine_tune",
                    parent_log_id=first_log.id,
                    target_image_uri=fine_tune_from,
                    input_desc=fine_tune_prompt,
                    input_refs=input_refs,
                    final_prompt=final_prompt,  # 微调时用的最终 prompt
                    generate_model=generate_model,
                    num_output=1,
                    output_uris=[cover_storage_uri] if cover_storage_uri else None,
                    status="success",
                )
                self.db.add(tune_log)

        self.db.flush()
        return obj

    def update(
        self,
        mannequin_id: int,
        *,
        name: str | None = None,
        en_name: str | None = None,
        scope: str | None = None,
        cover_storage_uri: str | None = None,
        description: str | None = None,
        tags: list[dict[str, Any]] | None = None,
    ) -> Mannequin:
        obj = self.get(mannequin_id)
        if obj is None:
            raise ValueError(f"model {mannequin_id} 不存在")
        if name is not None:
            obj.name = name.strip()
        if en_name is not None:
            obj.en_name = en_name
        if scope is not None:
            obj.scope = scope
        if cover_storage_uri is not None:
            obj.cover_storage_uri = cover_storage_uri
        if description is not None:
            obj.description = description
        obj.updated_at = datetime.utcnow()

        if tags is not None:
            self._replace_tags(mannequin_id, tags)

        self.db.flush()
        return obj

    def delete(self, mannequin_id: int) -> bool:
        obj = self.get(mannequin_id)
        if obj is None:
            return False
        # 标签 CASCADE 自动清；generate_log SET NULL 自动处理
        self.db.delete(obj)
        self.db.flush()
        return True

    # ==================================================================
    # 标签
    # ==================================================================

    def _replace_tags(self, mannequin_id: int, tags: list[dict[str, Any]]) -> None:
        """全量替换标签。tags 每项: {group_key, dim_key, tag_values: [str,...]}"""
        self.db.query(MannequinTag).filter(
            MannequinTag.mannequin_id == mannequin_id
        ).delete(synchronize_session=False)

        for t in tags:
            gk = t["group_key"]
            dk = t["dim_key"]
            for val in t["tag_values"]:
                val = str(val).strip()
                if val:
                    self.db.add(MannequinTag(
                        mannequin_id=mannequin_id,
                        group_key=gk,
                        dim_key=dk,
                        tag_value=val,
                    ))

    def list_tags(self, mannequin_id: int) -> list[dict[str, Any]]:
        rows = self.db.query(MannequinTag).filter(
            MannequinTag.mannequin_id == mannequin_id
        ).all()
        # 按 (group_key, dim_key) 聚合
        grouped: dict[tuple[str, str], list[str]] = {}
        for r in rows:
            key = (r.group_key, r.dim_key)
            grouped.setdefault(key, []).append(r.tag_value)
        result = []
        for (gk, dk), vals in grouped.items():
            result.append({"group_key": gk, "dim_key": dk, "tag_values": vals})
        return result

    # ==================================================================
    # 列表查询（支持 scope + 关键词 + 多维筛选）
    # ==================================================================

    def list(
        self,
        *,
        scope: str | None = None,        # all / official / mine
        q: str | None = None,            # 自然语言或关键词
        dims: dict[str, list[str]] | None = None,   # 多维筛选 {dim_key: [val,...]}
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Mannequin], int]:
        stmt = select(Mannequin)
        count_stmt = select(func.count(Mannequin.id))

        if scope and scope != "all":
            stmt = stmt.where(Mannequin.scope == scope)
            count_stmt = count_stmt.where(Mannequin.scope == scope)

        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(or_(
                Mannequin.name.ilike(like),
                Mannequin.en_name.ilike(like),
                Mannequin.mannequin_no.ilike(like),
                Mannequin.description.ilike(like),
            ))
            count_stmt = count_stmt.where(or_(
                Mannequin.name.ilike(like),
                Mannequin.en_name.ilike(like),
                Mannequin.mannequin_no.ilike(like),
                Mannequin.description.ilike(like),
            ))

        if dims:
            # 标签筛选：每个 dim_key 至少有一个 tag_value 命中即保留
            for dk, vals in dims.items():
                subq = (
                    select(MannequinTag.mannequin_id)
                    .where(
                        MannequinTag.dim_key == dk,
                        MannequinTag.tag_value.in_(vals),
                    )
                    .distinct()
                )
                stmt = stmt.where(Mannequin.id.in_(subq))
                count_stmt = count_stmt.where(Mannequin.id.in_(subq))

        total = self.db.execute(count_stmt).scalar() or 0
        stmt = stmt.order_by(desc(Mannequin.created_at))
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        items = list(self.db.execute(stmt).scalars().all())
        return items, total


# ============================================================================
# 通用工具
# ============================================================================

def _gen_mannequin_no(db: Session) -> str:
    """生成下一个模特编号 WF-M001。"""
    from sqlalchemy import select, func
    rows = db.execute(select(Mannequin.mannequin_no)).scalars().all()
    nums = []
    for val in rows:
        m = re.search(r"(\d+)$", val or "")
        if m:
            nums.append(int(m.group(1)))
    n = (max(nums) + 1) if nums else 1
    return f"WF-M{n:03d}"
