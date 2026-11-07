# PRediction Market. Targets marked (WP x) need that work package merged first.
VENV   ?= .venv
BIN    := $(VENV)/bin
PY     := $(BIN)/python
SAMPLE := examples/sample-target

.PHONY: setup dev test lint fmt check smoke-llm settle-sample league-sample demo-bug-local demo-good-local demo-reset tunnel

setup:            ## create .venv (Python 3.12) and install prm + dev deps
	uv venv --python 3.12 $(VENV)
	uv pip install --python $(PY) -e ".[dev]"

dev:              ## (WP E) run the server with the fake LLM
	PRM_FAKE_LLM=1 $(BIN)/prm serve --reload

test:             ## run the test suite with the deterministic fake LLM
	PRM_FAKE_LLM=1 $(BIN)/pytest -q

lint:             ## ruff lint + format check (what CI runs)
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

fmt:              ## auto-fix lint and format
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

check: lint test  ## run before every PR

smoke-llm:        ## (WP B) one real call per configured model; needs ANTHROPIC_API_KEY
	$(BIN)/prm smoke-llm

settle-sample:    ## (WP C, D) settle the sample target's clean head: prints counts only
	$(BIN)/prm settle --target $(SAMPLE)

league-sample:    ## (WP G) league on the sample target
	$(BIN)/prm league --target $(SAMPLE) --n 120

demo-bug-local:   ## (WP E) full offline market on examples/patches/bug_auth.patch
	PRM_FAKE_LLM=1 $(BIN)/prm demo bug --local --target $(SAMPLE) --repo local/sample-target

demo-good-local:  ## (WP E) full offline market on examples/patches/good_docstring.patch
	PRM_FAKE_LLM=1 $(BIN)/prm demo good --local --target $(SAMPLE) --repo local/sample-target

demo-reset:       ## (WP E) reset demo state
	$(BIN)/prm demo reset

tunnel:           ## public URL fallback; set PRM_PUBLIC_URL to the printed URL
	cloudflared tunnel --url http://localhost:8000
