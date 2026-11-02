"""Literal-only Lua exchange. SavedVariables are parsed, never executed."""

import json
import math
import re

SPACE = re.compile(r"(?:\s+|--[^\n]*(?:\n|$))")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")
STRING_SPECIAL = {'"': re.compile(r'["\\]'), "'": re.compile(r"['\\]")}


def lua_literal(value):
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("Non-finite Lua value")
        return str(value)
    if isinstance(value, str):
        text = json.dumps(value, ensure_ascii=False)
        return re.sub(r"\\u00([0-9a-fA-F]{2})", lambda m: "\\" + f"{int(m[1], 16):03d}", text)
    if isinstance(value, list):
        return "{" + ",".join(lua_literal(v) for v in value) + "}"
    if isinstance(value, dict):
        return (
            "{"
            + ",".join("[" + lua_literal(k) + "]=" + lua_literal(v) for k, v in value.items())
            + "}"
        )
    raise ValueError("Unsupported Lua data type")


class Parser:
    def __init__(self, text: str, *, max_bytes: int = 16_000_000, max_tokens: int = 500_000):
        if len(text.encode("utf-8")) > max_bytes:
            raise ValueError(f"SavedVariables exceeds {max_bytes // 1_000_000} MB")
        self.text, self.pos, self.tokens = text.lstrip("\ufeff"), 0, 0
        self.max_tokens = max_tokens

    def whitespace(self):
        while True:
            match = SPACE.match(self.text, self.pos)
            if not match:
                return
            self.pos += len(match[0])

    def take(self, token):
        self.whitespace()
        if self.text.startswith(token, self.pos):
            self.pos += len(token)
            return True
        return False

    def require(self, token):
        if not self.take(token):
            raise ValueError("Expected literal token: " + token)

    def identifier(self):
        self.whitespace()
        match = IDENTIFIER.match(self.text, self.pos)
        if match:
            self.pos += len(match[0])
            return match[0]
        raise ValueError("Expected identifier")

    def value(self, depth=0):
        self.whitespace()
        self.tokens += 1
        if depth > 40 or self.tokens > self.max_tokens:
            raise ValueError("SavedVariables nesting/size limit")
        if self.pos >= len(self.text):
            raise ValueError("Truncated Lua data")
        if self.text[self.pos] in "\"'":
            quote = self.text[self.pos]
            self.pos += 1
            result: list[str] = []
            while self.pos < len(self.text):
                match = STRING_SPECIAL[quote].search(self.text, self.pos)
                if match is None:
                    break
                result.append(self.text[self.pos:match.start()])
                char = match[0]
                self.pos = match.end()
                if char == quote:
                    return "".join(result)
                if char == "\\":
                    if self.pos >= len(self.text):
                        break
                    escaped = self.text[self.pos]
                    self.pos += 1
                    if escaped.isdigit():
                        digits = escaped
                        while (
                            len(digits) < 3
                            and self.pos < len(self.text)
                            and self.text[self.pos].isdigit()
                        ):
                            digits += self.text[self.pos]
                            self.pos += 1
                        if int(digits) > 255:
                            raise ValueError("Invalid Lua byte escape")
                        result.append(chr(int(digits)))
                    elif escaped == "\n":
                        result.append("\n")
                    elif escaped in {"n", "r", "t", "b", "f", "\\", '"', "'"}:
                        result.append(
                            {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(
                                escaped, escaped
                            )
                        )
                    else:
                        raise ValueError("Unsupported Lua escape")
            raise ValueError("Unterminated Lua string")
        if self.take("{"):
            entries, index = {}, 1
            while not self.take("}"):
                if self.take("["):
                    key = self.value(depth + 1)
                    self.require("]")
                    self.require("=")
                    if not isinstance(key, (str, int)) or isinstance(key, bool):
                        raise ValueError("Invalid table key")
                    value = self.value(depth + 1)
                else:
                    saved = self.pos
                    match = IDENTIFIER.match(self.text, self.pos)
                    if match:
                        key = self.identifier()
                    if match and self.take("="):
                        value = self.value(depth + 1)
                    else:
                        self.pos = saved
                        key, value = index, self.value(depth + 1)
                        index += 1
                if key in entries:
                    raise ValueError("Duplicate Lua table key")
                entries[key] = value
                if not self.take(",") and not self.take(";"):
                    self.require("}")
                    break
            if entries and set(entries) == set(range(1, len(entries) + 1)):
                return [entries[k] for k in range(1, len(entries) + 1)]
            return entries
        match = NUMBER.match(self.text, self.pos)
        if match:
            self.pos += len(match[0])
            return float(match[0]) if any(c in match[0] for c in ".eE") else int(match[0])
        word = self.identifier()
        if word not in ("nil", "true", "false"):
            raise ValueError("Executable Lua is not allowed")
        return {"nil": None, "true": True, "false": False}[word]


def read_saved_variables(text: str, *, max_bytes: int = 16_000_000, max_tokens: int = 500_000) -> dict:
    parser = Parser(text, max_bytes=max_bytes, max_tokens=max_tokens)
    if parser.identifier() != "CennikAddonDB":
        raise ValueError("Expected CennikAddonDB assignment")
    parser.require("=")
    data = parser.value()
    parser.whitespace()
    if parser.pos != len(parser.text) or not isinstance(data, dict):
        raise ValueError("Only one literal SavedVariables assignment is allowed")
    return data
