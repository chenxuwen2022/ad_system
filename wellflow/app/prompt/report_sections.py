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
    "positioning": ["产品类型", "品牌调性", "设计风格"],
    "details": ["产品规格", "产品细节"],
    "selling_points": ["核心卖点"],
    "audience": ["目标受众", "目标人群"],
}

# 模板里存在但不参与四块拆分的字段（仅供完整报告展示），登记以免覆盖测试误报。
SECTION_IGNORED_FIELDS: list[str] = ["品牌名称", "品牌LOGO详情", "模特推荐", "场景推荐", "补充说明"]

# 与前端 report.ts 的解析规则保持一致：可选列表前缀、可选 ** 加粗、全/半角冒号。
_FIELD_RE = re.compile(r"^\s*(?:[-*+]\s+)?(?:\*\*)?([^*：:\n]{1,24}?)(?:\*\*)?\s*[：:]\s*(.+?)\s*$")
_EDGE_BOLD_RE = re.compile(r"^\*\*|\*\*$")


def parse_report_fields(report: str) -> dict[str, str]:
    """逐行解析 `**字段名**：值`，返回 {字段名: 值}；同名字段后出现的覆盖先出现的。"""
    fields: dict[str, str] = {}
    for line in report.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = _FIELD_RE.match(line)
        if not match:
            continue
        fields[match.group(1).strip()] = _EDGE_BOLD_RE.sub("", match.group(2)).strip()
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
