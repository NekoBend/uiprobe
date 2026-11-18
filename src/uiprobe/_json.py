"""JSON output that is safe to print on a terminal."""

from __future__ import annotations

import json
import re
from typing import Final

__all__ = ["dumps"]

# `json.dumps(ensure_ascii=False)` escapes only U+0000-U+001F, the quote and
# the backslash. These also act on a terminal or change how text is laid out:
# DEL, the C1 controls (U+009B is a single-character CSI), the line and
# paragraph separators, and the bidirectional formatting characters.
_UNSAFE: Final = re.compile(
    r"[\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]"
)


def dumps(value: object, *, indent: int | None = None) -> str:
    """Serialize `value` as JSON, escaping controls as ``\\uXXXX``.

    Other non-ASCII text, such as Japanese, stays readable. The text parses
    back to the same value as plain `json.dumps` would give. Outside strings
    JSON has only ASCII, so every match lies inside a string.
    """
    text = json.dumps(value, ensure_ascii=False, indent=indent)
    return _UNSAFE.sub(lambda match: f"\\u{ord(match[0]):04x}", text)
