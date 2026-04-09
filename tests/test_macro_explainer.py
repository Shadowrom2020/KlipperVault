import json
from pathlib import Path

from klipper_macro_explainer import (
    _COMMAND_EXPLAINERS,
    _COMMAND_GROUPS,
    _get_command_explainer,
    _explain_line,
    load_command_pack,
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


def test_explain_line_ignores_full_line_semicolon_comments() -> None:
    line = _explain_line("; this is only a comment", 1, set(), {})

    assert line is None


def test_explain_line_ignores_inline_semicolon_comments() -> None:
    line = _explain_line("G1 X10 Y20 ; move to position", 1, set(), {})

    assert line is not None
    assert line.kind == "motion"
    assert "X=10" in line.details
    assert "Y=20" in line.details


def test_explain_line_strips_semicolon_tail_before_template_detection() -> None:
    line = _explain_line(
        "{% set Z_axis_was_homed = true %} ; Set Z_axis_was_homed to true if Z is already homed",
        1,
        set(),
        {},
    )

    assert line is not None
    assert line.kind == "control"
    assert line.confidence == "high"
    assert line.summary == "Template variable assignment."


def test_explain_line_template_inline_expression_is_high_confidence() -> None:
    line = _explain_line("RESPOND MSG={{ printer.toolhead.position.x }}", 1, set(), {})

    assert line is not None
    assert line.kind == "control"
    assert line.summary == "Template expression inside g-code."
    assert line.confidence == "high"


def test_explain_line_template_if_block_is_high_confidence() -> None:
    line = _explain_line("{% if printer.idle_timeout.state == 'Printing' %}", 1, set(), {})

    assert line is not None
    assert line.kind == "control"
    assert line.summary == "Conditional branch begins."
    assert line.confidence == "high"


def test_explain_line_ignores_inline_hash_comments() -> None:
    line = _explain_line("G1 X5 Y6 # planner note", 1, set(), {})

    assert line is not None
    assert line.kind == "motion"
    assert "X=5" in line.details
    assert "Y=6" in line.details


def test_inline_comment_markers_inside_quotes_are_preserved() -> None:
    line = _explain_line('RESPOND MSG="value # keep"', 1, set(), {})

    assert line is not None
    assert line.kind == "message"
    assert "value # keep" in line.details


def test_explain_line_dispatches_known_command_from_registry() -> None:
    line = _explain_line("G1 X10 F6000", 1, set(), {})

    assert line is not None
    assert line.kind == "motion"
    assert line.confidence == "high"
    assert "motion" in line.effects
    assert line.summary == "Linear move command."
    assert "X=10" in line.details


def test_explain_line_force_move_has_specific_explanation() -> None:
    line = _explain_line("FORCE_MOVE STEPPER=stepper_z DISTANCE={z_clearance} VELOCITY=10", 1, set(), {})

    assert line is not None
    assert line.kind == "motion"
    assert line.summary == "Forces direct stepper movement."
    assert line.confidence == "high"
    assert "disruptive" in line.effects
    assert "stepper_z" in line.details
    assert "{z_clearance}" in line.details
    assert "velocity 10" in line.details


def test_explain_line_set_kinematic_position_has_specific_explanation() -> None:
    line = _explain_line("SET_KINEMATIC_POSITION X=0 Y=0 Z=10 SET_HOMED=XYZ", 1, set(), {})

    assert line is not None
    assert line.kind == "state"
    assert line.summary == "Overrides internal kinematic coordinates."
    assert line.confidence == "high"
    assert "X=0" in line.details
    assert "Y=0" in line.details
    assert "Z=10" in line.details
    assert "without physically moving" in line.details
    assert "SET_HOMED=XYZ" in line.details


def test_explain_line_set_tmc_current_has_specific_explanation() -> None:
    line = _explain_line("SET_TMC_CURRENT STEPPER=stepper_z CURRENT=0.75 HOLDCURRENT=0.50", 1, set(), {})

    assert line is not None
    assert line.kind == "state"
    assert line.summary == "Adjusts TMC motor current."
    assert line.confidence == "high"
    assert "stepper_z" in line.details
    assert "run current 0.75" in line.details
    assert "hold current 0.50" in line.details


def test_explain_line_set_stepper_enable_has_specific_explanation() -> None:
    line = _explain_line("SET_STEPPER_ENABLE STEPPER=extruder enable=0", 1, set(), {})

    assert line is not None
    assert line.kind == "state"
    assert line.summary == "Disables a stepper driver."
    assert line.confidence == "high"
    assert "extruder" in line.details
    assert "stops holding torque" in line.details


def test_explain_line_manual_stepper_has_specific_explanation() -> None:
    line = _explain_line("MANUAL_STEPPER STEPPER=stepper_z MOVE=10 SPEED=5", 1, set(), {})

    assert line is not None
    assert line.kind == "motion"
    assert line.summary == "Moves a manual stepper."
    assert line.confidence == "high"
    assert "stepper_z" in line.details
    assert "move to 10" in line.details
    assert "speed is 5" in line.details


def test_explain_line_sync_extruder_motion_has_specific_explanation() -> None:
    line = _explain_line("SYNC_EXTRUDER_MOTION EXTRUDER=extruder MOTION_QUEUE=extruder1", 1, set(), {})

    assert line is not None
    assert line.kind == "state"
    assert line.summary == "Reassigns extruder motion synchronization."
    assert line.confidence == "high"
    assert "extruder" in line.details
    assert "extruder1" in line.details
    assert "switching toolheads" in line.details


def test_explain_line_set_stepper_phase_has_specific_explanation() -> None:
    line = _explain_line("SET_STEPPER_PHASE STEPPER=stepper_x PHASE=12", 1, set(), {})

    assert line is not None
    assert line.kind == "state"
    assert line.summary == "Sets stepper phase reference."
    assert line.confidence == "high"
    assert "stepper_x" in line.details
    assert "12" in line.details
    assert "electrical phase" in line.details


def test_explain_line_stepper_buzz_has_specific_explanation() -> None:
    line = _explain_line("STEPPER_BUZZ STEPPER=stepper_y", 1, set(), {})

    assert line is not None
    assert line.kind == "motion"
    assert line.summary == "Runs stepper buzz test."
    assert line.confidence == "high"
    assert "stepper_y" in line.details
    assert "back and forth" in line.details


def test_explain_line_dump_tmc_has_specific_explanation() -> None:
    line = _explain_line("DUMP_TMC STEPPER=stepper_x", 1, set(), {})

    assert line is not None
    assert line.kind == "message"
    assert line.summary == "Prints TMC driver diagnostics."
    assert line.confidence == "high"
    assert "stepper_x" in line.details
    assert "console" in line.details


def test_explain_line_init_tmc_has_specific_explanation() -> None:
    line = _explain_line("INIT_TMC STEPPER=stepper_z", 1, set(), {})

    assert line is not None
    assert line.kind == "state"
    assert line.summary == "Reinitializes a TMC driver."
    assert line.confidence == "high"
    assert "stepper_z" in line.details
    assert "re-send configuration" in line.details


def test_explain_line_set_tmc_field_has_specific_explanation() -> None:
    line = _explain_line("SET_TMC_FIELD STEPPER=stepper_x FIELD=SGTHRS VALUE=120", 1, set(), {})

    assert line is not None
    assert line.kind == "state"
    assert line.summary == "Sets a low-level TMC register field."
    assert line.confidence == "high"
    assert "SGTHRS" in line.details
    assert "120" in line.details
    assert "stepper_x" in line.details


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


def test_explain_macro_script_includes_rename_existing_even_without_gcode() -> None:
    macro = {"macro_name": "PAUSE", "rename_existing": "PAUSE_BASE", "gcode": ""}

    result = explain_macro_script(macro, [])

    assert result["has_content"] is True
    assert len(result["lines"]) == 1
    line = result["lines"][0]
    assert line["summary"] == "Preserves replaced macro under a new name."
    assert line["kind"] == "state"
    assert line["confidence"] == "high"
    assert "Klipper standard macro/behavior" in str(line["details"])


def test_explain_macro_script_rename_existing_links_prior_definition_when_present() -> None:
    available_macros = [
        {
            "macro_name": "PAUSE",
            "display_name": "PAUSE",
            "file_path": "base.cfg",
            "is_active": False,
            "is_deleted": False,
        },
        {
            "macro_name": "PAUSE",
            "display_name": "PAUSE",
            "file_path": "override.cfg",
            "is_active": True,
            "is_deleted": False,
        },
    ]
    macro = {
        "macro_name": "PAUSE",
        "file_path": "override.cfg",
        "rename_existing": "PAUSE_BASE",
        "gcode": "RESPOND MSG=ok",
    }

    result = explain_macro_script(macro, available_macros)

    assert result["has_content"] is True
    assert len(result["lines"]) == 2
    rename_line = result["lines"][0]
    assert rename_line["summary"] == "Preserves replaced macro under a new name."
    assert rename_line["references"][0]["file_path"] == "base.cfg"
    assert "base.cfg" in str(rename_line["details"])


def test_explain_macro_script_recognizes_renamed_standard_macro_alias_calls() -> None:
    macro = {
        "macro_name": "G28",
        "rename_existing": "G28.1",
        "gcode": "G28.1",
    }

    result = explain_macro_script(macro, [])

    assert result["has_content"] is True
    assert len(result["lines"]) == 2

    rename_line = result["lines"][0]
    call_line = result["lines"][1]

    assert rename_line["summary"] == "Preserves replaced macro under a new name."
    assert "G28.1" in str(rename_line["details"])
    assert "Klipper standard macro/behavior" in str(rename_line["details"])

    assert call_line["kind"] == "macro_call"
    assert call_line["summary"] == "Calls renamed macro G28.1."
    assert "previous implementation of G28" in str(call_line["details"])
    assert "Klipper standard macro/behavior" in str(call_line["details"])


def test_command_registry_contains_expected_coverage() -> None:
    expected = {
        "G0",
        "G1",
        "M104",
        "M109",
        "M112",
        "DUMP_TMC",
        "INIT_TMC",
        "MANUAL_STEPPER",
        "RESPOND",
        "SET_FAN_SPEED",
        "SET_KINEMATIC_POSITION",
        "SET_STEPPER_PHASE",
        "SET_STEPPER_ENABLE",
        "SET_TMC_CURRENT",
        "SET_TMC_FIELD",
        "STEPPER_BUZZ",
        "SYNC_EXTRUDER_MOTION",
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


def test_command_pack_alias_maps_custom_command_to_existing_behavior() -> None:
    macro = {"macro_name": "PACKED", "gcode": "MY_HOME X"}
    command_pack = {
        "MY_HOME": {
            "alias_of": "G28",
            "effects": ["motion", "printer_state_change"],
            "confidence": "high",
        }
    }

    result = explain_macro_script(macro, [], command_pack=command_pack)
    line = result["lines"][0]

    assert line["kind"] == "motion"
    assert line["summary"] == "Homes the printer."
    assert "printer_state_change" in line["effects"]


def test_command_pack_allows_custom_static_command_description() -> None:
    macro = {"macro_name": "PACKED", "gcode": "CUSTOM_BEEP"}
    command_pack = {
        "CUSTOM_BEEP": {
            "kind": "message",
            "summary": "Triggers a custom buzzer pattern.",
            "details": "This runs a user-defined buzzer macro for completion feedback.",
            "confidence": "high",
            "effects": ["user_feedback"],
        }
    }

    result = explain_macro_script(macro, [], command_pack=command_pack)
    line = result["lines"][0]

    assert line["kind"] == "message"
    assert line["summary"] == "Triggers a custom buzzer pattern."
    assert "completion feedback" in line["details"]
    assert line["confidence"] == "high"
    assert line["effects"] == ["user_feedback"]


def test_template_set_line_reports_source_default_and_result_type() -> None:
    line = _explain_line("{% set z_hop = params.Z|default(30)|int %}", 1, set(), {})

    assert line is not None
    assert line.kind == "control"
    assert line.summary == "Template variable assignment."
    assert "template variable z_hop" in line.details
    assert "params.Z" in line.details
    assert "falls back to 30" in line.details
    assert "value type is int" in line.details


def test_explain_macro_script_multiline_jinja_set_is_single_confident_control_line() -> None:
    macro = {
        "macro_name": "MULTI",
        "gcode": (
            "{% set home_all = ('X' in rawparams.upper() and 'Y' in rawparams.upper() and 'Z' in rawparams.upper()) or\n"
            "                  ('X' not in rawparams.upper() and 'Y' not in rawparams.upper() and 'Z' not in rawparams.upper()) %}"
        ),
    }

    result = explain_macro_script(macro, [])

    assert result["has_content"] is True
    assert len(result["lines"]) == 1
    line = result["lines"][0]
    assert line["kind"] == "control"
    assert line["summary"] == "Template variable assignment."
    assert line["confidence"] == "high"
    assert "template variable home_all" in str(line["details"])
    assert "combines multiple checks" in str(line["details"])


def test_explain_macro_script_multiline_jinja_set_preserves_following_line_numbers() -> None:
    macro = {
        "macro_name": "MULTI",
        "gcode": (
            "{% set home_all = ('X' in rawparams.upper()) or\n"
            "                  ('Y' in rawparams.upper()) %}\n"
            "G28"
        ),
    }

    result = explain_macro_script(macro, [])

    assert result["has_content"] is True
    assert len(result["lines"]) == 2
    assert result["lines"][0]["line_number"] == 1
    assert result["lines"][0]["kind"] == "control"
    assert result["lines"][1]["line_number"] == 3
    assert result["lines"][1]["kind"] == "motion"


def test_load_command_pack_supports_commands_wrapper_and_filters_invalid_entries(tmp_path: Path) -> None:
    pack_path = tmp_path / "command_pack.json"
    pack_path.write_text(
        json.dumps(
            {
                "commands": {
                    "MY_HOME": {"alias_of": "g28", "confidence": "high"},
                    "CUSTOM_BEEP": {
                        "kind": "message",
                        "summary": "Beeps",
                        "details": "Play a custom tone.",
                        "effects": ["user_feedback", 1, ""],
                    },
                    "BROKEN": "not a dict",
                }
            }
        ),
        encoding="utf-8",
    )

    pack = load_command_pack(pack_path)

    assert set(pack.keys()) == {"MY_HOME", "CUSTOM_BEEP"}
    assert pack["MY_HOME"].get("alias_of") == "G28"
    assert pack["CUSTOM_BEEP"].get("effects") == ["user_feedback"]

