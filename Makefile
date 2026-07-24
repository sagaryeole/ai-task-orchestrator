.PHONY: test lint format install dev clean release docs docs-serve

test:
	python -m unittest discover -s tests

lint:
	ruff check src/ orchestrator.py tests/

format:
	ruff format src/ orchestrator.py tests/

install:
	pip install -e .

dev:
	pip install -e ".[dev]" 2>/dev/null || pip install -e .
	pip install ruff pre-commit
	pre-commit install

clean:
	rm -rf dist/ build/ *.egg-info/ __pycache__/ src/*.egg-info/
	find . -name "*.pyc" -delete
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

release:
	python -m build
	twine upload dist/*

docs:
	mkdocs build

docs-serve:
	mkdocs serve
