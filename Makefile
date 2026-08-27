.PHONY: install test generate run eval demo lint

install:
	pip install -e ".[dev]"

test:
	pytest -v

lint:
	ruff check src/ tests/

generate:
	python -m recon.generate.orchestrator

# --- these targets don't exist yet — Days 3-9 build them out ---

run:
	python -m recon.match.engine data/dev
	python -m recon.match.engine data/test

# --- this target doesn't exist yet — Day 8 builds it out ---

eval:
	@echo "TODO (Day 8): recon evaluate --results results/ --ground-truth data/test/ground_truth.json"

demo: generate run eval
	@echo "TODO (Day 9): full pipeline, end to end, on a clean clone"
