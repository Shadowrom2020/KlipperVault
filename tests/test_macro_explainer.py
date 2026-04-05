from klipper_macro_explainer import (
    _COMMAND_EXPLAINERS,
    _COMMAND_GROUPS,
    _get_command_explainer,
    _explain_line,
    _parse_parameters,
    explain_macro_script,
)


def test_parse_parameters_handles_compact_values_without_false_command_tokens() -> None:
    params = _parse_parameters(["X10", "Y-2.5", "S{temp}", "PROBE_CALIBRATE", "MSG=hello", "world"])

    assert params["X"] == "10"
    assert params["Y"] == "-2.5"
    assert params["S"] == "{temp}"
    assert params["MSG"] == "hello world"
    assert "P" not in params


def test_explain_line_dispatches_known_command_from_registry() -> None:
    line = _explain_line("G1 X10 F6000", 1, set(), {})

    assert line is not None
    assert line.kind == "motion"
    assert line.confidence == "high"
    assert "motion" in line.effects
    assert line.summary == "Linear move command."
    assert "X=10" in line.details


def test_explain_line_unknown_command_uses_generic_fallback() -> None:
    line = _explain_line("MY_CUSTOM_CMD SPEED=FAST", 1, set(), {})

    assert line is not None
    assert line.kind == "unknown"
    assert line.confidence == "low"
    assert line.effects == []
    assert line.summary == "Runs command MY_CUSTOM_CMD."
    assert "SPEED=FAST" in line.details


def test_explain_macro_script_links_macro_references_with_active_first() -> None:
    available_macros = [
        {
            "macro_name": "PRINT_START",
            "display_name": "PRINT_START",
            "file_path": "base.cfg",
            "is_active": False,
            "is_deleted": False,
        },
        {
            "macro_name": "PRINT_START",
            "display_name": "PRINT_START",
            "file_path": "override.cfg",
            "is_active": True,
            "is_deleted": False,
        },
    ]
    macro = {"macro_name": "WRAPPER", "gcode": "PRINT_START"}

    result = explain_macro_script(macro, available_macros)

    assert result["has_content"] is True
    assert len(result["lines"]) == 1
    assert result["lines"][0]["kind"] == "macro_call"
    assert result["lines"][0]["confidence"] == "high"
    assert "macro_call_transfer" in result["lines"][0]["effects"]
    assert result["references"][0]["file_path"] == "override.cfg"
    assert result["confidence"]["high"] == 1
    assert result["effects"]["macro_call_transfer"] == 1
    assert result["risk_line_count"] == 0


def test_command_registry_contains_expected_coverage() -> None:
    expected = {
        "G0",
        "G1",
        "M104",
        "M109",
        "M112",
        "RESPOND",
        "SET_FAN_SPEED",
        "SAVE_CONFIG",
        "PID_CALIBRATE",
    }

    assert expected.issubset(_COMMAND_EXPLAINERS.keys())


def test_command_aliases_share_same_explainer_function() -> None:
    assert _get_command_explainer("G0") is _get_command_explainer("G1")
    assert _get_command_explainer("G2") is _get_command_explainer("G3")
    assert _get_command_explainer("M18") is _get_command_explainer("M84")


def test_command_groups_have_no_duplicate_command_tokens() -> None:
    tokens: list[str] = []
    for commands, _explainer in _COMMAND_GROUPS:
        tokens.extend(commands)

    # If this fails, a command was registered multiple times and later entries
    # would silently overwrite earlier ones in _build_command_explainers().
    assert len(tokens) == len(set(tokens))


def test_command_explainers_map_stays_in_lockstep_with_groups() -> None:
    expected_unique_tokens: set[str] = set()
    for commands, _explainer in _COMMAND_GROUPS:
        expected_unique_tokens.update(commands)

    assert len(_COMMAND_EXPLAINERS) == len(expected_unique_tokens)
    assert set(_COMMAND_EXPLAINERS) == expected_unique_tokens


def test_explain_line_disruptive_and_persistent_effects() -> None:
    line = _explain_line("SAVE_CONFIG", 1, set(), {})

    assert line is not None
    assert line.kind == "state"
    assert line.confidence == "high"
    assert "persistent_write" in line.effects
    assert "disruptive" in line.effects


def test_explain_macro_script_tracks_risk_line_count_for_disruptive_commands() -> None:
    macro = {"macro_name": "RISKY", "gcode": "SAVE_CONFIG\nM112"}

    result = explain_macro_script(macro, [])

    assert result["has_content"] is True
    assert result["effects"]["disruptive"] == 2
    assert result["risk_line_count"] == 2


def test_explain_macro_script_summary_mentions_risk_and_low_confidence() -> None:
    macro = {"macro_name": "RISKY", "gcode": "SAVE_CONFIG\nM400\nCUSTOM_CMD"}

    result = explain_macro_script(macro, [])
    summary = str(result["summary"])

    assert "Caution:" in summary
    assert "blocking wait" in summary
    assert "low explanation confidence" in summary


def test_explain_macro_script_includes_flow_sequence_metadata() -> None:
    macro = {
        "macro_name": "FLOWY",
        "gcode": "G28\nM109 S200\nG1 X10 Y10\nRESPOND MSG=done",
    }

    result = explain_macro_script(macro, [])

    assert result["flow"] == ["movement", "heat and wait", "user feedback"]
    assert "Execution flow:" in str(result["flow_summary"])


def test_explain_macro_script_concise_verbosity_shortens_details() -> None:
    macro = {"macro_name": "VERBOSE", "gcode": "G1 X10 E1 F1200"}

    detailed = explain_macro_script(macro, [], verbosity="detailed")
    concise = explain_macro_script(macro, [], verbosity="concise")

    assert detailed["verbosity"] == "detailed"
    assert concise["verbosity"] == "concise"
    assert "It also changes extrusion" in str(detailed["lines"][0]["details"])
    assert "It also changes extrusion" not in str(concise["lines"][0]["details"])


def test_template_set_line_reports_source_default_and_result_type() -> None:
    line = _explain_line("{% set z_hop = params.Z|default(30)|int %}", 1, set(), {})

    assert line is not None
    assert line.kind == "control"
    assert line.summary == "Template variable assignment."
    assert "template variable z_hop" in line.details
    assert "params.Z" in line.details
    assert "falls back to 30" in line.details
    assert "value type is int" in line.details

