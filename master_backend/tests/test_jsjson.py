"""``js_json_dumps`` must be byte-identical to ``JSON.stringify(x, null, 2)``.

Every expected string in the tables below was captured from real node output
(``node -e 'console.log(vals.map(v => JSON.stringify(v)).join("\\n"))'``) rather than
reasoned from Python conventions.  Note the deliberate contradiction with lane spec
§5: the spec says "never ``1e+21``", but JavaScript *does* print the plus sign for
positive exponents (``1e+21``, ``1e+22``) while omitting it for negative ones
(``1e-7``).  The spec's own governing rule — match JavaScript exactly — wins.
"""
from __future__ import annotations

import pytest

from master_backend.app.api.jsjson import js_json_dumps

# (python float, exact JSON.stringify output)
FLOAT_TABLE = [
    (0.0, "0"),
    (-0.0, "0"),
    (0.1, "0.1"),
    (2.5, "2.5"),
    (-1.5, "-1.5"),
    (-273.15, "-273.15"),
    (1.0, "1"),
    (100.0, "100"),
    (123.456, "123.456"),
    (1e15, "1000000000000000"),
    (1e16, "10000000000000000"),
    (1e20, "100000000000000000000"),
    (9007199254740992.0, "9007199254740992"),
    (1.2345678901234568e20, "123456789012345680000"),
    (1e21, "1e+21"),
    (1e22, "1e+22"),
    (-1e21, "-1e+21"),
    (1.7976931348623157e308, "1.7976931348623157e+308"),
    (0.00123, "0.00123"),
    (1 / 3, "0.3333333333333333"),
    (1e-6, "0.000001"),
    (1e-7, "1e-7"),
    (1.5e-7, "1.5e-7"),
    (1.23e-7, "1.23e-7"),
    (1e-21, "1e-21"),
    (5e-324, "5e-324"),
    (float("nan"), "null"),
    (float("inf"), "null"),
    (float("-inf"), "null"),
]

INT_TABLE = [
    (0, "0"),
    (1, "1"),
    (-1, "-1"),
    (1700000006500, "1700000006500"),
    (2**53, "9007199254740992"),
    (123456789012345680000, "123456789012345680000"),
]

STRING_TABLE = [
    ("", '""'),
    ("é", '"é"'),
    ("日本語", '"日本語"'),
    ('a b', '"a b"'),
    ('"', '"\\""'),
    ("\\", '"\\\\"'),
    ("/", '"/"'),
    ("\n", '"\\n"'),
    ("\t", '"\\t"'),
    ("\r", '"\\r"'),
    ("\b", '"\\b"'),
    ("\f", '"\\f"'),
    ("\x00", '"\\u0000"'),
    ("\x07", '"\\u0007"'),
    ("\x0b", '"\\u000b"'),
    ("\x1f", '"\\u001f"'),
    ("\x7f", '"\x7f"'),
    ("\u2028", '"\u2028"'),
    ("\u2029", '"\u2029"'),
]


@pytest.mark.parametrize("value,expected", FLOAT_TABLE, ids=repr)
def test_floats(value: float, expected: str) -> None:
    assert js_json_dumps(value) == expected


@pytest.mark.parametrize("value,expected", INT_TABLE, ids=repr)
def test_ints(value: int, expected: str) -> None:
    assert js_json_dumps(value) == expected


@pytest.mark.parametrize("value,expected", STRING_TABLE, ids=repr)
def test_strings(value: str, expected: str) -> None:
    assert js_json_dumps(value) == expected


def test_other_scalars() -> None:
    assert js_json_dumps(True) == "true"
    assert js_json_dumps(False) == "false"
    assert js_json_dumps(None) == "null"
    # bool must not fall into the int branch
    assert js_json_dumps([True, False]) == "[\n  true,\n  false\n]"


def test_empty_containers_stay_inline() -> None:
    assert js_json_dumps([]) == "[]"
    assert js_json_dumps({}) == "{}"
    assert js_json_dumps({"a": [], "b": {}}) == '{\n  "a": [],\n  "b": {}\n}'


def test_nested_layout_two_space_indent() -> None:
    assert js_json_dumps([1, [2, 3], {"k": [4]}]) == (
        "[\n"
        "  1,\n"
        "  [\n"
        "    2,\n"
        "    3\n"
        "  ],\n"
        "  {\n"
        '    "k": [\n'
        "      4\n"
        "    ]\n"
        "  }\n"
        "]"
    )


def test_key_order_preserved_and_coerced() -> None:
    assert js_json_dumps({"z": 1, "a": 2}) == '{\n  "z": 1,\n  "a": 2\n}'
    assert js_json_dumps({1: "a"}) == '{\n  "1": "a"\n}'


def test_tuple_serialises_as_array() -> None:
    assert js_json_dumps((1, 2)) == "[\n  1,\n  2\n]"


def test_unsupported_type_raises() -> None:
    with pytest.raises(TypeError):
        js_json_dumps(object())
    with pytest.raises(TypeError):
        js_json_dumps({"a": {1, 2}})


def test_full_document_matches_node_output() -> None:
    """Exact transcript of a node ``JSON.stringify(doc, null, 2)`` run."""
    doc = {
        "a": [
            1e21,
            1.5e-7,
            -0.0,
            0.1,
            1.2345678901234568e20,
            1e-6,
            1e20,
            9007199254740992.0,
            1 / 3,
        ],
        "e": [],
        "o": {},
        "s": 'é\u001f"\\\u2028',
    }
    expected = (
        "{\n"
        '  "a": [\n'
        "    1e+21,\n"
        "    1.5e-7,\n"
        "    0,\n"
        "    0.1,\n"
        "    123456789012345680000,\n"
        "    0.000001,\n"
        "    100000000000000000000,\n"
        "    9007199254740992,\n"
        "    0.3333333333333333\n"
        "  ],\n"
        '  "e": [],\n'
        '  "o": {},\n'
        '  "s": "é\\u001f\\"\\\\\u2028"\n'
        "}"
    )
    assert js_json_dumps(doc) == expected
