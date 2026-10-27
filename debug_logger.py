"""
Colored terminal logging system.

TWITTER_LOG_LEVEL (defaults to max):
  max, trace, all, verbose — debug + trace (maximum verbosity, including Selenium).
  normal, debug — debug without trace.
  quiet, minimal, errors — only info and above (no debug/trace).
"""
import os
import sys
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union


class LogLevel(Enum):
    TRACE = "📍"
    DEBUG = "🔍"
    INFO = "ℹ️"
    SUCCESS = "✅"
    WARNING = "⚠️"
    ERROR = "❌"
    CRITICAL = "🔴"


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"


Rows = Union[Mapping[str, Any], Sequence[Tuple[str, Any]]]


def _log_level_from_env() -> Tuple[bool, bool]:
    """
    TWITTER_LOG_LEVEL:
      max | trace | all  — debug + trace (maximum verbosity)
      normal | debug     — debug without trace
      quiet | minimal    — info level and above
    """
    raw = os.getenv("TWITTER_LOG_LEVEL", "max").strip().lower()
    if raw in ("quiet", "minimal", "errors", "error"):
        return False, False
    if raw in ("max", "trace", "all", "verbose"):
        return True, True
    return True, False


class DebugLogger:
    def __init__(self, verbose: bool = True, trace: bool = False) -> None:
        self.verbose = verbose
        # Do not name self.trace to avoid shadowing trace() method which causes 'bool' object is not callable.
        self._trace_on = trace
        self.indent_level = 0

    @property
    def trace_enabled(self) -> bool:
        return self._trace_on

    @property
    def verbose_enabled(self) -> bool:
        return self.verbose

    def _timestamp(self) -> str:
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]

    def _indent(self) -> str:
        return "  " * self.indent_level

    def _colored(self, text: str, color: str, bold: bool = False) -> str:
        if not sys.stdout.isatty():
            return text
        style = Colors.BOLD if bold else ""
        return f"{style}{color}{text}{Colors.RESET}"

    def trace(self, message: str, **kwargs: Any) -> None:
        if not self._trace_on:
            return
        self._emit(LogLevel.TRACE, message, Colors.BRIGHT_MAGENTA, bold=False, **kwargs)

    def debug(self, message: str, **kwargs: Any) -> None:
        if not self.verbose:
            return
        self._emit(LogLevel.DEBUG, message, Colors.CYAN, **kwargs)

    def info(self, message: str, **kwargs: Any) -> None:
        self._emit(LogLevel.INFO, message, Colors.BLUE, **kwargs)

    def success(self, message: str, **kwargs: Any) -> None:
        self._emit(LogLevel.SUCCESS, message, Colors.GREEN, bold=True, **kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        self._emit(LogLevel.WARNING, message, Colors.YELLOW, bold=True, **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        self._emit(LogLevel.ERROR, message, Colors.RED, bold=True, **kwargs)

    def critical(self, message: str, **kwargs: Any) -> None:
        self._emit(LogLevel.CRITICAL, message, Colors.BRIGHT_RED, bold=True, **kwargs)

    def _emit(
        self,
        level: LogLevel,
        message: str,
        color: str,
        bold: bool = False,
        **kwargs: Any,
    ) -> None:
        timestamp = self._colored(self._timestamp(), Colors.BRIGHT_BLACK)
        level_str = self._colored(level.value, color, bold=bold)
        indent = self._indent()
        log_line = f"[{timestamp}] {level_str} {indent}{message}"
        print(log_line)
        for key, value in kwargs.items():
            extra = f"{indent}    └─ {key}: {value}"
            print(self._colored(extra, Colors.BRIGHT_BLACK))

    def section(self, title: str, subtitle: Optional[str] = None) -> None:
        print()
        w = 62
        inner = w - 4
        line = "═" * (w - 2)
        print(self._colored(f"╔{line}╗", Colors.BRIGHT_CYAN, bold=True))
        print(self._colored(f"║  {title.upper():<{inner}}  ║", Colors.BRIGHT_CYAN, bold=True))
        if subtitle:
            sub = subtitle[:inner]
            print(self._colored(f"║  {sub:<{inner}}  ║", Colors.BRIGHT_BLACK))
        print(self._colored(f"╚{line}╝", Colors.BRIGHT_CYAN, bold=True))
        print()

    def subsection(self, title: str) -> None:
        bar = self._colored(" ─── ", Colors.BRIGHT_BLUE)
        print(f"{bar}{self._colored(title, Colors.BRIGHT_BLUE, bold=True)}{bar}")

    def divider(self, char: str = "─", width: int = 62) -> None:
        print(self._colored(char * width, Colors.BRIGHT_BLACK))

    def banner(self, lines: Sequence[str]) -> None:
        print()
        for ln in lines:
            print(self._colored(f"  {ln}", Colors.BRIGHT_CYAN, bold=True))
        self.divider("·", 48)

    def panel(self, title: str, rows: Rows) -> None:
        if isinstance(rows, Mapping):
            pairs: List[Tuple[str, Any]] = [(str(k), v) for k, v in rows.items()]
        else:
            pairs = [(str(k), v) for k, v in rows]
        key_w = max((len(a) for a, _ in pairs), default=0)
        key_w = min(max(key_w, 8), 28)
        top = f"╭─ {title} " + "─" * max(0, 44 - len(title)) + "╮"
        print(self._colored(top, Colors.BRIGHT_CYAN, bold=True))
        pipe = self._colored("│", Colors.BRIGHT_BLACK)
        for k, v in pairs:
            ks = self._colored(f"{k:<{key_w}}", Colors.CYAN)
            vs = str(v)
            if len(vs) > 120:
                vs = vs[:117] + "..."
            print(f"  {ks}  {pipe}  {vs}")
        print(self._colored("╰" + "─" * 50 + "╯", Colors.BRIGHT_CYAN))

    def step(self, current: int, total: int, title: str) -> None:
        pct = 100 * current / total if total else 0
        tag = self._colored(f"[{current}/{total}]", Colors.BRIGHT_MAGENTA, bold=True)
        pct_s = self._colored(f"({pct:.0f}%)", Colors.BRIGHT_BLACK)
        print(f"{tag} {self._colored(title, Colors.BRIGHT_WHITE)} {pct_s}")

    def timing(self, label: str, seconds: float) -> None:
        s = f"{seconds:.3f}s"
        if self._trace_on:
            self.trace(f"⏱ {label}: {s}")
        elif self.verbose:
            self.debug(f"⏱ {label}: {s}")

    def table_header(self, *columns: str) -> None:
        header = " | ".join(f"{col:<20}" for col in columns)
        print(self._colored(header, Colors.BRIGHT_GREEN, bold=True))
        print(self._colored("─" * (len(header) + 10), Colors.BRIGHT_GREEN))

    def table_row(self, *values: Any) -> None:
        row = " | ".join(f"{str(val):<20}" for val in values)
        print(row)

    def table_separator(self, width: int = 80) -> None:
        print(self._colored("─" * width, Colors.BRIGHT_BLACK))

    def push_indent(self) -> None:
        self.indent_level += 1

    def pop_indent(self) -> None:
        self.indent_level = max(0, self.indent_level - 1)

    def progress(self, current: int, total: int, label: str = "") -> None:
        percentage = (current / total) * 100 if total > 0 else 0
        filled = int(20 * current / total) if total > 0 else 0
        bar = "█" * filled + "░" * (20 - filled)
        progress_str = f"[{bar}] {percentage:6.1f}% ({current}/{total})"
        if label:
            progress_str = f"{label}: {progress_str}"
        self.info(progress_str)

    def stats(self, title: str, data: dict) -> None:
        self.subsection(title)
        for key, value in data.items():
            print(f"  {self._colored(key, Colors.CYAN)}: {value}")

    def wallet_result(
        self,
        address: str,
        chain: str,
        exists: Optional[bool] = None,
        balance: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        if exists is True:
            status = self._colored("✓", Colors.GREEN)
        elif exists is False:
            status = self._colored("✗", Colors.RED)
        else:
            status = self._colored("?", Colors.YELLOW)
        result = f"{status} {chain.upper():<10} {address:<45}"
        if balance:
            result += f" | Balance: {balance}"
        if error:
            result += f" | {error}"
        print(result)


_v, _t = _log_level_from_env()
logger = DebugLogger(verbose=_v, trace=_t)
