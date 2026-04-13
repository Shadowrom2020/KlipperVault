#!/usr/bin/env python3
# Copyright (C) 2026 Jürgen Herrmann
# SPDX-License-Identifier: GPL-3.0-or-later
"""gettext i18n helper for KlipperVault UI labels and messages."""

from __future__ import annotations

import gettext
import sys
from pathlib import Path

_DEFAULT_LANGUAGE = "en"
_current_language = _DEFAULT_LANGUAGE

_meipass = getattr(sys, "_MEIPASS", "")
if getattr(sys, "frozen", False) and _meipass:
    _LOCALES_DIR = Path(str(_meipass)) / "locales"
else:
    _LOCALES_DIR = Path(__file__).resolve().parent / "locales"
_GETTEXT_DOMAIN = "klippervault"
_active_gettext: gettext.NullTranslations = gettext.NullTranslations()


def _language_is_available(language: str) -> bool:
    """Return True when gettext catalog exists for requested language."""
    if language == _DEFAULT_LANGUAGE:
        return True
    gettext_catalog = _LOCALES_DIR / language / "LC_MESSAGES" / f"{_GETTEXT_DOMAIN}.mo"
    return gettext_catalog.exists()


def _load_gettext_translations(language: str) -> gettext.NullTranslations:
    """Load gettext translations for one language or return null translations."""
    if language == _DEFAULT_LANGUAGE:
        return gettext.NullTranslations()

    catalog_path = _LOCALES_DIR / language / "LC_MESSAGES" / f"{_GETTEXT_DOMAIN}.mo"
    if not catalog_path.exists():
        return gettext.NullTranslations()

    try:
        with catalog_path.open("rb") as catalog_file:
            return gettext.GNUTranslations(catalog_file)
    except Exception:
        return gettext.NullTranslations()


def set_language(language: str | None) -> str:
    """Set active UI language and return normalized effective language."""
    global _active_gettext, _current_language
    normalized = (language or _DEFAULT_LANGUAGE).strip().lower().replace("_", "-")
    short = normalized.split("-", maxsplit=1)[0]
    if _language_is_available(short):
        _current_language = short
    else:
        _current_language = _DEFAULT_LANGUAGE

    _active_gettext = _load_gettext_translations(_current_language)
    return _current_language


def get_language() -> str:
    """Return currently active UI language code."""
    return _current_language


def t(message: str, **kwargs: object) -> str:
    """Translate one message template and apply optional format kwargs."""
    translated = _active_gettext.gettext(message)

    if kwargs:
        try:
            return translated.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return translated
    return translated


set_language(_DEFAULT_LANGUAGE)
