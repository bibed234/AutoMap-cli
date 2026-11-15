import pytest

from map.auto_map import _numeric_extrema


@pytest.mark.parametrize(
    "values, expected",
    [
        ({"a": "9", "b": "100"}, ("b", "a")),
        ({"a": 9, "b": "100", "c": -2.5}, ("b", "c")),
        ({"a": "1.25", "b": "-0.5"}, ("a", "b")),
        ({"a": None, "b": "unknown", "c": 0}, ("c", "c")),
        ({"a": "NaN", "b": "Infinity", "c": True}, None),
        ({"ukraine": "2M", "poland": 100000}, ("ukraine", "poland")),
        ({"a": "1B", "b": "2.5M", "c": "100k"}, ("a", "c")),
        ({"a": "1,234", "b": "2 345", "c": "0.1"}, ("b", "c")),
        ({}, None),
    ],
)
def test_numeric_extrema(values, expected):
    assert _numeric_extrema(values) == expected
