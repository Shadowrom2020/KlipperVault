#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal i18n helper for KlipperVault UI labels and messages."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_DEFAULT_LANGUAGE = "en"
_current_language = _DEFAULT_LANGUAGE

_LOCALES_DIR = Path(__file__).resolve().parent / "locales"
_translation_cache: dict[str, dict[str, str]] = {}


def _is_valid_mapping(candidate: object) -> bool:
    """Return True when candidate is a dict[str, str]."""
    if not isinstance(candidate, dict):
        return False
    return all(isinstance(k, str) and isinstance(v, str) for k, v in candidate.items())


def _load_language_translations(language: str) -> dict[str, str]:
    """Load one language file from src/locales/<language>.py with safe fallback."""
    if language == _DEFAULT_LANGUAGE:
        return {}
    if language in _translation_cache:
        return _translation_cache[language]

    locale_path = _LOCALES_DIR / f"{language}.py"
    if not locale_path.exists():
        _translation_cache[language] = {}
        return {}

    module_name = f"klippervault_locale_{language}"
    spec = importlib.util.spec_from_file_location(module_name, locale_path)
    if spec is None or spec.loader is None:
        _translation_cache[language] = {}
        return {}

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        _translation_cache[language] = {}
        return {}

    translations = getattr(module, "TRANSLATIONS", {})
    if not _is_valid_mapping(translations):
        _translation_cache[language] = {}
        return {}

    _translation_cache[language] = dict(translations)
    return _translation_cache[language]


def _language_is_available(language: str) -> bool:
    """Return True when locale file exists for requested language."""
    if language == _DEFAULT_LANGUAGE:
        return True
    return (_LOCALES_DIR / f"{language}.py").exists()


def set_language(language: str | None) -> str:
    """Set active UI language and return normalized effective language."""
    global _current_language
    normalized = (language or _DEFAULT_LANGUAGE).strip().lower().replace("_", "-")
    short = normalized.split("-", maxsplit=1)[0]
    if _language_is_available(short):
        _current_language = short
    else:
        _current_language = _DEFAULT_LANGUAGE
    return _current_language


def get_language() -> str:
    """Return currently active UI language code."""
    return _current_language


def t(message: str, **kwargs: object) -> str:
    """Translate one message template and apply optional format kwargs."""
    translated = _load_language_translations(_current_language).get(message, message)
    if kwargs:
        return translated.format(**kwargs)
    return translated
