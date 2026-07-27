.PHONY: setup gam test run app app-profiles clean help

VENV := .venv
PY := $(VENV)/bin/python
UV := uv
UV_VERSION := 0.11.7
# Interpreter used to create the venv. macOS's own /usr/bin/python3 is 3.9, and pyproject declares
# requires-python = ">=3.10" — so a bare `python3 -m venv` builds a venv that pip then refuses to
# install into. Leave PYTHON empty to auto-pick the newest usable python3.1x on PATH, or pin it:
#   make setup PYTHON=/opt/homebrew/bin/python3.13
PYTHON ?=
PYTHON_CANDIDATES := python3.14 python3.13 python3.12 python3.11 python3.10 python3
PROFILE ?= core

help:
	@echo "make setup   - create venv and install (dev + native window); needs Python 3.10+"
	@echo "               override the interpreter with: make setup PYTHON=python3.13"
	@echo "make gam     - vendor the GAM7 binary into gamgui/resources/gam7"
	@echo "make test    - run the offline test suite"
	@echo "make run     - launch the app (native window; falls back to a browser URL)"
	@echo "make app PROFILE=core                 - build the Core macOS .app"
	@echo "make app PROFILE=classroom-oneroster  - build Core + OneRoster"
	@echo "make app-profiles                      - build/self-test both profiles serially"
	@echo "make clean   - remove venv and build artifacts"

setup:
	@test "$$($(UV) --version | awk '{print $$1 " " $$2}')" = "uv $(UV_VERSION)" || \
		(echo "GamGUI requires uv $(UV_VERSION); install that exact version before setup." >&2; exit 1)
	@py="$(PYTHON)"; \
	if [ -n "$$py" ]; then \
	  "$$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null || { \
	    echo "make setup: PYTHON=$$py is not a usable Python 3.10+ ($$("$$py" -V 2>&1))." >&2; exit 1; }; \
	else \
	  for c in $(PYTHON_CANDIDATES); do \
	    command -v "$$c" >/dev/null 2>&1 || continue; \
	    "$$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null || continue; \
	    py="$$c"; break; \
	  done; \
	fi; \
	if [ -z "$$py" ]; then \
	  echo "make setup: no Python 3.10+ found on PATH (macOS ships 3.9, which cannot install gamgui)." >&2; \
	  echo "  Install one:   brew install python@3.13   (or the python.org macOS installer)" >&2; \
	  echo "  Or point at an existing one:   make setup PYTHON=/path/to/python3.13" >&2; \
	  exit 1; \
	fi; \
	echo "syncing $(VENV) with $$py ($$("$$py" -V 2>&1))"; \
	$(UV) sync --frozen --python "$$py" --extra dev --extra desktop --extra build

gam:
	./scripts/fetch_gam.sh $(if $(TAG),--tag $(TAG))

test:
	$(PY) -m pytest -q

run:
	$(PY) -m gamgui.app

app:
	PROFILE="$(PROFILE)" ./scripts/build_app.sh

app-profiles:
	$(MAKE) app PROFILE=core
	test -x dist/GamGUI.app/Contents/MacOS/GamGUI
	dist/GamGUI.app/Contents/MacOS/GamGUI --self-test --json
	rm -rf dist/GamGUI-core.app
	mv dist/GamGUI.app dist/GamGUI-core.app
	mv dist/GamGUI.app.artifact.json dist/GamGUI-core.app.artifact.json
	$(MAKE) app PROFILE=classroom-oneroster
	test -x dist/GamGUI.app/Contents/MacOS/GamGUI
	dist/GamGUI.app/Contents/MacOS/GamGUI --self-test --json

clean:
	rm -rf $(VENV) build dist *.egg-info .pytest_cache
