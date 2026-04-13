#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Explain Klipper macro gcode in user-facing language."""

from __future__ import annotations

from collections.abc import Callable
from collections import Counter
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping, Sequence, TypedDict

# ---------------------------------------------------------------------------
# Module-level compiled regex constants (avoid per-call recompilation)
# ---------------------------------------------------------------------------
_RE_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_RE_AXIS_LETTER = re.compile(r"([A-Za-z])(.*)")
_RE_VALUE_START = re.compile(r"^[+\-0-9.{]")
_RE_UPPER_COMMAND = re.compile(r"[A-Z0-9_.]+")
_RE_SET_CLAUSE = re.compile(r"set\s+(.+?)\s*=\s*(.+)", re.IGNORECASE | re.DOTALL)
_RE_FOR_CLAUSE = re.compile(r"for\s+(.+?)\s+in\s+(.+)", re.IGNORECASE | re.DOTALL)
_RE_FILTER_EXPR = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\((.*)\))?$")
_RE_PARAMS = re.compile(r"\bparams\.([A-Za-z_][A-Za-z0-9_]*)")
_RE_PRINTER_OBJECTS = re.compile(r"\bprinter\.([A-Za-z_][A-Za-z0-9_\.]*)")
_RE_FILTERS = re.compile(r"\|\s*([A-Za-z_][A-Za-z0-9_]*)")


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
    confidence: str
    effects: list[str]
    summary: str
    details: str
    references: list[dict[str, object]]


CommandExplainer = Callable[[str, dict[str, str], str], tuple[str, str, str]]


class CommandPackEntry(TypedDict, total=False):
    """One plugin command definition for explainer extension packs."""

    alias_of: str
    kind: str
    summary: str
    details: str
    confidence: str
    effects: list[str]


_DEFAULT_COMMAND_PACK_PATH = (Path.home() / ".config" / "klippervault" / "klippervault_command_pack.json").resolve()


def build_macro_reference_index(macros: Sequence[Mapping[str, object]]) -> dict[str, list[MacroReference]]:
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
    macro: Mapping[str, object] | None,
    available_macros: Sequence[Mapping[str, object]] | None = None,
    verbosity: str = "detailed",
    command_pack: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, Any]:
    """Explain a macro body in plain language and discover macro references."""
    normalized_verbosity = _normalize_verbosity(verbosity)
    custom_explainers, metadata_overrides = _build_command_pack_explainers(command_pack)

    if macro is None:
        return {
            "summary": "Select a macro to see an explanation.",
            "lines": [],
            "references": [],
            "flow": [],
            "flow_summary": "",
            "verbosity": normalized_verbosity,
            "has_content": False,
        }

    gcode_text = str(macro.get("gcode") or "")
    body_lines = gcode_text.splitlines()
    explainable_lines = _collapse_multiline_jinja_blocks(body_lines)

    current_macro_names = {
        str(macro.get("macro_name", "")).strip().lower(),
        str(macro.get("display_name") or macro.get("runtime_macro_name") or "").strip().lower(),
    }
    current_macro_names.discard("")
    reference_index = build_macro_reference_index(available_macros or [])
    rename_existing = str(macro.get("rename_existing") or "").strip()
    renamed_aliases = _build_renamed_alias_map(macro, rename_existing)
    rename_existing_entry = _build_rename_existing_entry(macro, rename_existing, reference_index)
    has_explainable_content = bool(body_lines) or rename_existing_entry is not None
    if not has_explainable_content:
        return {
            "summary": "This macro does not currently contain any g-code lines.",
            "lines": [],
            "references": [],
            "flow": [],
            "flow_summary": "",
            "verbosity": normalized_verbosity,
            "has_content": False,
        }

    categories: Counter[str] = Counter()
    confidence_levels: Counter[str] = Counter()
    effects: Counter[str] = Counter()
    references: dict[tuple[str, str], dict[str, object]] = {}
    explanation_entries: list[ExplanationLine] = []
    explanation_lines: list[dict[str, object]] = []
    explained_line_count = 0

    if rename_existing_entry is not None:
        explained_line_count += 1
        categories[rename_existing_entry.kind] += 1
        confidence_levels[rename_existing_entry.confidence] += 1
        for effect in rename_existing_entry.effects:
            effects[effect] += 1
        explanation_entries.append(rename_existing_entry)
        explanation_lines.append(_serialize_line(rename_existing_entry, normalized_verbosity))
        for reference in rename_existing_entry.references:
            key = (str(reference["macro_name"]), str(reference["file_path"]))
            references[key] = reference

    for line_number, raw_line in explainable_lines:
        entry = _explain_line(
            raw_line,
            line_number,
            current_macro_names,
            reference_index,
            renamed_aliases,
            custom_explainers,
            metadata_overrides,
        )
        if entry is None:
            continue
        explained_line_count += 1
        categories[entry.kind] += 1
        confidence_levels[entry.confidence] += 1
        for effect in entry.effects:
            effects[effect] += 1
        explanation_entries.append(entry)
        explanation_lines.append(_serialize_line(entry, normalized_verbosity))
        for reference in entry.references:
            key = (str(reference["macro_name"]), str(reference["file_path"]))
            references[key] = reference

    if not explanation_lines:
        return {
            "summary": "This macro does not contain any executable lines that need explanation.",
            "lines": [],
            "references": [],
            "flow": [],
            "flow_summary": "",
            "verbosity": normalized_verbosity,
            "has_content": False,
        }

    flow = _build_flow_phases(explanation_entries)

    return {
        "summary": _build_summary(categories, references, confidence_levels, effects, explained_line_count),
        "lines": explanation_lines,
        "references": list(references.values()),
        "confidence": dict(confidence_levels),
        "effects": dict(effects),
        "risk_line_count": int(effects.get("disruptive", 0)),
        "flow": flow,
        "flow_summary": _build_flow_summary(flow),
        "verbosity": normalized_verbosity,
        "has_content": True,
    }


def load_command_pack(path: str | Path | None = None) -> dict[str, CommandPackEntry]:
    """Load command-pack JSON from path, env override, or default config location."""
    pack_path = _resolve_command_pack_path(path)
    if not pack_path.exists():
        return {}

    try:
        payload = json.loads(pack_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    source: object = payload
    if isinstance(payload, dict) and isinstance(payload.get("commands"), dict):
        source = payload.get("commands")

    if not isinstance(source, dict):
        return {}

    command_pack: dict[str, CommandPackEntry] = {}
    for key, value in source.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        normalized = _normalize_command_pack_entry(value)
        if not normalized:
            continue
        command_pack[key.strip().upper()] = normalized

    return command_pack


def _resolve_command_pack_path(path: str | Path | None) -> Path:
    """Resolve command-pack path from explicit argument, env, or default location."""
    if path is not None:
        return Path(path).expanduser().resolve()

    env_path = os.environ.get("KLIPPERVAULT_COMMAND_PACK_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()

    return _DEFAULT_COMMAND_PACK_PATH


def _normalize_command_pack_entry(entry: dict[str, object]) -> CommandPackEntry:
    """Normalize one command-pack entry, filtering unsupported fields."""
    normalized: CommandPackEntry = {}

    alias_of = entry.get("alias_of")
    if isinstance(alias_of, str) and alias_of.strip():
        normalized["alias_of"] = alias_of.strip().upper()

    kind = entry.get("kind")
    if isinstance(kind, str) and kind.strip():
        normalized["kind"] = kind.strip().lower()

    summary = entry.get("summary")
    if isinstance(summary, str) and summary.strip():
        normalized["summary"] = summary.strip()

    details = entry.get("details")
    if isinstance(details, str) and details.strip():
        normalized["details"] = details.strip()

    confidence = entry.get("confidence")
    if isinstance(confidence, str) and confidence.strip().lower() in {"high", "medium", "low"}:
        normalized["confidence"] = confidence.strip().lower()

    effects = entry.get("effects")
    if isinstance(effects, list):
        filtered_effects = [item.strip() for item in effects if isinstance(item, str) and item.strip()]
        if filtered_effects:
            normalized["effects"] = filtered_effects

    return normalized


def _normalize_verbosity(verbosity: str) -> str:
    """Normalize verbosity selector with safe fallback."""
    return "concise" if str(verbosity or "").strip().lower() == "concise" else "detailed"


def _serialize_line(entry: ExplanationLine, verbosity: str) -> dict[str, object]:
    """Serialize explanation line with optional concise detail reduction."""
    payload = asdict(entry)
    if verbosity == "concise":
        payload["details"] = _first_sentence(str(payload.get("details", "")))
    return payload


def _first_sentence(text: str) -> str:
    """Return first sentence-like chunk from detail text."""
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""

    parts = _RE_SENTENCE_SPLIT.split(cleaned, maxsplit=1)
    return parts[0]


def _build_flow_phases(entries: list[ExplanationLine]) -> list[str]:
    """Build ordered, de-duplicated high-level flow phases for a macro."""
    phases: list[str] = []
    for entry in entries:
        phase = _phase_for_entry(entry)
        if phase and phase not in phases:
            phases.append(phase)
    return phases


def _phase_for_entry(entry: ExplanationLine) -> str:
    """Map one explained line to a high-level flow phase."""
    effects = set(entry.effects)
    if entry.kind == "macro_call":
        return "macro call chain"
    if "temperature_control" in effects or "blocking_wait" in effects:
        return "heat and wait"
    if entry.kind == "motion":
        return "movement"
    if entry.kind == "state":
        return "state updates"
    if entry.kind == "control":
        return "template control"
    if entry.kind == "message":
        return "user feedback"
    return "custom or unknown commands"


def _build_flow_summary(flow: list[str]) -> str:
    """Render compact sentence that describes macro phase sequence."""
    if not flow:
        return ""
    return "Execution flow: " + " -> ".join(flow) + "."


def _build_summary(
    categories: Counter[str],
    references: dict[tuple[str, str], dict[str, object]],
    confidence_levels: Counter[str],
    effects: Counter[str],
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

    disruptive_count = int(effects.get("disruptive", 0))
    if disruptive_count:
        parts.append(f"Caution: {disruptive_count} line(s) may interrupt or restart printer operation.")

    blocking_count = int(effects.get("blocking_wait", 0))
    if blocking_count:
        parts.append(f"It contains {blocking_count} blocking wait line(s) that can pause macro progress.")

    low_confidence_count = int(confidence_levels.get("low", 0))
    if low_confidence_count:
        parts.append(f"{low_confidence_count} line(s) have low explanation confidence and may need manual verification.")

    return " ".join(parts)


def _explain_line(
    raw_line: str,
    line_number: int,
    current_macro_names: set[str],
    reference_index: dict[str, list[MacroReference]],
    renamed_aliases: dict[str, str] | None = None,
    custom_explainers: dict[str, CommandExplainer] | None = None,
    metadata_overrides: dict[str, dict[str, object]] | None = None,
) -> ExplanationLine | None:
    """Explain one line of macro body text."""
    stripped = raw_line.strip()

    if not stripped:
        return None

    if stripped.startswith("#") or stripped.startswith(";"):
        return None

    # In g-code, ';' starts a comment segment. Strip it aggressively so
    # trailing comments never interfere with command/template detection.
    stripped = stripped.split(";", 1)[0].strip()
    if not stripped:
        return None

    stripped = _strip_inline_comment(stripped).strip()
    if not stripped:
        return None

    if stripped.startswith("{%") and stripped.endswith("%}"):
        return _explain_template_block(raw_line, line_number)

    if "{{" in stripped and "}}" in stripped:
        return ExplanationLine(
            line_number=line_number,
            text=raw_line,
            kind="control",
            confidence="high",
            effects=["template_control"],
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
            confidence="high",
            effects=["macro_call_transfer"],
            summary=f"Calls macro {upper_command}.",
            details=(
                f"This line transfers control to macro {upper_command}. {reference_note} is available in "
                f"{macro_refs[0]['file_path']}, and you can open it from the link button below."
            ),
            references=macro_refs,
        )

    alias_source = (renamed_aliases or {}).get(upper_command, "")
    if alias_source:
        details = (
            f"This line calls the renamed macro alias {upper_command}, which points to the previous "
            f"implementation of {alias_source}."
        )
        details += (
            " No indexed replacement source was found, so this is assumed to call the renamed "
            "Klipper standard macro/behavior."
        )
        return ExplanationLine(
            line_number=line_number,
            text=raw_line,
            kind="macro_call",
            confidence="high",
            effects=["macro_call_transfer"],
            summary=f"Calls renamed macro {upper_command}.",
            details=details,
            references=[],
        )

    explain = _get_command_explainer(upper_command, custom_explainers)
    kind, summary, details = explain(upper_command, params, stripped)
    confidence, effects = _resolve_line_metadata(upper_command, kind, metadata_overrides)
    return ExplanationLine(
        line_number=line_number,
        text=raw_line,
        kind=kind,
        confidence=confidence,
        effects=effects,
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

        axis_match = _RE_AXIS_LETTER.fullmatch(token)
        if axis_match and axis_match.group(2):
            candidate_value = axis_match.group(2)
            # Only treat compact one-letter parameters (e.g. X10, E-2, S{temp})
            # as key/value pairs. Plain words like PROBE_CALIBRATE should remain
            # command text, not synthetic parameters.
            if _RE_VALUE_START.match(candidate_value):
                candidate_key = axis_match.group(1).upper()
                params[candidate_key] = candidate_value
                current_key = candidate_key
                continue

            # Command-like all-caps tokens should stop value continuation,
            # but mixed/lowercase text is often part of a quoted value split
            # by whitespace (e.g. MSG="value # keep").
            if _RE_UPPER_COMMAND.fullmatch(token):
                current_key = None
                continue

            if current_key is not None:
                params[current_key] = f"{params[current_key]} {token}".strip()
            continue

        if current_key is not None:
            params[current_key] = f"{params[current_key]} {token}".strip()
    return params


def _strip_inline_comment(line: str) -> str:
    """Strip inline '#' comments while preserving hash markers inside quotes."""
    in_single = False
    in_double = False

    for idx, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if char == "#" and not in_single and not in_double:
            return line[:idx]
    return line


def _collapse_multiline_jinja_blocks(body_lines: Sequence[str]) -> list[tuple[int, str]]:
    """Merge multiline Jinja blocks into single logical explainer lines.

    Macros often split ``{% ... %}`` or ``{{ ... }}`` blocks across lines.
    Explaining those lines independently can misclassify continuation lines.
    """
    collapsed: list[tuple[int, str]] = []
    idx = 0
    total = len(body_lines)

    while idx < total:
        raw_line = body_lines[idx]
        stripped = _strip_inline_comment(raw_line.strip()).strip()

        if stripped.startswith("{%") and not stripped.endswith("%}"):
            start_line = idx + 1
            parts = [raw_line]
            idx += 1
            while idx < total:
                parts.append(body_lines[idx])
                candidate = _strip_inline_comment(body_lines[idx].strip()).strip()
                idx += 1
                if candidate.endswith("%}"):
                    break
            collapsed.append((start_line, "\n".join(parts)))
            continue

        if stripped.startswith("{{") and not stripped.endswith("}}"):
            start_line = idx + 1
            parts = [raw_line]
            idx += 1
            while idx < total:
                parts.append(body_lines[idx])
                candidate = _strip_inline_comment(body_lines[idx].strip()).strip()
                idx += 1
                if candidate.endswith("}}"):
                    break
            collapsed.append((start_line, "\n".join(parts)))
            continue

        collapsed.append((idx + 1, raw_line))
        idx += 1

    return collapsed


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


def _build_rename_existing_entry(
    macro: Mapping[str, object],
    rename_existing: str,
    reference_index: dict[str, list[MacroReference]],
) -> ExplanationLine | None:
    """Build explainer entry for section-level rename_existing directives."""
    if not rename_existing:
        return None

    macro_name = str(macro.get("macro_name") or "").strip()
    macro_name_lower = macro_name.lower()
    current_file_path = str(macro.get("file_path") or "").strip()

    candidate_refs = [asdict(ref) for ref in reference_index.get(macro_name_lower, [])]
    source_refs: list[dict[str, object]] = []
    for ref in candidate_refs:
        ref_path = str(ref.get("file_path") or "").strip()
        if current_file_path and ref_path == current_file_path:
            continue
        source_refs.append(ref)

    display_macro_name = macro_name or "this macro"
    summary = "Preserves replaced macro under a new name."
    details = (
        f"Section directive rename_existing maps the previously active {display_macro_name} implementation "
        f"to callable name {rename_existing} while this section takes over {display_macro_name}."
    )
    if source_refs:
        details += (
            f" A prior definition is indexed in {source_refs[0]['file_path']}, "
            "and you can open it from the reference link."
        )
    else:
        details += (
            " No prior macro definition with this name was found in indexed cfg files, "
            "so this is treated as preserving a Klipper standard macro/behavior under the renamed alias."
        )

    return ExplanationLine(
        line_number=0,
        text=f"rename_existing: {rename_existing}",
        kind="state",
        confidence="high",
        effects=["printer_state_change"],
        summary=summary,
        details=details,
        references=source_refs[:3],
    )


def _build_renamed_alias_map(macro: Mapping[str, object], rename_existing: str) -> dict[str, str]:
    """Build mapping of alias command -> source macro name from rename_existing."""
    if not rename_existing:
        return {}

    macro_name = str(macro.get("macro_name") or "").strip()
    if not macro_name:
        return {}

    return {rename_existing.upper(): macro_name}


def _confidence_for_explanation(kind: str) -> str:
    """Return confidence level for an explanation category."""
    if kind == "unknown":
        return "low"
    if kind == "control":
        return "medium"
    return "high"


def _resolve_line_metadata(
    command: str,
    kind: str,
    metadata_overrides: dict[str, dict[str, object]] | None,
) -> tuple[str, list[str]]:
    """Resolve confidence/effects, allowing plugin packs to override defaults."""
    base_confidence = _confidence_for_explanation(kind)
    base_effects = _infer_effects(command, kind)

    if not metadata_overrides:
        return base_confidence, base_effects

    override = metadata_overrides.get(command, {})
    confidence = str(override.get("confidence", "")).strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = base_confidence

    raw_effects = override.get("effects")
    if isinstance(raw_effects, list):
        filtered = [item for item in raw_effects if isinstance(item, str) and item.strip()]
        effects = list(dict.fromkeys(filtered)) or base_effects
    else:
        effects = base_effects

    return confidence, effects


def _build_command_pack_explainers(
    command_pack: Mapping[str, Mapping[str, object]] | None,
) -> tuple[dict[str, CommandExplainer], dict[str, dict[str, object]]]:
    """Compile user-provided command pack into explainers and metadata overrides."""
    if not command_pack:
        return {}, {}

    explainers: dict[str, CommandExplainer] = {}
    metadata_overrides: dict[str, dict[str, object]] = {}

    for raw_command, entry in command_pack.items():
        command = str(raw_command or "").strip().upper()
        if not command:
            continue
        if not isinstance(entry, dict):
            continue

        alias_of = str(entry.get("alias_of", "")).strip().upper()
        if alias_of:
            aliased_explainer = _get_command_explainer(alias_of)
            explainers[command] = aliased_explainer
            metadata_overrides[command] = {
                "confidence": entry.get("confidence"),
                "effects": entry.get("effects"),
            }
            continue

        kind = str(entry.get("kind", "")).strip().lower()
        summary = str(entry.get("summary", "")).strip()
        details = str(entry.get("details", "")).strip()
        if not (kind and summary and details):
            continue

        def _packed_explainer(_: str, __: dict[str, str], ___: str, *, _kind: str = kind, _summary: str = summary, _details: str = details) -> tuple[str, str, str]:
            return _kind, _summary, _details

        explainers[command] = _packed_explainer
        metadata_overrides[command] = {
            "confidence": entry.get("confidence"),
            "effects": entry.get("effects"),
        }

    return explainers, metadata_overrides


def _infer_effects(command: str, kind: str) -> list[str]:
    """Infer coarse side effects for a line so UI can highlight impact."""
    effects: list[str] = []

    if kind == "motion":
        effects.append("motion")
    if kind == "temperature":
        effects.append("temperature_control")
    if kind == "state":
        effects.append("printer_state_change")
    if kind == "message":
        effects.append("user_feedback")
    if kind == "control":
        effects.append("template_control")

    if command in {"G4", "M109", "M190", "M400", "TEMPERATURE_WAIT"}:
        effects.append("blocking_wait")

    if command in {"M104", "M109", "M140", "M190", "SET_HEATER_TEMPERATURE", "TURN_OFF_HEATERS"}:
        effects.append("heater_target_change")

    if command in {"SAVE_CONFIG", "SAVE_VARIABLE"}:
        effects.append("persistent_write")

    if command in {"M112", "RESTART", "FIRMWARE_RESTART", "SAVE_CONFIG", "FORCE_MOVE"}:
        effects.append("disruptive")

    # Preserve insertion order while deduplicating.
    return list(dict.fromkeys(effects))


def _explain_template_block(raw_line: str, line_number: int) -> ExplanationLine:
    """Explain Jinja control directives embedded in a macro."""
    cleaned = _strip_inline_comment(raw_line.strip()).strip()
    if cleaned.startswith("{%") and cleaned.endswith("%}"):
        stripped = cleaned[2:-2].strip()
    else:
        stripped = raw_line.strip()[2:-2].strip()
    normalized = stripped.lower()
    confidence = "high"

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
            details = _describe_template_set_assignment(variable_name, expression)
            details += " " + _describe_template_expression(expression)
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
        confidence=confidence,
        effects=["template_control"],
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
    match = _RE_SET_CLAUSE.match(clause)
    if not match:
        return "", ""
    return match.group(1).strip(), match.group(2).strip()


def _parse_template_for_clause(clause: str) -> tuple[str, str]:
    """Split a Jinja for directive into loop variable and iterable."""
    match = _RE_FOR_CLAUSE.match(clause)
    if not match:
        return "", ""
    return match.group(1).strip(), match.group(2).strip()


def _split_template_filters(expression: str) -> list[str]:
    """Split Jinja expression into base expression and top-level filters."""
    parts: list[str] = []
    current: list[str] = []
    paren_depth = 0

    for char in expression:
        if char == "|" and paren_depth == 0:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue
        if char == "(":
            paren_depth += 1
        elif char == ")" and paren_depth > 0:
            paren_depth -= 1
        current.append(char)

    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _describe_template_set_assignment(variable_name: str, expression: str) -> str:
    """Describe a Jinja set assignment with source, fallback, and type hints."""
    parts = _split_template_filters(expression)
    if not parts:
        return f"This sets template variable {variable_name}."

    source_expr = parts[0]
    source_description = source_expr
    if source_expr.startswith("params."):
        source_description = f"macro parameter {source_expr}"
    elif source_expr.startswith("printer."):
        source_description = f"live printer value {source_expr}"

    default_value = ""
    output_type = ""

    for filter_expr in parts[1:]:
        match = _RE_FILTER_EXPR.match(filter_expr)
        if not match:
            continue
        filter_name = match.group(1).strip().lower()
        filter_arg = (match.group(2) or "").strip()

        if filter_name == "default" and filter_arg and not default_value:
            default_value = filter_arg

        if filter_name in {"int", "float", "bool", "str", "string"} and not output_type:
            output_type = {
                "int": "int",
                "float": "float",
                "bool": "bool",
                "str": "string",
                "string": "string",
            }[filter_name]

    details = f"This sets template variable {variable_name} from {source_description}."
    if default_value:
        details += f" If the source is missing, it falls back to {default_value}."
    if output_type:
        details += f" The resulting value type is {output_type}."
    return details


def _describe_template_expression(expression: str) -> str:
    """Generate a more concrete explanation for a Jinja expression."""
    expr = str(expression or "").strip()
    if not expr:
        return ""

    notes: list[str] = []
    parameter_names = sorted(set(_RE_PARAMS.findall(expr)))
    printer_objects = sorted(set(_RE_PRINTER_OBJECTS.findall(expr)))
    filters = sorted(set(_RE_FILTERS.findall(expr)))

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


def _static_explanation(effect: str, summary: str, details: str) -> tuple[str, str, str]:
    """Return a standard (effect, summary, details) explainer tuple."""
    return effect, summary, details


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
    return _static_explanation(
        "motion",
        "Runs firmware retraction.",
        "This retracts filament using Klipper firmware-retraction settings instead of a raw E move.",
    )


def _explain_firmware_unretraction(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "motion",
        "Runs firmware unretraction.",
        "This restores filament after a firmware retraction using configured unretract behavior.",
    )


def _explain_arc_plane_select(command: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    planes = {"G17": "XY", "G18": "XZ", "G19": "YZ"}
    plane = planes.get(command, "active")
    return "state", f"Selects arc plane {plane}.", "This sets which coordinate plane Klipper uses for subsequent G2/G3 arc interpolation."


def _explain_home_command(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    axes = [axis for axis in ("X", "Y", "Z") if axis in params]
    target = ", ".join(axes) if axes else "all configured axes"
    return "motion", "Homes the printer.", f"This command moves {target} to their reference endstops so Klipper knows where the machine is."


def _explain_disable_steppers(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Disables stepper motors.",
        "This turns motors off so axes may no longer hold position until re-enabled by movement or explicit commands.",
    )


def _explain_wait_moves(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Waits for queued moves to finish.",
        "This blocks execution until all already queued motion has completed.",
    )


def _explain_speed_factor(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    factor = params.get("S", "unspecified")
    return "state", "Sets speed override factor.", f"This sets the motion speed override to {factor}% (M220 scale)."


def _explain_extrude_factor(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    factor = params.get("S", "unspecified")
    return "state", "Sets extrusion override factor.", f"This sets extrusion flow override to {factor}% (M221 scale)."


def _explain_acceleration(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Sets print/travel acceleration.", f"This updates acceleration limits using {_format_params(params)}."


def _explain_query_nozzle_temperature(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "message",
        "Requests current temperature report.",
        "This asks Klipper to report current heater temperatures (M105).",
    )


def _explain_emergency_stop(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Emergency stop.",
        "This immediately halts printer activity and places Klipper in an emergency state requiring restart.",
    )


def _explain_get_current_position(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "message",
        "Requests current position report.",
        "This asks Klipper to print current motion coordinates (M114).",
    )


def _explain_firmware_version(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "message",
        "Requests firmware version information.",
        "This asks Klipper to report firmware/version details (M115).",
    )


def _explain_host_echo(_: str, params: dict[str, str], source: str) -> tuple[str, str, str]:
    message = source.partition(" ")[2].strip() or "a host message"
    return "message", "Echoes a message to the host.", f"This sends {message} to the host console (M118)."


def _explain_query_endstops_legacy(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "message",
        "Queries endstop states (legacy).",
        "This requests endstop status using M119. Klipper docs recommend QUERY_ENDSTOPS for extended usage.",
    )


def _explain_absolute_positioning(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Enables absolute movement mode.",
        "Subsequent X, Y, and Z moves are interpreted as machine coordinates instead of relative offsets.",
    )


def _explain_relative_positioning(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Enables relative movement mode.",
        "Subsequent X, Y, and Z moves are treated as offsets from the current position.",
    )


def _explain_set_position(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Overrides the current logical position.", f"This tells Klipper to treat the current position as {_format_params(params)} without physically moving the toolhead."


def _explain_absolute_extrusion(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Enables absolute extrusion mode.",
        "Extrusion moves now use a running absolute E position until another command changes the mode.",
    )


def _explain_relative_extrusion(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Enables relative extrusion mode.",
        "Extrusion values now represent incremental amounts rather than absolute E coordinates.",
    )


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
    return _static_explanation(
        "state",
        "Turns the part-cooling fan off.",
        "This command stops the standard print cooling fan.",
    )


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
    return _static_explanation(
        "message",
        "Reports detailed toolhead position.",
        "This emits Klipper's GET_POSITION diagnostic output for current coordinate systems and internal position state.",
    )


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
    return _static_explanation(
        "temperature",
        "Turns heaters off.",
        "This clears active heater targets so the printer can cool down.",
    )


def _explain_set_heater_temperature(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    heater = params.get("HEATER", "the selected heater")
    target = params.get("TARGET", "0")
    return "temperature", "Sets an arbitrary heater target.", f"This sets heater {heater} to target {target}C."


def _explain_update_delayed_gcode(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    delayed_id = params.get("ID", "a delayed_gcode task")
    duration = params.get("DURATION", "its configured delay")
    return "state", "Schedules delayed g-code.", f"This updates delayed task {delayed_id} so it runs after {duration} second(s)."


def _explain_bed_mesh_calibrate(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "motion",
        "Runs bed mesh calibration.",
        "This probes the bed to build a compensation mesh that later print moves can use.",
    )


def _explain_bed_mesh_clear(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Clears active bed mesh.",
        "This removes current bed mesh compensation from active motion planning.",
    )


def _explain_bed_mesh_output(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    pgp = params.get("PGP")
    detail = "This prints current mesh values to the terminal."
    if pgp == "1":
        detail += " With PGP=1 it also prints generated point/index information."
    return "message", "Prints mesh values.", detail


def _explain_bed_mesh_map(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "message",
        "Prints mesh state as JSON.",
        "This outputs bed mesh data in machine-friendly JSON format for host tools.",
    )


def _explain_bed_mesh_profile(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    action = next((key for key in ("LOAD", "SAVE", "REMOVE") if key in params), None)
    profile = params.get(action, "default") if action else params.get("PROFILE", "default")
    verb = action.lower() if action else "manage"
    return "state", "Manages a stored bed mesh profile.", f"This asks Klipper to {verb} the bed mesh profile named {profile}."


def _explain_bed_mesh_offset(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "state", "Applies bed mesh lookup offsets.", f"This shifts active mesh lookup behavior using {_format_params(params)}."


def _explain_bed_tilt_calibrate(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "motion",
        "Runs bed tilt calibration.",
        "This probes configured points and estimates XY tilt correction values.",
    )


def _explain_bed_screws_adjust(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "motion",
        "Starts manual bed screw adjustment.",
        "This walks through screw positions so the bed can be manually leveled.",
    )


def _explain_quad_gantry_level(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "motion",
        "Runs quad gantry leveling.",
        "This probes and adjusts the gantry so the toolhead plane is aligned before printing.",
    )


def _explain_z_tilt_adjust(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "motion",
        "Runs Z tilt adjustment.",
        "This probes configured points and adjusts independent Z steppers to compensate for tilt.",
    )


def _explain_screws_tilt_calculate(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "motion",
        "Calculates screw turn guidance.",
        "This probes bed points and computes screw adjustment turns for bed leveling.",
    )


def _explain_probe(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "motion", "Runs a probe measurement.", f"This performs a probing move using {_format_params(params)} and reports trigger position."


def _explain_probe_accuracy(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    samples = params.get("SAMPLES", "default sample count")
    return "motion", "Measures probe repeatability.", f"This runs repeated probe samples ({samples}) and reports spread statistics."


def _explain_probe_calibrate(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "motion",
        "Starts probe Z-offset calibration.",
        "This opens the guided process to calibrate probe z_offset, usually followed by SAVE_CONFIG.",
    )


def _explain_query_probe(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "message",
        "Queries probe trigger state.",
        "This reports whether the probe is currently open or triggered.",
    )


def _explain_query_endstops(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "message",
        "Queries endstop states.",
        "This reports endstop trigger/open state for configured axes.",
    )


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


def _explain_set_kinematic_position(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    axes = [f"{axis}={params[axis]}" for axis in ("X", "Y", "Z") if axis in params]
    axis_text = ", ".join(axes) if axes else "the supplied axis values"
    set_homed = params.get("SET_HOMED")
    clear_homed = params.get("CLEAR_HOMED")

    details = (
        f"This overrides Klipper's internal kinematic position to {axis_text} without physically moving the toolhead. "
        "Use it carefully, because incorrect values can desynchronize logical and physical position until re-homing."
    )
    if set_homed:
        details += f" SET_HOMED={set_homed} marks listed axes as homed."
    if clear_homed:
        details += f" CLEAR_HOMED={clear_homed} clears homed state for listed axes."

    return "state", "Overrides internal kinematic coordinates.", details


def _explain_set_tmc_current(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    stepper = params.get("STEPPER", "a configured TMC stepper")
    run_current = params.get("CURRENT", "the existing run current")
    hold_current = params.get("HOLDCURRENT", params.get("HOLD_CURRENT", "the existing hold current"))

    details = (
        f"This changes TMC motor current for {stepper}: run current {run_current}, hold current {hold_current}. "
        "It applies immediately and affects torque, motor heat, and skipped-step behavior."
    )
    return "state", "Adjusts TMC motor current.", details


def _explain_set_stepper_enable(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    stepper = params.get("STEPPER", "a configured stepper")
    enable_value = str(params.get("ENABLE", params.get("enable", ""))).strip()

    if enable_value == "1":
        state_text = "enables"
        summary = "Enables a stepper driver."
        effect_text = "so the motor can hold position and accept commanded moves"
    elif enable_value == "0":
        state_text = "disables"
        summary = "Disables a stepper driver."
        effect_text = "so the motor stops holding torque until it is re-enabled"
    else:
        state_text = "changes the enable state of"
        summary = "Changes stepper driver enable state."
        effect_text = f"using ENABLE={enable_value or 'unspecified'}"

    details = f"This {state_text} {stepper}, {effect_text}."
    return "state", summary, details


def _explain_manual_stepper(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    stepper = params.get("STEPPER", "a configured manual stepper")
    move = params.get("MOVE")
    speed = params.get("SPEED")
    accel = params.get("ACCEL")
    stop_on_endstop = params.get("STOP_ON_ENDSTOP")
    enable_value = params.get("ENABLE") or params.get("enable")

    if move is not None:
        details = f"This commands manual stepper {stepper} to move to {move}."
        if speed is not None:
            details += f" Requested speed is {speed}."
        if accel is not None:
            details += f" Requested acceleration is {accel}."
        if stop_on_endstop is not None:
            details += f" STOP_ON_ENDSTOP={stop_on_endstop} changes whether the move aborts on endstop trigger."
        return "motion", "Moves a manual stepper.", details

    if enable_value is not None:
        if str(enable_value).strip() == "0":
            return "state", "Disables a manual stepper.", f"This turns off manual stepper {stepper} so it no longer holds torque."
        if str(enable_value).strip() == "1":
            return "state", "Enables a manual stepper.", f"This enables manual stepper {stepper} so it can be driven and hold position."

    return "state", "Controls a manual stepper.", f"This updates manual stepper {stepper} using {_format_params(params)}."


def _explain_sync_extruder_motion(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    extruder = params.get("EXTRUDER", "the active extruder")
    motion_queue = params.get("MOTION_QUEUE", "the selected motion queue")

    if str(motion_queue).strip() in {"", "None", "none"}:
        details = (
            f"This detaches extruder motion for {extruder} from any shared motion queue. "
            "That usually stops it from mirroring another extruder's planned extrusion moves."
        )
    else:
        details = (
            f"This synchronizes extruder motion for {extruder} with motion queue {motion_queue}. "
            "It is commonly used when switching toolheads or remapping which extruder follows planned extrusion."
        )
    return "state", "Reassigns extruder motion synchronization.", details


def _explain_set_stepper_phase(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    stepper = params.get("STEPPER", "a configured stepper")
    phase = params.get("PHASE", "the requested phase")

    details = (
        f"This sets the tracked electrical phase for {stepper} to {phase}. "
        "It is a low-level calibration/state command that affects how Klipper interprets the stepper's phase reference."
    )
    return "state", "Sets stepper phase reference.", details


def _explain_stepper_buzz(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "motion",
        "Runs stepper buzz test.",
        f"This jogs {params.get('STEPPER', 'a configured stepper')} back and forth in a short test pattern so you can identify the motor or verify wiring/direction.",
    )


def _explain_dump_tmc(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "message",
        "Prints TMC driver diagnostics.",
        f"This queries TMC register and status information for {params.get('STEPPER', 'a configured TMC stepper')} and prints it to the console for troubleshooting.",
    )


def _explain_init_tmc(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Reinitializes a TMC driver.",
        f"This asks Klipper to re-send configuration to the TMC driver for {params.get('STEPPER', 'a configured TMC stepper')}, which can help recover expected driver settings after low-level changes or diagnostics.",
    )


def _explain_set_tmc_field(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    stepper = params.get("STEPPER", "a configured TMC stepper")
    field = params.get("FIELD", "a driver field")
    value = params.get("VALUE", "an unspecified value")
    return (
        "state",
        "Sets a low-level TMC register field.",
        f"This updates TMC field {field} on {stepper} to {value}. It is a low-level driver tuning/debug command and can change motor behavior immediately.",
    )


def _explain_force_move(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    stepper = params.get("STEPPER", "a configured stepper")
    distance = params.get("DISTANCE", "an unspecified distance")
    velocity = params.get("VELOCITY", "configured/default velocity")

    details = (
        f"This forces direct movement of {stepper} by {distance} at velocity {velocity}. "
        "Use this carefully because FORCE_MOVE bypasses normal coordinated kinematics and can move hardware unexpectedly."
    )
    if "{" in distance or "{{" in distance:
        details += " The movement distance is computed from a template expression at runtime."

    return "motion", "Forces direct stepper movement.", details


def _explain_set_pressure_advance(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    extruder = params.get("EXTRUDER", "active extruder")
    advance = params.get("ADVANCE", "current")
    return "state", "Adjusts pressure advance.", f"This updates pressure advance for {extruder} to {advance}."


def _explain_save_variable(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    variable = params.get("VARIABLE", "a variable")
    value = params.get("VALUE", "a value")
    return "state", "Persists a macro variable.", f"This saves {variable}={value} to disk through save_variables for reuse after restart."


def _explain_save_config(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Writes config and restarts.",
        "This persists pending calibration/config values to printer config and restarts Klipper host software.",
    )


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
    return _static_explanation(
        "state",
        "Resets SD print state.",
        "This unloads the current virtual SD file and clears SD print state.",
    )


def _explain_sd_list(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "message",
        "Lists SD card files.",
        "This requests a file listing from virtual SD storage (M20).",
    )


def _explain_sd_init(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Initializes SD state.",
        "This initializes virtual SD card handling (M21).",
    )


def _explain_sd_select(_: str, params: dict[str, str], source: str) -> tuple[str, str, str]:
    filename = source.partition(" ")[2].strip() or "an SD file"
    return "state", "Selects SD print file.", f"This selects {filename} for subsequent SD printing (M23)."


def _explain_sd_start_or_resume(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Starts or resumes SD print.",
        "This starts a selected SD print file or resumes a paused SD print (M24).",
    )


def _explain_sd_pause(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Pauses SD print.",
        "This pauses the active virtual SD print (M25).",
    )


def _explain_sd_set_position(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    offset = params.get("S", "unspecified offset")
    return "state", "Sets SD read offset.", f"This sets virtual SD read position to offset {offset} bytes (M26)."


def _explain_sd_status(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "message",
        "Reports SD print status.",
        "This requests current virtual SD print progress/state (M27).",
    )


def _explain_set_build_percentage(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    percent = params.get("P", "unspecified")
    return "message", "Sets display build percentage.", f"This updates displayed print progress to {percent}% (M73)."


def _explain_restart(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Restarts Klipper host.",
        "This reloads config and performs a host-side restart (without MCU error-state clear).",
    )


def _explain_firmware_restart(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Performs firmware restart.",
        "This restarts host and clears MCU error state (FIRMWARE_RESTART).",
    )


def _explain_status(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "message",
        "Requests Klipper status.",
        "This prints current Klipper host status information.",
    )


def _explain_help(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "message",
        "Requests available commands list.",
        "This asks Klipper to print known extended G-Code commands.",
    )


def _explain_pause(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Pauses the current print flow.",
        "This triggers the printer's pause behavior, which often parks the toolhead and waits for operator input.",
    )


def _explain_resume(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Resumes a paused print.",
        "This asks Klipper to continue execution after a pause sequence completes.",
    )


def _explain_clear_pause(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Clears pause state.",
        "This clears pause state without resuming movement, useful before a new print start.",
    )


def _explain_cancel_print(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return _static_explanation(
        "state",
        "Cancels the current print.",
        "This stops the active print workflow and usually triggers configured cleanup behavior.",
    )


def _explain_pid_calibrate(_: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    heater = params.get("HEATER", "a heater")
    target = params.get("TARGET", "unspecified")
    return "temperature", "Runs PID calibration.", f"This starts PID tuning for heater {heater} toward {target}C."


def _get_command_explainer(
    command: str,
    custom_explainers: dict[str, CommandExplainer] | None = None,
) -> CommandExplainer:
    """Return registered explainer for a command token."""
    if custom_explainers and command in custom_explainers:
        return custom_explainers[command]
    return _COMMAND_EXPLAINERS.get(command, _explain_generic_command)


_COMMAND_GROUPS: tuple[tuple[tuple[str, ...], CommandExplainer], ...] = (
    (("G0", "G1"), _explain_motion_command),
    (("G2", "G3"), _explain_arc_move),
    (("G4",), _explain_dwell),
    (("G10",), _explain_firmware_retraction),
    (("G11",), _explain_firmware_unretraction),
    (("G17", "G18", "G19"), _explain_arc_plane_select),
    (("G28",), _explain_home_command),
    (("G90",), _explain_absolute_positioning),
    (("G91",), _explain_relative_positioning),
    (("G92",), _explain_set_position),
    (("M18", "M84"), _explain_disable_steppers),
    (("M20",), _explain_sd_list),
    (("M21",), _explain_sd_init),
    (("M23",), _explain_sd_select),
    (("M24",), _explain_sd_start_or_resume),
    (("M25",), _explain_sd_pause),
    (("M26",), _explain_sd_set_position),
    (("M27",), _explain_sd_status),
    (("M73",), _explain_set_build_percentage),
    (("M82",), _explain_absolute_extrusion),
    (("M83",), _explain_relative_extrusion),
    (("M104",), _explain_nozzle_temperature),
    (("M105",), _explain_query_nozzle_temperature),
    (("M109",), _explain_nozzle_temperature_wait),
    (("M112",), _explain_emergency_stop),
    (("M114",), _explain_get_current_position),
    (("M115",), _explain_firmware_version),
    (("M117",), _explain_display_message),
    (("M118",), _explain_host_echo),
    (("M119",), _explain_query_endstops_legacy),
    (("M140",), _explain_bed_temperature),
    (("M190",), _explain_bed_temperature_wait),
    (("M204",), _explain_acceleration),
    (("M220",), _explain_speed_factor),
    (("M221",), _explain_extrude_factor),
    (("M400",), _explain_wait_moves),
    (("M106",), _explain_fan_command),
    (("M107",), _explain_fan_off),
    (("RESPOND",), _explain_respond),
    (("SET_DISPLAY_TEXT",), _explain_set_display_text),
    (("SET_GCODE_VARIABLE",), _explain_set_gcode_variable),
    (("SAVE_GCODE_STATE",), _explain_save_gcode_state),
    (("RESTORE_GCODE_STATE",), _explain_restore_gcode_state),
    (("SET_GCODE_OFFSET",), _explain_set_gcode_offset),
    (("GET_POSITION",), _explain_get_position),
    (("TEMPERATURE_WAIT",), _explain_temperature_wait),
    (("TURN_OFF_HEATERS",), _explain_turn_off_heaters),
    (("SET_HEATER_TEMPERATURE",), _explain_set_heater_temperature),
    (("UPDATE_DELAYED_GCODE",), _explain_update_delayed_gcode),
    (("BED_MESH_CALIBRATE",), _explain_bed_mesh_calibrate),
    (("BED_MESH_CLEAR",), _explain_bed_mesh_clear),
    (("BED_MESH_OUTPUT",), _explain_bed_mesh_output),
    (("BED_MESH_MAP",), _explain_bed_mesh_map),
    (("BED_MESH_PROFILE",), _explain_bed_mesh_profile),
    (("BED_MESH_OFFSET",), _explain_bed_mesh_offset),
    (("BED_TILT_CALIBRATE",), _explain_bed_tilt_calibrate),
    (("BED_SCREWS_ADJUST",), _explain_bed_screws_adjust),
    (("QUAD_GANTRY_LEVEL",), _explain_quad_gantry_level),
    (("Z_TILT_ADJUST",), _explain_z_tilt_adjust),
    (("SCREWS_TILT_CALCULATE",), _explain_screws_tilt_calculate),
    (("PROBE",), _explain_probe),
    (("PROBE_ACCURACY",), _explain_probe_accuracy),
    (("PROBE_CALIBRATE",), _explain_probe_calibrate),
    (("QUERY_PROBE",), _explain_query_probe),
    (("QUERY_ENDSTOPS",), _explain_query_endstops),
    (("SET_FAN_SPEED",), _explain_set_fan_speed),
    (("SET_PIN",), _explain_set_pin),
    (("SET_LED",), _explain_set_led),
    (("SET_LED_TEMPLATE",), _explain_set_led_template),
    (("SET_SERVO",), _explain_set_servo),
    (("SET_INPUT_SHAPER",), _explain_set_input_shaper),
    (("SET_VELOCITY_LIMIT",), _explain_set_velocity_limit),
    (("SET_KINEMATIC_POSITION",), _explain_set_kinematic_position),
    (("SET_TMC_CURRENT",), _explain_set_tmc_current),
    (("SET_STEPPER_ENABLE",), _explain_set_stepper_enable),
    (("MANUAL_STEPPER",), _explain_manual_stepper),
    (("SYNC_EXTRUDER_MOTION",), _explain_sync_extruder_motion),
    (("SET_STEPPER_PHASE",), _explain_set_stepper_phase),
    (("STEPPER_BUZZ",), _explain_stepper_buzz),
    (("DUMP_TMC",), _explain_dump_tmc),
    (("INIT_TMC",), _explain_init_tmc),
    (("SET_TMC_FIELD",), _explain_set_tmc_field),
    (("FORCE_MOVE",), _explain_force_move),
    (("SET_PRESSURE_ADVANCE",), _explain_set_pressure_advance),
    (("SAVE_VARIABLE",), _explain_save_variable),
    (("SAVE_CONFIG",), _explain_save_config),
    (("SET_IDLE_TIMEOUT",), _explain_set_idle_timeout),
    (("SET_TEMPERATURE_FAN_TARGET",), _explain_set_temperature_fan_target),
    (("SET_PRINT_STATS_INFO",), _explain_set_print_stats_info),
    (("SDCARD_PRINT_FILE",), _explain_sdcard_print_file),
    (("SDCARD_RESET_FILE",), _explain_sdcard_reset_file),
    (("RESTART",), _explain_restart),
    (("FIRMWARE_RESTART",), _explain_firmware_restart),
    (("STATUS",), _explain_status),
    (("HELP",), _explain_help),
    (("PAUSE",), _explain_pause),
    (("RESUME",), _explain_resume),
    (("CLEAR_PAUSE",), _explain_clear_pause),
    (("CANCEL_PRINT",), _explain_cancel_print),
    (("PID_CALIBRATE",), _explain_pid_calibrate),
)


def _build_command_explainers() -> dict[str, CommandExplainer]:
    """Expand grouped command aliases into a direct command lookup map."""
    command_explainers: dict[str, CommandExplainer] = {}
    for commands, explainer in _COMMAND_GROUPS:
        for command in commands:
            if command in command_explainers:
                raise ValueError(f"Duplicate command registration detected: {command}")
            command_explainers[command] = explainer
    return command_explainers


_COMMAND_EXPLAINERS = _build_command_explainers()


def _explain_generic_command(command: str, params: dict[str, str], __: str) -> tuple[str, str, str]:
    return "unknown", f"Runs command {command}.", f"This line executes {command} with {_format_params(params)}. KlipperVault recognizes the command structure, but does not yet have a richer built-in explanation for it."