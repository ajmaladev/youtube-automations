"""Render config/config.toml (with ${VAR} placeholders) into a real MPT config.

Stdlib only: this file is mounted into the MoneyPrinterTurbo container and run
at container start, so secrets come from the container environment and are
never written into this repository.

Placeholder forms (all keep the template valid TOML):
    key  = "${VAR}"              -> TOML string (escaped), "" if unset
    key  = "${VAR:-default}"     -> TOML string with a fallback
    keys = ["${VAR}"]            -> TOML list; comma-separated values, [] if unset
Derived variables:
    OLLAMA_BASE_URL              <- OLLAMA_HOST with "/v1" appended if missing

Usage: python render_config.py <template> <destination>
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Mapping

_LIST_RE = re.compile(r'\[\s*"\$\{([A-Za-z_][A-Za-z0-9_]*)\}"\s*\]')
_STR_RE = re.compile(r'"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}"]*))?\}"')


def _toml_str(value: str) -> str:
    # JSON string escaping is a valid subset of TOML basic-string escaping.
    return json.dumps(value, ensure_ascii=False)


def derived_env(env: Mapping[str, str]) -> dict[str, str]:
    out = dict(env)
    host = out.get("OLLAMA_HOST", "").strip().rstrip("/")
    if host and "OLLAMA_BASE_URL" not in env:
        if not host.startswith(("http://", "https://")):
            host = "http://" + host
        out["OLLAMA_BASE_URL"] = host if host.endswith("/v1") else host + "/v1"
    return out


def render(template: str, env: Mapping[str, str]) -> str:
    env = derived_env(env)

    def list_sub(m: re.Match) -> str:
        items = [v.strip() for v in env.get(m.group(1), "").split(",") if v.strip()]
        return "[" + ", ".join(_toml_str(v) for v in items) + "]"

    def str_sub(m: re.Match) -> str:
        value = env.get(m.group(1), "")
        if not value and m.group(2) is not None:
            value = m.group(2)
        return _toml_str(value)

    return _STR_RE.sub(str_sub, _LIST_RE.sub(list_sub, template))


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    src, dst = argv[1], argv[2]
    with open(src, encoding="utf-8") as f:
        rendered = render(f.read(), os.environ)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(rendered)
    print(f"render_config: wrote {dst}")  # never print values
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
