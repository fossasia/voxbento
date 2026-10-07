.PHONY: install test lint format run

install:
	uv sync --all-groups

test:
	uv run pytest tests/ -v

lint:
	uv run ruff check .

format:
	uv run ruff format .

run:
	uv run uvicorn fastapi_app:app --host 0.0.0.0 --port 8000 --reload
