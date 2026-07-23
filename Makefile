.PHONY: setup gam test run app clean help

VENV := .venv
PY := $(VENV)/bin/python
UV := uv
UV_VERSION := 0.11.7
PYTHON ?= python3

help:
	@echo "make setup   - create venv and install (dev + native window)"
	@echo "make gam     - vendor the GAM7 binary into gamgui/resources/gam7"
	@echo "make test    - run the offline test suite"
	@echo "make run     - launch the app (native window; falls back to a browser URL)"
	@echo "make app     - build the standalone macOS .app (PyInstaller, macOS only)"
	@echo "make clean   - remove venv and build artifacts"

setup:
	@test "$$($(UV) --version | awk '{print $$1 " " $$2}')" = "uv $(UV_VERSION)" || \
		(echo "GamGUI requires uv $(UV_VERSION); install that exact version before setup." >&2; exit 1)
	$(UV) sync --frozen --python "$(PYTHON)" --extra dev --extra desktop --extra build

gam:
	./scripts/fetch_gam.sh $(if $(TAG),--tag $(TAG))

test:
	$(PY) -m pytest -q

run:
	$(PY) -m gamgui.app

app:
	./scripts/build_app.sh

clean:
	rm -rf $(VENV) build dist *.egg-info .pytest_cache
