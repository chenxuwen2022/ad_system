"""Node1 商品报告 → 前端"重点洞察"四块的结构化归一化。

前端四块只消费本模块输出的稳定 key（positioning / details / selling_points / audience），
不直接匹配 prompt 里的中文字段名。修改 constant.py 的 Node1 输出模板
（改字段名、增删字段）时必须同步更新 SECTION_FIELDS / SECTION_IGNORED_FIELDS；
wellflow/tests/test_report_sections.py 会断言模板字段名全部被登记。
"""

from __future__ import annotations

import re

# 稳定 key → 报告字段名（按展示顺序排列）。prompt 模板改字段名时同步改这里。
SECTION_FIELDS: dict[str, list[str]] = {
    "positioning": ["品牌名称", "品牌 LOGO 详情", "产品类型", "品牌调性", "设计风格"],
    "details": ["配色结构", "版型与规格", "正面结构", "背面结构", "袖部结构", "拼接与剪裁", "面料与质感", "产品工艺", "AI 生成重点锁定项", "包装亮点"],
    "selling_points": ["核心卖点"],
    "audience": ["目标受众", "模特推荐", "场景推荐"],
}

# 模板里存在但不参与四块拆分的字段（仅供完整报告展示），登记以免覆盖测试误报。
SECTION_IGNORED_FIELDS: list[str] = ["补充说明"]

# 与前端 report.ts 的解析规则保持一致：可选列表前缀、可选 ** 加粗、全/半角冒号。
_FIELD_RE = re.compile(r"^\s*(?:[-*+]\s+)?(?:\*\*)?([^*：:\n]{1,24}?)(?:\*\*)?\s*[：:]\s*(.*?)\s*$")
_EDGE_BOLD_RE = re.compile(r"^\*\*|\*\*$")


def parse_report_fields(report: str) -> dict[str, str]:
    """解析单行或多行字段；编号列表保留换行，同名字段以后者为准。"""
    fields: dict[str, str] = {}
    current_name: str | None = None
    current_lines: list[str] = []

    def finish_field() -> None:
        if current_name is not None:
            fields[current_name] = "\n".join(current_lines).strip()

    for line in report.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if stripped == "---NEXT---":
            break
        if stripped.startswith("#"):
            finish_field()
            current_name, current_lines = None, []
            continue
        if not stripped:
            continue
        if re.match(r"^\d+[.)、]\s*", stripped):
            if current_name is not None:
                current_lines.append(stripped)
            continue
        match = _FIELD_RE.match(line)
        if match:
            finish_field()
            current_name = match.group(1).strip()
            value = _EDGE_BOLD_RE.sub("", match.group(2)).strip()
            current_lines = [value] if value else []
            continue
        if current_name is not None:
            current_lines.append(stripped)
    finish_field()
    return fields


def build_report_sections(report: str) -> dict[str, list[dict[str, str]]]:
    """把报告 Markdown 归一化为稳定 key 的四块结构。

    返回 {"positioning": [{"label": "产品类型", "value": "…"}, ...], ...}；
    缺失字段直接跳过，label 保留报告中的真实字段名供展示层使用。
    """
    fields = parse_report_fields(report)
    return {
        key: [{"label": name, "value": fields[name]} for name in names if fields.get(name)]
        for key, names in SECTION_FIELDS.items()
    }
