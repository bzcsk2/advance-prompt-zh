.PHONY: install test lint run

install:
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check src tests

run:
	uvicorn agentic_rag_enterprise.api.main:app --reload
