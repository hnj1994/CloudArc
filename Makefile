.PHONY: dev test demo run docker audit
dev:
	python3 -m venv .venv && .venv/bin/pip install -r requirements.lock.txt && .venv/bin/pip install -e ".[dev]"
test:
	.venv/bin/python -m pytest -q
demo:
	CLOUDARC_DATA_DIR=./data .venv/bin/cloudarc seed-demo
run:
	CLOUDARC_DATA_DIR=./data CLOUDARC_AUTH_MODE=dev .venv/bin/cloudarc serve --port 8080
docker:
	docker compose up -d --build
audit:
	.venv/bin/pip install pip-audit && .venv/bin/pip-audit -r requirements.lock.txt
