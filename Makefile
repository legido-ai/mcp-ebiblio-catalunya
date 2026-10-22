.PHONY: help test lint check-imports clean

PYTHON ?= python3

help:
	@echo "make test          - run the hermetic test suite (no credentials, no network)"
	@echo "make lint          - byte-compile the sources and tests"
	@echo "make check-imports - import the package from a clean interpreter"

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m compileall -q src tests

check-imports:
	PYTHONPATH=src $(PYTHON) -c "import odilo_ebiblio, odilo_ebiblio.client, odilo_ebiblio.drm; print('ok')"

clean:
	rm -rf .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
