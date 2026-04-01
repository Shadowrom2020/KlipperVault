#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Explain Klipper macro gcode in user-facing language."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import re


@dataclass(frozen=True)
class MacroReference:
    """One macro target that can be opened from an explanation."""

    macro_name: str
    display_name: str
    file_path: str
    is_active: bool
    is_deleted: bool


@dataclass(frozen=True)
class ExplanationLine:
    """Explanation for one source line inside a macro body."""

    line_number: int
    text: str
    kind: str
    summary: str
    details: str
    references: list[dict[str, object]]


def build_macro_reference_index(macros: list[dict[str, object]]) -> dict[str, list[MacroReference]]:
    """Build lookup of macro name to openable macro targets."""
    grouped: dict[str, list[MacroReference]] = {}
    for macro in macros:
        macro_name = str(macro.get("macro_name", "")).strip()
        display_name = str(
            macro.get("display_name")
            or macro.get("runtime_macro_name")
            or macro_name
        ).strip()
        file_path = str(macro.get("file_path", "")).strip()
        if not macro_name or not file_path:
            continue

        reference = MacroReference(
            macro_name=macro_name,
            display_name=display_name,
            file_path=file_path,
            is_active=bool(macro.get("is_active", False)),
            is_deleted=bool(macro.get("is_deleted", False)),
        )

        callable_names = {macro_name.lower(), display_name.lower()}
        for callable_name in callable_names:
            grouped.setdefault(callable_name, []).append(reference)

    for refs in grouped.values():
        refs.sort(key=lambda ref: (not ref.is_active, ref.is_deleted, ref.file_path))
    return grouped


def explain_macro_script(
    macro: dict[str, object] | None,
    available_macros: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Explain a macro body in plain language and discover macro references."""
    if macro is None:
        return {
            "summary": "Select a macro to see an explanation.",
            "lines": [],
            "references": [],
            "has_content": False,
        }

    gcode_text = str(macro.get("gcode") or "")
    body_lines = gcode_text.splitlines()
    if not body_lines:
        return {
            "summary": "This macro does not currently contain any g-code lines.",
            "lines": [],
            "references": [],
            "has_content": False,
        }

    current_macro_names = {
        str(macro.get("macro_name", "")).strip().lower(),
        str(macro.get("display_name") or macro.get("runtime_macro_name") or "").strip().lower(),
    }
    current_macro_names.discard("")
    reference_index = build_macro_reference_index(available_macros or [])
    categories: Counter[str] = Counter()
    references: dict[tuple[str, str], dict[str, object]] = {}
    explanation_lines: list[dict[str, object]] = []
    explained_line_count = 0

    for line_number, raw_line in enumerate(body_lines, start=1):
        entry = _explain_line(raw_line, line_number, current_macro_names, reference_index)
        if entry is None:
            continue
        explained_line_count += 1
        categories[entry.kind] += 1
        explanation_lines.append(asdict(entry))
        for reference in entry.references:
            key = (str(reference["macro_name"]), str(reference["file_path"]))
            references[key] = reference

    if not explanation_lines:
        return {
            "summary": "This macro does not contain any executable lines that need explanation.",
            "lines": [],
            "references": [],
            "has_content": False,
        }

    return {
        "summary": _build_summary(categories, references, explained_line_count),
        "lines": explanation_lines,
        "references": list(references.values()),
        "has_content": True,
    }


def _build_summary(
    categories: Counter[str],
    references: dict[tuple[str, str], dict[str, object]],
    line_count: int,
) -> str:
    """Build compact top-level description for one macro body."""
    parts = [f"This macro contains {line_count} g-code line(s)."]

    focus_parts: list[str] = []
    if categories.get("motion"):
        focus_parts.append("movement")
    if categories.get("temperature"):
        focus_parts.append("temperature control")
    if categories.get("state"):
        focus_parts.append("printer state changes")
    if categories.get("macro_call"):
        focus_parts.append("macro-to-macro calls")
    if categories.get("control"):
        focus_parts.append("template-driven branching")
    if categories.get("message"):
        focus_parts.append("user feedback")

    if focus_parts:
        parts.append("It mainly performs " + ", ".join(focus_parts[:-1] + [focus_parts[-1]]) + ".")

    if references:
        parts.append(f"It references {len(references)} other macro(s) that can be opened from this panel.")

    if categories.get("unknown"):
        parts.append("Some lines use commands that are not yet described in detail, so they are shown with a generic explanation.")

    return " ".join(parts)


def _explain_line(
    raw_line: str,
    line_number: int,
    current_macro_names: set[str],
    reference_index: dict[str, list[MacroReference]],
) -> ExplanationLine | None:
    """Explain one line of macro body text."""
    stripped = raw_line.strip()

    if not stripped:
        return None

    if stripped.startswith("#") or stripped.startswith(";"):
        return None

    if stripped.startswith("{%") and stripped.endswith("%}"):
        return _explain_template_block(raw_line, line_number)

    if "{{" in stripped and "}}" in stripped:
        return ExplanationLine(
            line_number=line_number,
            text=raw_line,
            kind="control",
            summary="Template expression inside g-code.",
            details="This line injects values computed from macro parameters or live printer state before the command runs.",
            references=[],
        )

    tokens = stripped.split()
    command = tokens[0]
    upper_command = command.upper()
    params = _parse_parameters(tokens[1:])

    macro_refs = _resolve_macro_references(upper_command, current_macro_names, reference_index)
    if macro_refs:
        reference_note = "Active definition" if bool(macro_refs[0].get("is_active", False)) else "Stored definition"
        return ExplanationLine(
            line_number=line_number,
            text=raw_line,
            kind="macro_call",
            summary=f"Calls macro {upper_command}.",
            details=(
                f"This line transfers control to macro {upper_command}. {reference_note} is available in "
                f"{macro_refs[0]['file_path']}, and you can open it from the link button below."
            ),
            references=macro_refs,
        )

    command_map = {
        "G0": _explain_motion_command,
        "G1": _explain_motion_command,
        "G2": _explain_arc_move,
        "G3": _explain_arc_move,
        "G4": _explain_dwell,
        "G10": _explain_firmware_retraction,
        "G11": _explain_firmware_unretraction,
        "G17": _explain_arc_plane_select,
        "G18": _explain_arc_plane_select,
        "G19": _explain_arc_plane_select,
        "G28": _explain_home_command,
        "G90": _explain_absolute_positioning,
        "G91": _explain_relative_positioning,
        "G92": _explain_set_position,
        "M18": _explain_disable_steppers,
        "M20": _explain_sd_list,
        "M21": _explain_sd_init,
        "M23": _explain_sd_select,
        "M24": _explain_sd_start_or_resume,
        "M25": _explain_sd_pause,
        "M26": _explain_sd_set_position,
        "M27": _explain_sd_status,
        "M73": _explain_set_build_percentage,
        "M82": _explain_absolute_extrusion,
        "M83": _explain_relative_extrusion,
        "M84": _explain_disable_steppers,
        "M105": _explain_query_nozzle_temperature,
        "M104": _explain_nozzle_temperature,
        "M109": _explain_nozzle_temperature_wait,
        "M112": _explain_emergency_stop,
        "M114": _explain_get_current_position,
        "M115": _explain_firmware_version,
        "M118": _explain_host_echo,
        "M119": _explain_query_endstops_legacy,
        "M140": _explain_bed_temperature,
        "M190": _explain_bed_temperature_wait,
        "M204": _explain_acceleration,
        "M220": _explain_speed_factor,
        "M221": _explain_extrude_factor,
        "M400": _explain_wait_moves,
        "M106": _explain_fan_command,
        "M107": _explain_fan_off,
        "M117": _explain_display_message,
        "RESPOND": _explain_respond,
        "SET_DISPLAY_TEXT": _explain_set_display_text,
        "SET_GCODE_VARIABLE": _explain_set_gcode_variable,
        "SAVE_GCODE_STATE": _explain_save_gcode_state,
        "RESTORE_GCODE_STATE": _explain_restore_gcode_state,
        "SET_GCODE_OFFSET": _explain_set_gcode_offset,
        "GET_POSITION": _explain_get_position,
        "TEMPERATURE_WAIT": _explain_temperature_wait,
        "TURN_OFF_HEATERS": _explain_turn_off_heaters,
        "SET_HEATER_TEMPERATURE": _explain_set_heater_temperature,
        "UPDATE_DELAYED_GCODE": _explain_update_delayed_gcode,
        "BED_MESH_CALIBRATE": _explain_bed_mesh_calibrate,
        "BED_MESH_CLEAR": _explain_bed_mesh_clear,
        "BED_MESH_OUTPUT": _explain_bed_mesh_output,
        "BED_MESH_MAP": _explain_bed_mesh_map,
        "BED_MESH_PROFILE": _explain_bed_mesh_profile,
        "BED_MESH_OFFSET": _explain_bed_mesh_offset,
        "BED_TILT_CALIBRATE": _explain_bed_tilt_calibrate,
        "BED_SCREWS_ADJUST": _explain_bed_screws_adjust,
        "QUAD_GANTRY_LEVEL": _explain_quad_gantry_level,
        "Z_TILT_ADJUST": _explain_z_tilt_adjust,
        "SCREWS_TILT_CALCULATE": _explain_screws_tilt_calculate,
        "PROBE": _explain_probe,
        "PROBE_ACCURACY": _explain_probe_accuracy,
        "PROBE_CALIBRATE": _explain_probe_calibrate,
        "QUERY_PROBE": _explain_query_probe,
        "QUERY_ENDSTOPS": _explain_query_endstops,
        "SET_FAN_SPEED": _explain_set_fan_speed,
        "SET_PIN": _explain_set_pin,
        "SET_LED": _explain_set_led,
        "SET_LED_TEMPLATE": _explain_set_led_template,
        "SET_SERVO": _explain_set_servo,
        "SET_INPUT_SHAPER": _explain_set_input_shaper,
        "SET_VELOCITY_LIMIT": _explain_set_velocity_limit,
        "SET_PRESSURE_ADVANCE": _explain_set_pressure_advance,
        "SAVE_VARIABLE": _explain_save_variable,
        "SAVE_CONFIG": _explain_save_config,
        "SET_IDLE_TIMEOUT": _explain_set_idle_timeout,
        "SET_TEMPERATURE_FAN_TARGET": _explain_set_temperature_fan_target,
        "SET_PRINT_STATS_INFO": _explain_set_print_stats_info,
        "SDCARD_PRINT_FILE": _explain_sdcard_print_file,
        "SDCARD_RESET_FILE": _explain_sdcard_reset_file,
        "RESTART": _explain_restart,
        "FIRMWARE_RESTART": _explain_firmware_restart,
        "STATUS": _explain_status,
        "HELP": _explain_help,
        "PAUSE": _explain_pause,
        "RESUME": _explain_resume,
        "CLEAR_PAUSE": _explain_clear_pause,
        "CANCEL_PRINT": _explain_cancel_print,
        "PID_CALIBRATE": _explain_pid_calibrate,
    }
    explain = command_map.get(upper_command, _explain_generic_command)
    kind, summary, details = explain(upper_command, params, stripped)
    return ExplanationLine(
        line_number=line_number,
        text=raw_line,
        kind=kind,
        summary=summary,
        details=details,
        references=[],
    )


def _parse_parameters(tokens: list[str]) -> dict[str, str]:
    """Parse g-code and Klipper-style command parameters."""
    params: dict[str, str] = {}
    current_key: str | None = None
    for token in tokens:
        if "=" in token:
            key, value = token.split("=", 1)
            current_key = key.upper()
            params[current_key] = value
            continue

        axis_match = re.fullmatch(r"([A-Za-z])(.*)", token)
        if axis_match and axis_match.group(2):
            candidate_value = axis_match.group(2)
            # Only treat compact one-letter parameters (e.g. X10, E-2, S{temp})
            # as key/value pairs. Plain words like PROBE_CALIBRATE should remain
            # command text, not synthetic parameters.
            if re.match(r"^[+\-0-9.{]", candidate_value):
                current_key = axis_match.group(1).upper()
                params[current_key] = candidate_value
                continue

            current_key = None
            continue

        if current_key is not None:
            params[current_key] = f"{params[current_key]} {token}".strip()
    return params


def _resolve_macro_references(
    command_name: str,
    current_macro_names: set[str],
    reference_index: dict[str, list[MacroReference]],
) -> list[dict[str, object]]:
    """Return openable macro targets for a likely macro call."""
    if command_name.lower() in current_macro_names:
        return []
    refs = reference_index.get(command_name.lower(), [])
    return [asdict(ref) for ref in refs[:3]]


def _explain_template_block(raw_line: str, line_number: int) -> ExplanationLine:
    """Explain Jinja control directives embedded in a macro."""
    stripped = raw_line.strip()[2:-2].strip()
    normalized = stripped.lower()

    if normalized.startswith("if "):
        condition = stripped[3:].strip()
        summary = "Conditional branch begins."
        details = (
            "The following lines only execute when this condition evaluates to true at runtime. "
            + _describe_template_expression(condition)
        )
    elif normalized.startswith("elif "):
        condition = stripped[5:].strip()
        summary = "Conditional fallback branch."
        details = (
            "This branch executes only if earlier conditions failed and this one matches. "
            + _describe_template_expression(condition)
        )
    elif normalized == "else":
        summary = "Default branch."
        details = "These lines execute when earlier conditional branches do not match."
    elif normalized == "endif":
        summary = "Conditional block ends."
        details = "Execution returns to normal flow after the template condition finishes."
    elif normalized.startswith("for "):
        loop_target, loop_source = _parse_template_for_clause(stripped)
        summary = "Loop begins."
        details = "The following block repeats for each item produced by this template expression."
        if loop_target and loop_source:
            details += (
                f" It assigns each item to {loop_target} while iterating over {loop_source}. "
                + _describe_template_expression(loop_source)
            )
    elif normalized == "endfor":
        summary = "Loop ends."
        details = "Execution leaves the repeated block and continues with the next line."
    elif normalized.startswith("set "):
        variable_name, expression = _parse_template_set_clause(stripped)
        summary = "Template variable assignment."
        details = "This line computes and stores a template value that later lines can reuse."
        if variable_name and expression:
            details += f" It assigns {variable_name} from {expression}. " + _describe_template_expression(expression)
    else:
        summary = "Template control directive."
        details = (
            "This line controls how Klipper renders the macro before the printer executes the resulting g-code. "
            + _describe_template_expression(stripped)
        )

    return ExplanationLine(
        line_number=line_number,
        text=raw_line,
        kind="control",
        summary=summary,
        details=details,
        references=[],
    )


def _format_params(params: dict[str, str]) -> str:
    """Format parameter mapping for generic explanations."""
    if not params:
        return "without explicit parameters"
    return ", ".join(f"{key}={value}" for key, value in params.items())


def _parse_template_set_clause(clause: str) -> tuple[str, str]:
    """Split a Jinja set directive into variable and expression."""
    match = re.match(r"set\s+(.+?)\s*=\s*(.+)", clause, flags=re.IGNORECASE)
    if not match:
        return "", ""
    return match.group(1).strip(), match.group(2).strip()


def _parse_template_for_clause(clause: str) -> tuple[str, str]:
    """Split a Jinja for directive into loop variable and iterable."""
    match = re.match(r"for\s+(.+?)\s+in\s+(.+)", clause, flags=re.IGNORECASE)
    if not match:
        return "", ""
    return match.group(1).strip(), match.group(2).strip()


def _describe_template_expression(expression: str) -> str:
    """Generate a more concrete explanation for a Jinja expression."""
    expr = str(expression or "").strip()
    if not expr:
        return ""

    notes: list[str] = []
    parameter_names = sorted(set(re.findall(r"\bparams\.([A-Za-z_][A-Za-z0-9_]*)", expr)))
    printer_objects = sorted(set(re.findall(r"\bprinter\.([A-Za-z_][A-Za-z0-9_\.]*)", expr)))
    filters = sorted(set(re.findall(r"\|\s*([A-Za-z_][A-Za-z0-9_]*)", expr)))

    if parameter_names:
        joined = ", ".join(parameter_names)
        notes.append(f"It depends on macro parameter(s) {joined}")

    if printer_objects:
        joined = ", ".join(printer_objects)
        notes.append(f"It reads live printer state from {joined}")

    if filters:
        joined = ", ".join(filters)
        notes.append(f"It applies template filter(s) {joined} to normalize or convert values")

    if "default(" in expr:
        notes.append("It includes fallback handling in case an input value is missing")

    if any(operator in expr for operator in ("==", "!=", ">=", "<=", " > ", " < ", " not in ", " in ")):
        notes.append("It compares values to decide whether the guarded block should run")

    if " and " in expr or " or " in expr:
        notes.append("It combines multiple checks into a single decision")

    if not notes:
        return f"This expression is evaluated as `{expr}` before Klipper renders the final command text."

    sentence = "; ".join(notes)
    return f"This expression is evaluated as `{expr}` before Klipper renders the final command text. {sentence}."


def _explain_motion_command(command: str, params: dict[str, str], _: str) -> tuple[str, str, str]:
    axes = [f"{axis}={params[axis]}" for axis in ("X", "Y", "Z") if axis in params]
    extrusion = f"E={params['E']}" if "E" in params else ""
    feedrate = f"feedrate F={params['F']}" if "F" in params else "current feedrate"
    moved = ", ".join(axes) if axes else "the current toolhead position"
    details = f"This performs a coordinated move toward {moved} using {feedrate}."
    if extrusion:
        details += f" It also changes extrusion by {extrusion}."
    return "motion", "Linear move command.", details


def _explain_arc_move(command: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    direction = "clockwise" if command == "G2" else "counter-clockwise"
    feedrate = f"F={params['F']}" if "F" in params else "the current feedrate"
    plane_offsets = ", ".join(f"{axis}={params[axis]}" for axis in ("I", "J", "K") if axis in params)
    detail = f"This performs a {direction} arc move using {feedrate}."
    if plane_offsets:
        detail += f" Arc center offset parameters: {plane_offsets}."
    return "motion", f"Arc move ({command}).", detail


def _explain_dwell(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    delay = params.get("P", "an unspecified time")
    return "state", "Pauses command processing briefly.", f"This dwell waits for {delay} millisecond(s) before continuing."


def _explain_firmware_retraction(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "motion", "Runs firmware retraction.", "This retracts filament using Klipper firmware-retraction settings instead of a raw E move."


def _explain_firmware_unretraction(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "motion", "Runs firmware unretraction.", "This restores filament after a firmware retraction using configured unretract behavior."


def _explain_arc_plane_select(command: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    planes = {"G17": "XY", "G18": "XZ", "G19": "YZ"}
    plane = planes.get(command, "active")
    return "state", f"Selects arc plane {plane}.", "This sets which coordinate plane Klipper uses for subsequent G2/G3 arc interpolation."


def _explain_home_command(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    axes = [axis for axis in ("X", "Y", "Z") if axis in params]
    target = ", ".join(axes) if axes else "all configured axes"
    return "motion", "Homes the printer.", f"This command moves {target} to their reference endstops so Klipper knows where the machine is."


def _explain_disable_steppers(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Disables stepper motors.", "This turns motors off so axes may no longer hold position until re-enabled by movement or explicit commands."


def _explain_wait_moves(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Waits for queued moves to finish.", "This blocks execution until all already queued motion has completed."


def _explain_speed_factor(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    factor = params.get("S", "unspecified")
    return "state", "Sets speed override factor.", f"This sets the motion speed override to {factor}% (M220 scale)."


def _explain_extrude_factor(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    factor = params.get("S", "unspecified")
    return "state", "Sets extrusion override factor.", f"This sets extrusion flow override to {factor}% (M221 scale)."


def _explain_acceleration(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Sets print/travel acceleration.", f"This updates acceleration limits using {_format_params(params)}."


def _explain_query_nozzle_temperature(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "message", "Requests current temperature report.", "This asks Klipper to report current heater temperatures (M105)."


def _explain_emergency_stop(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Emergency stop.", "This immediately halts printer activity and places Klipper in an emergency state requiring restart."


def _explain_get_current_position(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "message", "Requests current position report.", "This asks Klipper to print current motion coordinates (M114)."


def _explain_firmware_version(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "message", "Requests firmware version information.", "This asks Klipper to report firmware/version details (M115)."


def _explain_host_echo(_: str, params: dict[str, str], source: str) -> tuple[str, str, str]:
    message = source.partition(" ")[2].strip() or "a host message"
    return "message", "Echoes a message to the host.", f"This sends {message} to the host console (M118)."


def _explain_query_endstops_legacy(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "message", "Queries endstop states (legacy).", "This requests endstop status using M119. Klipper docs recommend QUERY_ENDSTOPS for extended usage."


def _explain_absolute_positioning(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Enables absolute movement mode.", "Subsequent X, Y, and Z moves are interpreted as machine coordinates instead of relative offsets."


def _explain_relative_positioning(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Enables relative movement mode.", "Subsequent X, Y, and Z moves are treated as offsets from the current position."


def _explain_set_position(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Overrides the current logical position.", f"This tells Klipper to treat the current position as {_format_params(params)} without physically moving the toolhead."


def _explain_absolute_extrusion(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Enables absolute extrusion mode.", "Extrusion moves now use a running absolute E position until another command changes the mode."


def _explain_relative_extrusion(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Enables relative extrusion mode.", "Extrusion values now represent incremental amounts rather than absolute E coordinates."


def _explain_nozzle_temperature(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    target = params.get("S") or params.get("R") or "unspecified"
    return "temperature", "Sets nozzle temperature target.", f"This updates the hotend target temperature to {target}C and continues immediately without waiting for the heater to settle."


def _explain_nozzle_temperature_wait(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    target = params.get("S") or params.get("R") or "unspecified"
    return "temperature", "Sets nozzle temperature and waits.", f"This heats or cools the hotend to {target}C and blocks the macro until the target is reached."


def _explain_bed_temperature(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    target = params.get("S") or params.get("R") or "unspecified"
    return "temperature", "Sets bed temperature target.", f"This updates the bed target temperature to {target}C and allows the macro to continue immediately."


def _explain_bed_temperature_wait(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    target = params.get("S") or params.get("R") or "unspecified"
    return "temperature", "Sets bed temperature and waits.", f"This blocks macro execution until the bed reaches {target}C."


def _explain_fan_command(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    speed = params.get("S", "current/default")
    return "state", "Sets part-cooling fan speed.", f"This updates the fan output using S={speed}. On many setups that value is a PWM-like speed level rather than a direct percentage."


def _explain_fan_off(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Turns the part-cooling fan off.", "This command stops the standard print cooling fan."


def _explain_display_message(_: str, params: dict[str, str], source: str) -> tuple[str, str, str]:
    message = source.partition(" ")[2].strip() or "a status message"
    return "message", "Displays a printer message.", f"This sends {message} to the printer display or front-end status message area."


def _explain_set_display_text(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    message = params.get("MSG", "")
    if message:
        return "message", "Sets display text.", f"This writes the display message to {message}."
    return "message", "Clears display text.", "SET_DISPLAY_TEXT without MSG clears the current display message."


def _explain_respond(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    message = params.get("MSG", "a host-visible response")
    response_type = params.get("TYPE", "echo")
    return "message", "Sends a host response.", f"This emits a {response_type} message back to the UI or console so the user can see runtime feedback: {message}."


def _explain_set_gcode_variable(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    macro_name = params.get("MACRO", "another macro")
    variable = params.get("VARIABLE", "a variable")
    value = params.get("VALUE", "a new value")
    return "state", "Updates a macro variable.", f"This writes {variable}={value} into macro {macro_name}, changing state that later macro calls can read."


def _explain_save_gcode_state(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    name = params.get("NAME", "an unnamed snapshot")
    return "state", "Saves the current g-code state.", f"This stores movement and mode settings under {name} so they can be restored later."


def _explain_restore_gcode_state(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    name = params.get("NAME", "a saved snapshot")
    move = params.get("MOVE", "0")
    return "state", "Restores a saved g-code state.", f"This restores the snapshot named {name}. MOVE={move} controls whether Klipper also moves back to the saved position."


def _explain_set_gcode_offset(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Adjusts toolhead offsets.", f"This changes the active g-code coordinate offset using {_format_params(params)}. It affects how future moves are interpreted."


def _explain_get_position(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "message", "Reports detailed toolhead position.", "This emits Klipper's GET_POSITION diagnostic output for current coordinate systems and internal position state."


def _explain_temperature_wait(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    sensor = params.get("SENSOR", "the selected sensor")
    minimum = params.get("MINIMUM")
    maximum = params.get("MAXIMUM")
    if minimum and maximum:
        window = f"between {minimum}C and {maximum}C"
    elif minimum:
        window = f"at least {minimum}C"
    elif maximum:
        window = f"at most {maximum}C"
    else:
        window = "within the requested range"
    return "temperature", "Waits for a sensor target range.", f"This pauses execution until {sensor} is {window}."


def _explain_turn_off_heaters(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "temperature", "Turns heaters off.", "This clears active heater targets so the printer can cool down."


def _explain_set_heater_temperature(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    heater = params.get("HEATER", "the selected heater")
    target = params.get("TARGET", "0")
    return "temperature", "Sets an arbitrary heater target.", f"This sets heater {heater} to target {target}C."


def _explain_update_delayed_gcode(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    delayed_id = params.get("ID", "a delayed_gcode task")
    duration = params.get("DURATION", "its configured delay")
    return "state", "Schedules delayed g-code.", f"This updates delayed task {delayed_id} so it runs after {duration} second(s)."


def _explain_bed_mesh_calibrate(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "motion", "Runs bed mesh calibration.", "This probes the bed to build a compensation mesh that later print moves can use."


def _explain_bed_mesh_clear(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Clears active bed mesh.", "This removes current bed mesh compensation from active motion planning."


def _explain_bed_mesh_output(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    pgp = params.get("PGP")
    detail = "This prints current mesh values to the terminal."
    if pgp == "1":
        detail += " With PGP=1 it also prints generated point/index information."
    return "message", "Prints mesh values.", detail


def _explain_bed_mesh_map(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "message", "Prints mesh state as JSON.", "This outputs bed mesh data in machine-friendly JSON format for host tools."


def _explain_bed_mesh_profile(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    action = next((key for key in ("LOAD", "SAVE", "REMOVE") if key in params), None)
    profile = params.get(action, "default") if action else params.get("PROFILE", "default")
    verb = action.lower() if action else "manage"
    return "state", "Manages a stored bed mesh profile.", f"This asks Klipper to {verb} the bed mesh profile named {profile}."


def _explain_bed_mesh_offset(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Applies bed mesh lookup offsets.", f"This shifts active mesh lookup behavior using {_format_params(params)}."


def _explain_bed_tilt_calibrate(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "motion", "Runs bed tilt calibration.", "This probes configured points and estimates XY tilt correction values."


def _explain_bed_screws_adjust(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "motion", "Starts manual bed screw adjustment.", "This walks through screw positions so the bed can be manually leveled."


def _explain_quad_gantry_level(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "motion", "Runs quad gantry leveling.", "This probes and adjusts the gantry so the toolhead plane is aligned before printing."


def _explain_z_tilt_adjust(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "motion", "Runs Z tilt adjustment.", "This probes configured points and adjusts independent Z steppers to compensate for tilt."


def _explain_screws_tilt_calculate(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "motion", "Calculates screw turn guidance.", "This probes bed points and computes screw adjustment turns for bed leveling."


def _explain_probe(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "motion", "Runs a probe measurement.", f"This performs a probing move using {_format_params(params)} and reports trigger position."


def _explain_probe_accuracy(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    samples = params.get("SAMPLES", "default sample count")
    return "motion", "Measures probe repeatability.", f"This runs repeated probe samples ({samples}) and reports spread statistics."


def _explain_probe_calibrate(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "motion", "Starts probe Z-offset calibration.", "This opens the guided process to calibrate probe z_offset, usually followed by SAVE_CONFIG."


def _explain_query_probe(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "message", "Queries probe trigger state.", "This reports whether the probe is currently open or triggered."


def _explain_query_endstops(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "message", "Queries endstop states.", "This reports endstop trigger/open state for configured axes."


def _explain_set_fan_speed(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    fan = params.get("FAN", "the named fan")
    if "TEMPLATE" in params:
        return "state", "Assigns fan control template.", f"This binds template {params.get('TEMPLATE', '')} to fan {fan} for continuous speed evaluation."
    speed = params.get("SPEED", "unspecified")
    return "state", "Sets named fan speed.", f"This sets fan {fan} to speed {speed} (0.0 to 1.0 scale)."


def _explain_set_pin(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    pin = params.get("PIN", "a configured output pin")
    if "TEMPLATE" in params:
        return "state", "Assigns pin control template.", f"This assigns template {params.get('TEMPLATE', '')} to pin {pin}."
    return "state", "Sets output pin value.", f"This updates pin {pin} using {_format_params(params)}."


def _explain_set_led(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    led = params.get("LED", "a configured LED")
    return "state", "Sets LED output.", f"This updates LED {led} color/intensity parameters using {_format_params(params)}."


def _explain_set_led_template(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    led = params.get("LED", "a configured LED")
    template = params.get("TEMPLATE", "")
    if template:
        return "state", "Assigns LED template.", f"This binds display template {template} to LED {led} for dynamic color updates."
    return "state", "Clears LED template.", f"This removes any template assignment from LED {led}."


def _explain_set_servo(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    servo = params.get("SERVO", "a configured servo")
    return "state", "Sets servo position.", f"This updates servo {servo} using {_format_params(params)}."


def _explain_set_input_shaper(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Adjusts input shaper settings.", f"This changes resonance compensation parameters using {_format_params(params)}."


def _explain_set_velocity_limit(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Adjusts motion limits.", f"This updates toolhead velocity/acceleration limits via {_format_params(params)}."


def _explain_set_pressure_advance(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    extruder = params.get("EXTRUDER", "active extruder")
    advance = params.get("ADVANCE", "current")
    return "state", "Adjusts pressure advance.", f"This updates pressure advance for {extruder} to {advance}."


def _explain_save_variable(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    variable = params.get("VARIABLE", "a variable")
    value = params.get("VALUE", "a value")
    return "state", "Persists a macro variable.", f"This saves {variable}={value} to disk through save_variables for reuse after restart."


def _explain_save_config(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Writes config and restarts.", "This persists pending calibration/config values to printer config and restarts Klipper host software."


def _explain_set_idle_timeout(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    timeout = params.get("TIMEOUT", "configured default")
    return "state", "Sets idle timeout.", f"This updates idle timeout to {timeout} second(s)."


def _explain_set_temperature_fan_target(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    fan = params.get("TEMPERATURE_FAN", params.get("temperature_fan", "temperature fan"))
    target = params.get("TARGET", params.get("target", "configured target"))
    return "temperature", "Sets temperature-fan target.", f"This updates temperature_fan {fan} to target {target}."


def _explain_set_print_stats_info(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Updates print layer metadata.", f"This passes slicer layer information to Klipper print_stats using {_format_params(params)}."


def _explain_sdcard_print_file(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    filename = params.get("FILENAME", "an SD file")
    return "state", "Starts SD-card print job.", f"This loads and starts virtual SD print file {filename}."


def _explain_sdcard_reset_file(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Resets SD print state.", "This unloads the current virtual SD file and clears SD print state."


def _explain_sd_list(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "message", "Lists SD card files.", "This requests a file listing from virtual SD storage (M20)."


def _explain_sd_init(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Initializes SD state.", "This initializes virtual SD card handling (M21)."


def _explain_sd_select(_: str, params: dict[str, str], source: str) -> tuple[str, str, str]:
    filename = source.partition(" ")[2].strip() or "an SD file"
    return "state", "Selects SD print file.", f"This selects {filename} for subsequent SD printing (M23)."


def _explain_sd_start_or_resume(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Starts or resumes SD print.", "This starts a selected SD print file or resumes a paused SD print (M24)."


def _explain_sd_pause(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Pauses SD print.", "This pauses the active virtual SD print (M25)."


def _explain_sd_set_position(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    offset = params.get("S", "unspecified offset")
    return "state", "Sets SD read offset.", f"This sets virtual SD read position to offset {offset} bytes (M26)."


def _explain_sd_status(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "message", "Reports SD print status.", "This requests current virtual SD print progress/state (M27)."


def _explain_set_build_percentage(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    percent = params.get("P", "unspecified")
    return "message", "Sets display build percentage.", f"This updates displayed print progress to {percent}% (M73)."


def _explain_restart(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Restarts Klipper host.", "This reloads config and performs a host-side restart (without MCU error-state clear)."


def _explain_firmware_restart(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Performs firmware restart.", "This restarts host and clears MCU error state (FIRMWARE_RESTART)."


def _explain_status(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "message", "Requests Klipper status.", "This prints current Klipper host status information."


def _explain_help(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "message", "Requests available commands list.", "This asks Klipper to print known extended G-Code commands."


def _explain_pause(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Pauses the current print flow.", "This triggers the printer's pause behavior, which often parks the toolhead and waits for operator input."


def _explain_resume(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Resumes a paused print.", "This asks Klipper to continue execution after a pause sequence completes."


def _explain_clear_pause(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Clears pause state.", "This clears pause state without resuming movement, useful before a new print start."


def _explain_cancel_print(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Cancels the current print.", "This stops the active print workflow and usually triggers configured cleanup behavior."


def _explain_pid_calibrate(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    heater = params.get("HEATER", "a heater")
    target = params.get("TARGET", "unspecified")
    return "temperature", "Runs PID calibration.", f"This starts PID tuning for heater {heater} toward {target}C."


def _explain_generic_command(command: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "unknown", f"Runs command {command}.", f"This line executes {command} with {_format_params(params)}. KlipperVault recognizes the command structure, but does not yet have a richer built-in explanation for it."