from wellflow.app.workflows.report_progress import ReportProgressStream, split_report_progress


def test_split_report_progress() -> None:
    progress, report = split_report_progress(
        "已查看三张商品图；正在整理识别报告。\n\n"
        "## 服饰类产品信息识别报告\n**品牌名称**：示例"
    )
    assert progress == "已查看三张商品图；正在整理识别报告。"
    assert report.startswith("## 服饰类产品信息识别报告")
    assert "已查看" not in report


def test_split_streamed_title_across_chunks() -> None:
    stream = ReportProgressStream()
    assert stream.feed("已查看") == ("已查看", "")
    assert stream.feed("三张商品图\n\n## 服饰类") == ("三张商品图\n\n", "")
    progress, report = stream.feed("产品信息识别报告\n品牌：示例")
    assert progress == ""
    assert report == "## 服饰类产品信息识别报告\n品牌：示例"
    assert stream.feed("\n下一行") == ("", "\n下一行")
    assert stream.finish() == ""


def test_preserve_report_without_expected_title() -> None:
    stream = ReportProgressStream()
    assert stream.feed("**品牌名称**：示例") == ("**品牌名称**：示例", "")
    assert stream.finish() == "**品牌名称**：示例"
