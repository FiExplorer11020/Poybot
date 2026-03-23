.PHONY: install lint format test run migrate up down frontend validate smoke

install:
	pip install -e .[dev]

lint:
	ruff check app tests

format:
	black app tests

test:
	PYTHONPATH=. python -m pytest -p no:capture -q

smoke:
	PYTHONPATH=. python -m py_compile app/main.py app/live/state.py app/api/v1/live_routes.py app/services/adaptive_strategy.py

validate: smoke test
	cd ../frontend && npm run lint

run:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

migrate:
	alembic upgrade head

up:
	docker compose up --build -d

down:
	docker compose down -v

frontend:
	cd ../frontend && npm run dev
