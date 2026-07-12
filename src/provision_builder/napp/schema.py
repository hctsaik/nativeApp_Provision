"""A tiny JSON-Schema-subset validator (stdlib only).

Supports exactly what the app/package manifests need: ``type`` (object, array,
string, integer, number, boolean), ``required``, ``properties``, ``items``,
``pattern`` and ``enum``. Enough to enforce the contract without pulling in the
third-party ``jsonschema`` package (SPEC D2 zero-third-party runtime).
"""

from __future__ import annotations

import re
from typing import Any

from provision_builder.napp.errors import InvalidManifest

_PYTYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


def _fail(path: str, message: str) -> None:
    where = path or "<root>"
    raise InvalidManifest(f"{where}: {message}")


def _check(value: Any, schema: dict, path: str) -> None:
    expected = schema.get("type")
    if expected is not None:
        pytype = _PYTYPES[expected]
        # bool is a subclass of int — reject it where a number/integer is meant.
        if expected in {"integer", "number"} and isinstance(value, bool):
            _fail(path, f"expected {expected}, got boolean")
        if not isinstance(value, pytype):
            _fail(path, f"expected {expected}, got {type(value).__name__}")

    if expected == "object":
        for key in schema.get("required", []):
            if key not in value:
                _fail(path, f"missing required property '{key}'")
        props = schema.get("properties", {})
        for key, subschema in props.items():
            if key in value:
                _check(value[key], subschema, f"{path}.{key}" if path else key)

    if expected == "array":
        item_schema = schema.get("items")
        if item_schema is not None:
            for i, item in enumerate(value):
                _check(item, item_schema, f"{path}[{i}]")

    if expected == "string":
        pattern = schema.get("pattern")
        if pattern is not None and not re.fullmatch(pattern, value):
            _fail(path, f"{value!r} does not match {pattern}")

    if "enum" in schema and value not in schema["enum"]:
        _fail(path, f"{value!r} not in {schema['enum']}")


def validate(instance: Any, schema: dict) -> None:
    """Raise :class:`InvalidManifest` if ``instance`` violates ``schema``."""
    _check(instance, schema, "")
