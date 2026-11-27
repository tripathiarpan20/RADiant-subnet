"""Gate g4: reject solution code that hides what it does (rule set ``radiant-v1``).

Adapted from Pareton's gate/obfuscation.py (deterministic, same answer every
time) for whole solution files rather than patch lines. It doesn't try to
prove code honest; it refuses the constructs that make code hard to review
or that build behaviour at runtime: executing strings, decoding payloads,
reflection with computed names, loading native code, and text aimed at
reviewers. A legitimate solution never needs these to run a classifier.

Shared by ``radiant-sdk validate`` and the validator's worker, so both give
the same ``OBFUSCATED`` verdict with the same rule name and line.
"""

from __future__ import annotations

import io
import re
import tokenize
import unicodedata
from typing import Mapping

from . import reasons as R

RULE_SETS = ("radiant-v1",)


def _rules(*pairs: tuple[str, str]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple((name, re.compile(p)) for name, p in pairs)


# Every line of every text file, comments included.
GENERAL = _rules(
    ("encoded_blob", r"[A-Za-z0-9+/]{120,}={0,2}"),
    ("encoded_blob", r"(?:0x)?[0-9a-fA-F]{128,}"),
    ("reviewer_directed_text", r"(?i)\b(?:ignore|disregard)\s+(?:all\s+|any\s+|the\s+)?"
                               r"(?:previous|prior|above|earlier)\s+(?:instructions?|rules?|prompts?)"),
    ("reviewer_directed_text", r"(?i)\byou\s+are\s+(?:an?\s+)?(?:ai|llm|language\s+model|assistant|reviewer|"
                               r"grader|validator)\b"),
    ("reviewer_directed_text", r"(?i)\b(?:note\s+to\s+)?(?:reviewer|grader|validator)\s*:"),
)

_NON_LITERAL_ARG = r"\s*,(?!\s*[\"'][\w.]*[\"']\s*[,)])"

# Python code with comments removed (strings kept).
PYTHON = _rules(
    ("dynamic_code", r"(?<![\w.])(?<!def )(?:exec|eval|compile)\s*\("),
    ("dynamic_code", r"\b__import__\s*\("),
    ("dynamic_code", r"\bimportlib\s*\.\s*(?:import_module\s*\((?!\s*[rbuf]*[\"'][A-Za-z_][\w.]*[\"']\s*\))"
                     r"|reload|util\s*\.\s*(?:spec_from|module_from))|\bfrom\s+importlib(?:\.util)?\s+import\b"),
    ("dynamic_code", r"\bmarshal\s*\.\s*loads?\b"),
    ("dynamic_code", r"\b(?:pickle|dill|cloudpickle|joblib)\s*\.\s*loads?\b"),
    ("dynamic_code", r"\btypes\s*\.\s*(?:CodeType|FunctionType)\b"),
    ("dynamic_code", r"\b(?:import\s+builtins|from\s+builtins\s+import)\b"),
    ("dynamic_code", r"\b__builtins__\b"),
    ("encoded_payload", r"\b(?:b64decode|b32decode|b16decode|b85decode|a85decode|decodebytes|unhexlify|a2b_\w+)\b"),
    ("encoded_payload", r"\b(?:zlib|lzma|bz2|gzip)\s*\.\s*decompress\b"),
    ("encoded_payload", r"\bcodecs\s*\.\s*(?:decode|encode|getdecoder)\b"),
    ("encoded_payload", r"\bbytes\s*\.\s*fromhex\b"),
    ("encoded_payload", r"(?i)['\"]rot.?13['\"]"),
    ("reflection", r"\b(?:getattr|setattr|delattr)\s*\(\s*(?:sys|os|builtins|importlib|radiant_sdk\w*)"
                   + _NON_LITERAL_ARG),
    ("reflection", r"\b(?:globals|locals)\s*\(\s*\)\s*(?:\[|\.\s*(?:update|setdefault|get)\b)"),
    ("reflection", r"\bsys\s*\.\s*modules\s*(?:\[|\.\s*(?:update|setdefault|pop|get)\b)"),
    ("reflection", r"\.__(?:code|globals|closure)__\b"),
    ("reflection", r"\.__dict__\s*\[(?!\s*[rbu]?[\"'][\w.]*[\"']\s*\])"),
    ("reflection", r"\b(?:operator\s*\.\s*(?:attrgetter|methodcaller)|inspect\s*\.\s*(?:stack|currentframe))\b"),
    ("reflection", r"\bsys\s*\.\s*(?:_getframe|settrace|setprofile|addaudithook)\b"),
    ("runtime_native_code", r"\b(?:load_inline|cpp_extension|cffi|nvrtc|RawKernel|RawModule)\b"),
    ("runtime_native_code", r"\bctypes\b"),
    ("runtime_native_code", r"\btorch\s*\.\s*ops\s*\.\s*load_library\b"),
    ("process_or_shell", r"\bos\s*\.\s*(?:system|popen|exec\w*|spawn\w*|fork\w*|kill\w*|_exit)\s*\("),
)


def _strip_python_comments(src: str) -> str:
    """Blank out comments, keeping line numbers. Falls back to the source if it won't tokenize."""
    try:
        lines = src.splitlines(keepends=True)
        out = list(lines)
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                row, col = tok.start
                out[row - 1] = out[row - 1][:col] + "\n"
        return "".join(out)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src


def _non_ascii_identifier(src: str) -> tuple[int, str] | None:
    """Identifiers must be ASCII: look-alike letters hide which name is used."""
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.NAME and not tok.string.isascii():
                return tok.start[0], tok.string
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return None


def scan(files: Mapping[str, bytes], rule_set: str = "radiant-v1") -> list[tuple[str, str, int, str]]:
    """[(rule, path, line, snippet)] for every finding, in file order."""
    if rule_set not in RULE_SETS:
        raise ValueError(f"unknown obfuscation rule set {rule_set!r}")
    findings = []
    for path in sorted(files):
        if path == ".env":
            continue
        text = files[path].decode("utf-8", "replace")
        for n, line in enumerate(text.splitlines(), 1):
            for name, rx in GENERAL:
                if rx.search(line):
                    findings.append((name, path, n, line.strip()[:120]))
        if path.endswith(".py"):
            code = _strip_python_comments(text)
            for n, line in enumerate(code.splitlines(), 1):
                for name, rx in PYTHON:
                    if rx.search(line):
                        findings.append((name, path, n, line.strip()[:120]))
            bad = _non_ascii_identifier(text)
            if bad is not None:
                ch = next((c for c in bad[1] if not c.isascii()), bad[1][0])
                findings.append(("non_ascii_identifier", path, bad[0],
                                 f"{bad[1]!r} ({unicodedata.name(ch, '?')})"))
    return findings


def check(files: Mapping[str, bytes], rule_set: str = "radiant-v1") -> None:
    found = scan(files, rule_set)
    if found:
        rule, path, line, snippet = found[0]
        more = f" (+{len(found) - 1} more)" if len(found) > 1 else ""
        raise R.SolutionRejected(R.OBFUSCATED, f"{rule} at {path}:{line}: {snippet}{more}")
