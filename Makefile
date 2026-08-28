.PHONY: install test generate run eval demo lint

install:
	pip install -e ".[dev]"

test:
	pytest -v

lint:
	ruff check src/ tests/

generate:
	python -m recon.generate.orchestrator

run:
	python -m recon.match.engine data/dev
	python -m recon.match.engine data/test

# --- eval requires GEMINI_API_KEY set; ablation sample size matches the
# 50-100 range chosen for this project ---
eval:
	python -m recon.evaluate.run_eval data/dev --llm --ablation 75

demo: generate run eval
	@echo "TODO (Day 9): full pipeline, end to end, on a clean clone"
