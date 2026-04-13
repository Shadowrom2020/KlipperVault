VENV ?= .venv
PYBABEL := $(shell test -x $(VENV)/bin/pybabel && echo $(VENV)/bin/pybabel || echo pybabel)
I18N_DOMAIN := klippervault
I18N_POT := src/locales/$(I18N_DOMAIN).pot

.PHONY: i18n-extract i18n-update i18n-compile i18n

# Extract translatable strings from Python sources into the template catalog.
i18n-extract:
	$(PYBABEL) extract -F babel.ini -o $(I18N_POT) src klipper_vault_gui.py

# Merge template updates into language catalogs.
i18n-update: i18n-extract
	$(PYBABEL) update -i $(I18N_POT) -d src/locales -D $(I18N_DOMAIN)

# Compile language catalogs for runtime gettext loading.
i18n-compile:
	$(PYBABEL) compile -d src/locales -D $(I18N_DOMAIN)

# Full translation refresh: extract, update, and compile catalogs.
i18n: i18n-update i18n-compile
