.PHONY: venv install fmt lint type test run

VENV=.venv
PY=$(VENV)/bin/python
PIP=$(VENV)/bin/pip

venv:
	python3 -m venv $(VENV)
	$(PIP) install -U pip

install: venv
	$(PIP) install -r requirements.txt
	$(PIP) install -r requirements-dev.txt

fmt:
	$(VENV)/bin/ruff check --fix .
	$(VENV)/bin/isort .
	$(VENV)/bin/black .

lint:
	$(VENV)/bin/ruff check .
	$(VENV)/bin/black --check .
	$(VENV)/bin/isort --check-only .

type:
	$(VENV)/bin/mypy src

test:
	$(VENV)/bin/pytest

run:
	$(PY) src/main.py
