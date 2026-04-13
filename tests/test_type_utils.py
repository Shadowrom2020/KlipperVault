from klipper_type_utils import to_dict_list, to_int, to_str_list, to_text


def test_to_int_handles_common_inputs() -> None:
    assert to_int(True) == 1
    assert to_int(7) == 7
    assert to_int(7.9) == 7
    assert to_int("42") == 42
    assert to_int("bad", default=5) == 5
    assert to_int(object(), default=9) == 9


def test_to_text_strips_and_defaults_empty() -> None:
    assert to_text("  hello ") == "hello"
    assert to_text(123) == "123"
    assert to_text(None) == ""


def test_to_dict_list_filters_non_dict_items() -> None:
    raw = [{"a": 1}, "bad", 3, {"b": 2}]
    assert to_dict_list(raw) == [{"a": 1}, {"b": 2}]
    assert to_dict_list("not-a-list") == []


def test_to_str_list_filters_non_string_items() -> None:
    raw = ["a", 1, "b", None]
    assert to_str_list(raw) == ["a", "b"]
    assert to_str_list({"x": "y"}) == []
