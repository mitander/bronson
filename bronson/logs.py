"""Pure log cleanup, severity classification, and Rich presentation."""

import re
from collections.abc import Callable

from rich.text import Span, Text

LOG_MAX_LINES = 20000
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?")
ERROR_RE = re.compile(
    r"##\[error\]|^\s*::error(?:\s.*?)?::"  # GitHub annotations
    r"|^\s*(?:error(?:\[[^\]]+\])?|fatal)(?:\s+[A-Z]+\d+)?:"
    r"|:\s+(?:fatal\s+)?error(?:\s+[A-Z]+\d+)?:"  # compiler file:line diagnostics
    r"|^\s*(?:\[ERROR\]|npm\s+(?:ERR!|error\b))"
    r"|^\s*(?:FAILED|FAIL)(?:\s|:|$)"  # pytest, Jest, Go
    r"|^\s*--- FAIL:|^\s*not ok\s+\d+\b"  # Go, TAP
    r"|^\s*[✗✕×]\s+"  # test runner failure bullets
    r"|^\s*test\b.*\.\.\.\s*FAILED\b|^\s*test result: FAILED\b"
    r"|\b[1-9]\d* failed\b"  # nonzero test failure summaries
    r"|panicked at|^\s*Traceback \(most recent call last\)"
    r"|^\s*Exception in thread\b"
    r"|Process completed with exit code -?[1-9]\d*\b",
    re.IGNORECASE,
)
WARNING_RE = re.compile(r"##\[warning\]|^\s*warning:|^\s*WARN(?:ING)?:", re.IGNORECASE)
GROUP_RE = re.compile(r"##\[(?:group|endgroup)\]", re.IGNORECASE)


def clean_log_line(content: str) -> str:
    """Terminal colors must not hide diagnostics or determine their severity."""
    if "\x1b" in content:
        content = Text.from_ansi(content).plain
    return content.lstrip("\ufeff")


def log_severity(content: str) -> str | None:
    content = TIMESTAMP_RE.sub("", clean_log_line(content))
    if ERROR_RE.search(content):
        return "error"
    if WARNING_RE.search(content):
        return "warning"
    return None


def paint_log(text: Text, color: Callable[[str, str], str]) -> Text:
    """Apply current theme roles without tying failure detection to colors."""
    styles = {
        "red": color("error", "red"),
        "yellow": color("warning", "yellow"),
        "dim": color("text_muted", "dim"),
        "bold dim": "bold " + color("text_muted", "dim"),
    }
    shown = text.copy()
    shown.style = styles.get(str(text.style), text.style)
    shown.spans = [
        Span(span.start, span.end, styles.get(str(span.style), span.style)) for span in text.spans
    ]
    return shown


def style_log_line(line: Text, content: str) -> Text:
    """Dim timestamps and use the same severity as the rail and navigation."""
    content = clean_log_line(content)
    severity = log_severity(content)
    match = TIMESTAMP_RE.match(content)
    if match:
        line.append(match.group(0), style="dim")
        content = content[match.end() :]
    if severity == "error":
        line.append(content, style="red")
    elif severity == "warning":
        line.append(content, style="yellow")
    elif GROUP_RE.search(content):
        line.append(content, style="dim")
    else:
        line.append(content)
    return line


def render_log(raw: str, fallback_label: str, prefix_columns: int, label_index: int) -> list[Text]:
    """Turn gh log output into readable text.

    gh prefixes log lines with tab-separated context columns: both run-level
    (`job\tstep\tline`) and job-level (`job\tstep\tline`) logs carry two. We
    strip those columns and open a `── label ──` section whenever the chosen
    label changes — the job name for run views, the step name for job views.
    gh prints UNKNOWN when it cannot map a prefix, so unknown or missing names
    fall back to `fallback_label`.
    """
    if not raw.strip():
        return [Text("(no log output yet)", style="dim")]
    lines: list[Text] = []
    current_label: str | None = None
    for raw_line in raw.splitlines()[:LOG_MAX_LINES]:
        parts = raw_line.split("\t", prefix_columns) if prefix_columns else [raw_line]
        if not prefix_columns or len(parts) < prefix_columns + 1:
            label, content = current_label or fallback_label, raw_line
        else:
            label = parts[label_index]
            content = parts[-1]
        if label in ("", "UNKNOWN") or label.startswith("UNKNOWN"):
            label = fallback_label
        if label != current_label:
            current_label = label
            lines.append(Text(f"── {label} ", style="bold dim"))
        lines.append(style_log_line(Text(), content.lstrip("\ufeff")))
    if len(raw.splitlines()) > LOG_MAX_LINES:
        lines.append(Text(f"… truncated after {LOG_MAX_LINES} lines", style="dim italic"))
    return lines
