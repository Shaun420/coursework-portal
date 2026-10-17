VENV ?= ../.venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

.PHONY: install dev seed test loadtest lint clean

install:
	$(PIP) install -r requirements-dev.txt

dev:
	$(PY) -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

seed:
	$(PY) -m scripts.seed

test:
	$(PY) -m pytest

loadtest:
	$(PY) -m scripts.loadtest --users 200 --duration 25

lint:
	$(PY) -m ruff check app tests scripts

clean:
	rm -rf data .pytest_cache **/__pycache__
