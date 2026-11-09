#!/usr/bin/env python3
"""Extract every /api/v2 route from server.py's dispatch code.

Used by the OpenAPI spec generator and by tests/test_openapi.py (which fails
when a route exists in the dispatcher but not in the spec).

The dispatchers are plain if/elif chains, so this is a small, deliberate
parser rather than a runtime introspection trick: it reads
`_handle_api_get` and `_handle_api_post_put`, tracks the current HTTP method
(including `and method == "X"` guards), and turns each `api_path == "..."` or
`re.match(r"^...$", api_path)` into a route template (`/api/v2/projects/{id}`).

Run:  python3 scripts/extract_api_routes.py [--json]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# /api/v2/... only: /api/bot/*, /api/config, /api/health are intentionally
# excluded from the documented surface (they are internal/bot endpoints).
PREFIX = "/api/v2/"

_EQ_RE = re.compile(r'api_path == "([^"]+)"')
_METHOD_GUARD = re.compile(r'and\s+method\s*==\s*"([A-Z]+)"')
_METHOD_IN = re.compile(r'and\s+method\s+in\s+\(([^)]+)\)')
_RE_MATCH = re.compile(r'm = re\.match\(r"(\^[^"]+)"\s*,\s*api_path\)')
_STARTSWITH = re.compile(r'api_path\.startswith\("([^"]+)"\)')


def _regex_to_template(pattern: str) -> str:
    """Turn a dispatch regex into an OpenAPI path template."""
    text = pattern
    if text.startswith("^"):
        text = text[1:]
    if text.endswith("$"):
        text = text[:-1]
    # (\d+) / ([^/]+) / ([A-Za-z0-9_-]+) → {id} / {key} / {name}
    text = re.sub(r"\(\\d\+\)", "{id}", text)
    text = re.sub(r"\(\[\^/\]\+\)", "{key}", text)
    text = re.sub(r"\(\[\^/\]\{1,64\}\)", "{key}", text)
    text = re.sub(r"\(\[\^/\]\+\)\?", "{key}", text)
    # anything else left in parentheses is a named-ish segment
    text = re.sub(r"\([^)]*\)", "{param}", text)
    text = text.replace("\\", "")
    return _uniquify_params(text)


def _param_names(template: str) -> list[str]:
    return re.findall(r"\{([^}]+)\}", template)


def _uniquify_params(template: str) -> str:
    """OpenAPI requires unique parameter names within a path.

    The dispatch regexes use the same `(\\d+)` group name for every id, so
    `/projects/{id}/history/{id}/replies` needs the second one renamed. The new
    name is derived from the preceding segment (`history_id`), which is also
    more readable in the docs.
    """
    parts = template.split("/")
    seen: dict[str, int] = {}
    for i, part in enumerate(parts):
        m = re.fullmatch(r"\{([^}]+)\}", part)
        if not m:
            continue
        name = m.group(1)
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            prev = parts[i - 1] if i > 0 else "item"
            base = re.sub(r"[^a-z0-9]+", "_", prev.lower()).strip("_") or "item"
            if base.endswith("s") and len(base) > 3:
                base = base[:-1]
            candidate = f"{base}_id"
            n = 2
            while candidate in seen:
                candidate = f"{base}_id{n}"
                n += 1
            seen[candidate] = 1
            parts[i] = "{" + candidate + "}"
    return "/".join(parts)


def extract_routes(source: str | None = None) -> list[dict]:
    src = source if source is not None else (ROOT / "server.py").read_text(encoding="utf-8")
    routes: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(method: str, path: str, kind: str) -> None:
        if not path.startswith(PREFIX):
            return
        key = (method.upper(), path)
        if key in seen:
            return
        seen.add(key)
        routes.append({
            "method": method.upper(),
            "path": path,
            "params": _param_names(path),
            "source": kind,
        })

    # ── GET dispatcher ────────────────────────────────────────────────────
    get_start = src.index("def _handle_api_get")
    get_end = src.index("def _handle_api_post_put")
    get_src = src[get_start:get_end]
    for line in get_src.splitlines():
        m = _EQ_RE.search(line)
        if m:
            add("GET", m.group(1), "eq")
            continue
        m = _STARTSWITH.search(line)
        if m and m.group(1).startswith(PREFIX):
            add("GET", m.group(1).rstrip("/") + "/{path}", "startswith")
            continue
        m = _RE_MATCH.search(line)
        if m:
            add("GET", _regex_to_template(m.group(1)), "regex")

    # ── POST/PUT/PATCH/DELETE dispatcher ──────────────────────────────────
    post_src = src[get_end:]
    # The dispatcher ends at the next top-level handler section marker.
    end_marker = "    # ── API tokens (admin session only)"
    if end_marker in post_src:
        post_src = post_src[:post_src.index(end_marker)]

    pending_regex: str | None = None
    for line in post_src.splitlines():
        m = _RE_MATCH.search(line)
        if m:
            pending_regex = m.group(1)
        if "if m and method" in line and pending_regex:
            guard = _METHOD_GUARD.search(line)
            if guard:
                add(guard.group(1), _regex_to_template(pending_regex), "regex")
            else:
                multi = _METHOD_IN.search(line)
                if multi:
                    for verb in re.findall(r'"([A-Z]+)"', multi.group(1)):
                        add(verb, _regex_to_template(pending_regex), "regex")
            pending_regex = None
            continue
        if "if m" in line and pending_regex and "method" not in line:
            # GET-style `if m:` inside the post/put dispatcher → all write
            # methods are routed through the same handler, so record the
            # methods the dispatcher can see.
            add("GET", _regex_to_template(pending_regex), "regex")
            pending_regex = None
            continue
        eq = _EQ_RE.search(line)
        if eq:
            guard = _METHOD_GUARD.search(line)
            if guard:
                add(guard.group(1), eq.group(1), "eq")
            elif "method ==" not in line:
                # `if api_path == "..."` without a method guard inside the
                # write dispatcher still means "not GET" (POST/PUT/DELETE/PATCH).
                add("POST", eq.group(1), "eq")

    routes.sort(key=lambda r: (r["path"], r["method"]))
    return routes


def main(argv: list[str]) -> int:
    routes = extract_routes()
    if "--json" in argv:
        print(json.dumps(routes, indent=2))
    else:
        for r in routes:
            print(f"{r['method']:6} {r['path']}")
        print(f"\n{len(routes)} routes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
