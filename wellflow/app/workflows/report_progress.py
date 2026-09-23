"""Separate Node1's short recognition progress from the Markdown report."""

import re


_REPORT_TITLE = re.compile(r"(?m)^##\s*服饰类产品信息识别报告\s*(?:\r?\n|$)")


def split_report_progress(raw: str) -> tuple[str, str]:
    """Return (progress, report), preserving text if the expected title is absent."""
    match = _REPORT_TITLE.search(raw)
    if match is None:
        return "", raw
    return raw[:match.start()].strip(), raw[match.start():]


class ReportProgressStream:
    def __init__(self) -> None:
        self._pending = ""
        self._started = False
        self._before_title = ""

    def feed(self, chunk: str) -> tuple[str, str]:
        if self._started:
            return "", chunk
        self._before_title += chunk
        self._pending += chunk
        progress, report = split_report_progress(self._pending)
        if _REPORT_TITLE.search(self._pending):
            self._pending = ""
            self._started = True
            self._before_title = ""
            return progress, report

        # Emit progress as tokens arrive. Only hold a possible partial title at
        # the beginning of the current line so it never flashes in the panel.
        line_start = self._pending.rfind("\n") + 1
        tail = self._pending[line_start:]
        title = "## 服饰类产品信息识别报告"
        keep_from = line_start if tail and title.startswith(tail) else len(self._pending)
        ready = self._pending[:keep_from]
        self._pending = self._pending[keep_from:]
        return ready, ""

    def finish(self) -> str:
        """Preserve an unfinished title or a report without the expected heading."""
        pending = self._before_title if not self._started else self._pending
        self._pending = ""
        self._before_title = ""
        return pending
