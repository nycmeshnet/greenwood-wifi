.PHONY: venv install test run clean help

PYTHON  := python3
VENV    := venv
PIP     := $(VENV)/bin/pip
PYTEST  := $(VENV)/bin/pytest
MAIN    := $(VENV)/bin/python main.py

help:
	@echo "Usage:"
	@echo "  make venv      Create virtual environment and install dependencies"
	@echo "  make install   Install/update dependencies into existing venv"
	@echo "  make test      Run unit tests"
	@echo "  make run       Run optimizer with default ranges (500 ft / 260 ft)"
	@echo "  make clean     Remove venv and cached data"

venv:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt pytest
	@echo "Venv ready. Activate with: source venv/bin/activate"

install:
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt pytest

test:
	$(PYTEST) tests/ -v

run:
	$(MAIN) --range-2g 500 --range-5g 260

clean:
	rm -rf $(VENV) data/cache/
