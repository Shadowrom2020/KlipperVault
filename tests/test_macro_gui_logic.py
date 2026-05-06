from klipper_macro_gui_logic import (
    duplicate_count_from_stats,
    duplicate_names_for_macros,
    filter_macros,
    find_active_override,
    next_sort_order,
    selected_or_first_macro,
    sort_macros,
)


def test_duplicate_detection_filtering_and_sorting() -> None:
    macros = [
        {
            "macro_name": "PRINT_START",
            "display_name": "PRINT_START",
            "file_path": "b.cfg",
            "line_number": "20",
            "is_active": True,
            "is_deleted": False,
        },
        {
            "macro_name": "PRINT_START",
            "display_name": "print_start",
            "file_path": "a.cfg",
            "line_number": 5,
            "is_active": False,
            "is_deleted": False,
        },
        {
            "macro_name": "CLEAN_NOZZLE",
            "display_name": "CLEAN_NOZZLE",
            "file_path": "c.cfg",
            "line_number": 3,
            "is_active": True,
            "is_deleted": True,
        },
    ]

    duplicates = duplicate_names_for_macros(macros)
    duplicate_only = filter_macros(macros, "print", True, "all", duplicates)
    active_only = filter_macros(macros, "", False, "active", duplicates)
    alpha_asc = sort_macros(macros[:2], "load_order")
    alpha_desc = sort_macros(macros[:2], "alpha_desc")

    assert duplicates == set()
    assert duplicate_only == []
    assert [row["file_path"] for row in active_only] == ["b.cfg", "c.cfg"]
    assert [row["file_path"] for row in alpha_asc] == ["b.cfg", "a.cfg"]
    assert [row["file_path"] for row in alpha_desc] == ["b.cfg", "a.cfg"]
    assert duplicate_count_from_stats({"total_macros": "5", "distinct_macro_names": 3}) == 0
    assert next_sort_order("alpha_asc") == "alpha_desc"


def test_selection_and_active_override_resolution() -> None:
    visible_macros = [
        {
            "macro_name": "HELLO",
            "display_name": "HELLO",
            "file_path": "base.cfg",
            "is_active": False,
        },
        {
            "macro_name": "HELLO",
            "display_name": "HELLO",
            "file_path": "override.cfg",
            "is_active": True,
        },
    ]

    selected = selected_or_first_macro(visible_macros, "base.cfg::HELLO")
    fallback = selected_or_first_macro(visible_macros, "missing::HELLO")
    active_override = find_active_override(visible_macros[0], visible_macros)

    assert selected == visible_macros[0]
    assert fallback == visible_macros[0]
    assert active_override == visible_macros[1]


def test_unknown_sort_order_falls_back_to_alpha_asc() -> None:
    """Verify that unsupported sort orders fall back to alphabetical sorting."""
    macros = [
        {
            "macro_name": "Z_MACRO",
            "file_path": "z_last.cfg",
            "line_number": 10,
            "load_order_index": 0,
        },
        {
            "macro_name": "A_MACRO",
            "file_path": "a_first.cfg",
            "line_number": 1,
            "load_order_index": 1,
        },
        {
            "macro_name": "M_MACRO",
            "file_path": "m_middle.cfg",
            "line_number": 5,
            "load_order_index": 2,
        },
    ]

    sorted_macros = sort_macros(macros, "load_order")
    assert [row["file_path"] for row in sorted_macros] == ["a_first.cfg", "m_middle.cfg", "z_last.cfg"]
