from klipper_vault_i18n import get_language, set_language, t


def test_set_language_normalizes_and_shortens_locale() -> None:
    language = set_language("DE_de")

    assert language == "de"
    assert get_language() == "de"


def test_unknown_language_falls_back_to_english() -> None:
    language = set_language("xx")

    assert language == "en"
    assert t("Close") == "Close"


def test_gettext_catalog_provides_existing_de_translations() -> None:
    set_language("de")

    assert t("Close") == "Schließen"
    assert t("Choose two versions to compare.") == "Wähle zwei Versionen zum Vergleichen."


def test_missing_format_arguments_do_not_raise() -> None:
    set_language("de")

    text = t("Compare stored versions for {macro_name}.")

    assert "{" in text and "}" in text
