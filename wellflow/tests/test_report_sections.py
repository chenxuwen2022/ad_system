"""report_sections 归一化测试：解析正确性 + prompt 模板字段覆盖。"""

from __future__ import annotations

import re
import unittest

from wellflow.app.prompt.constant import PRODUCT_ANALYZER_SYSTEM_PROMPT
from wellflow.app.prompt.report_sections import (
    SECTION_FIELDS,
    SECTION_IGNORED_FIELDS,
    build_report_sections,
    parse_report_fields,
)

SAMPLE_REPORT = """## 服饰类产品信息识别报告
**产品类型**：服装-上装-针织衫
**产品规格**：修身版型、羊毛混纺
**核心卖点**：柔软亲肤、利落廓形
**目标受众**：25-35 岁都市女性
**品牌调性**：极简、克制、高级
**产品细节**：罗纹领口、细密针脚
**设计风格**：现代通勤风
**场景推荐**：咖啡厅-慵懒午后
**补充说明**：克重需补充"""


class ParseReportFieldsTest(unittest.TestCase):
    def test_parses_bold_fullwidth_colon_lines(self):
        fields = parse_report_fields(SAMPLE_REPORT)
        self.assertEqual(fields["产品类型"], "服装-上装-针织衫")
        self.assertEqual(fields["核心卖点"], "柔软亲肤、利落廓形")
        self.assertEqual(fields["目标受众"], "25-35 岁都市女性")

    def test_tolerates_list_prefix_and_halfwidth_colon(self):
        fields = parse_report_fields("- **产品类型**: 连衣裙\n* 核心卖点：显瘦")
        self.assertEqual(fields["产品类型"], "连衣裙")
        self.assertEqual(fields["核心卖点"], "显瘦")

    def test_ignores_non_field_lines(self):
        fields = parse_report_fields(SAMPLE_REPORT)
        self.assertNotIn("## 服饰类产品信息识别报告", fields)


class BuildReportSectionsTest(unittest.TestCase):
    def test_groups_fields_into_stable_keys(self):
        sections = build_report_sections(SAMPLE_REPORT)
        self.assertEqual(
            sections["positioning"],
            [
                {"label": "产品类型", "value": "服装-上装-针织衫"},
                {"label": "品牌调性", "value": "极简、克制、高级"},
                {"label": "设计风格", "value": "现代通勤风"},
            ],
        )
        self.assertEqual(
            sections["details"],
            [
                {"label": "产品规格", "value": "修身版型、羊毛混纺"},
                {"label": "产品细节", "value": "罗纹领口、细密针脚"},
            ],
        )
        self.assertEqual(
            sections["selling_points"],
            [{"label": "核心卖点", "value": "柔软亲肤、利落廓形"}],
        )
        self.assertEqual(
            sections["audience"],
            [{"label": "目标受众", "value": "25-35 岁都市女性"}],
        )

    def test_missing_fields_are_skipped(self):
        sections = build_report_sections("**核心卖点**：only one")
        self.assertEqual(sections["positioning"], [])
        self.assertEqual(
            sections["selling_points"], [{"label": "核心卖点", "value": "only one"}]
        )


class PromptCoverageTest(unittest.TestCase):
    """prompt 模板里的字段名必须全部登记到 SECTION_FIELDS 或 SECTION_IGNORED_FIELDS。

    改 prompt 字段名却没同步映射时，这个测试会失败——防止前端四块静默变空。
    """

    def test_template_fields_are_all_registered(self):
        template_fields = set(re.findall(r"\*\*([^*\n]+)\*\*：", PRODUCT_ANALYZER_SYSTEM_PROMPT))
        registered = {name for names in SECTION_FIELDS.values() for name in names}
        registered.update(SECTION_IGNORED_FIELDS)
        missing = template_fields - registered
        self.assertFalse(
            missing,
            f"prompt 模板字段未在 report_sections 映射中登记: {sorted(missing)}",
        )


if __name__ == "__main__":
    unittest.main()
