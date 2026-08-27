.PHONY: install test generate run eval demo lint

install:
	pip install -e ".[dev]"

test:
	pytest -v

lint:
	ruff check src/ tests/

# --- these targets don't exist yet — Days 1-9 build them out ---

generate:
	@echo "TODO (Day 1-2): python -m recon.generate.ledger, .settlement, .bank -> data/dev, data/test"

run:
	@echo "TODO (Day 3-5): recon run --input data/test --out results/"

eval:
	@echo "TODO (Day 8): recon evaluate --results results/ --ground-truth data/test/ground_truth.json"

demo: generate run eval
	@echo "TODO (Day 9): full pipeline, end to end, on a clean clone"
