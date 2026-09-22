"""Portable platform capability and path helpers."""

from .capabilities import capability_report
from .locks import file_lock
from .paths import default_state_dir

__all__ = ["capability_report", "default_state_dir", "file_lock"]
