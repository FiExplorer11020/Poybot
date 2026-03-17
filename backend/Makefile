.PHONY: install lint format test run migrate up down frontend

install:
	pip install -e .[dev]

lint:
	ruff check app tests

format:
	black app tests

test:
	pytest -q

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
