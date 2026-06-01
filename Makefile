# E-14 Acta Scraper — convenience targets.
# Resume is the default (safe, idempotent). `make fresh` wipes & re-runs (asks first).
#
# Tunables (override on the CLI, e.g. `make run CONCURRENCY=96 RATE=200`):
CONCURRENCY ?= 64
RATE        ?= 120
RETRY       ?= 5
PY          := .venv/bin/python
CLI         := $(PY) -m e14.cli

DL_FLAGS = --concurrency $(CONCURRENCY) --rate $(RATE) --auto-retry $(RETRY)
# Pipe only stdout (the summary) to the run log; leave stderr on the terminal so
# the tqdm progress bar animates live. Detailed logs persist to logs/e14.log and
# every acta is recorded in logs/results.jsonl regardless.
LOG      = | tee logs/run_$$(date +%Y%m%d_%H%M%S).log

.DEFAULT_GOAL := help
.PHONY: help setup universe run resume fresh retry stats dictionary package verify clean distclean

help: ## Show this help
	@echo "E-14 Acta Scraper — targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Tunables: CONCURRENCY=$(CONCURRENCY) RATE=$(RATE) RETRY=$(RETRY)"
	@echo "Example:  make fresh CONCURRENCY=96 RATE=200"

setup: ## Create the venv and install dependencies (uv)
	uv venv --python 3.12 .venv
	uv pip install --python .venv -e .
	@mkdir -p data logs

universe: ## (Re)build the national acta list -> data/mesa_universe.csv
	@mkdir -p data logs
	$(CLI) build-universe

run: ## Resume the download from where it left off (safe, idempotent)
	@mkdir -p logs
	@echo ">> Resuming download (concurrency=$(CONCURRENCY) rate=$(RATE) retry=$(RETRY))"
	$(CLI) download $(DL_FLAGS) $(LOG)
	@$(MAKE) --no-print-directory stats

resume: run ## Alias for 'run' (resume an interrupted download)

fresh: ## Wipe local data and run a clean full download (ASKS for confirmation)
	@echo "!! FRESH RUN — this DELETES all downloaded actas + manifest + logs:"
	@echo "     data/manifest.db*  data/actas/  data/failed.csv  logs/results.jsonl  dist/"
	@printf "   Type 'yes' to wipe and re-download everything: " && read ans && [ "$$ans" = "yes" ] || (echo "aborted." && exit 1)
	@rm -rf data/manifest.db* data/actas data/failed.csv logs/results.jsonl dist
	@mkdir -p logs
	@echo ">> Fresh download starting (concurrency=$(CONCURRENCY) rate=$(RATE) retry=$(RETRY))"
	$(CLI) download $(DL_FLAGS) $(LOG)
	@$(MAKE) --no-print-directory stats

retry: ## Re-attempt only the actas marked failed
	$(CLI) download --retry-failed $(DL_FLAGS)
	@$(MAKE) --no-print-directory stats

stats: ## Show manifest status summary (done / failed / skipped + GB)
	@$(CLI) stats

dictionary: ## Build human-readable DIVIPOL dictionary + per-acta index.csv
	$(CLI) dictionary

package: ## Bundle raw PDFs -> dist/by_department/*.zip + SHA256SUMS + index
	$(CLI) package
	@echo ">> dist/ ready to upload (Internet Archive / torrent)."

verify: ## Verify downloaded PDFs against dist/VERIFICACION_SHA256.txt
	@test -f dist/VERIFICACION_SHA256.txt || (echo "Run 'make package' first." && exit 1)
	@cd data/actas && sha256sum -c ../../dist/VERIFICACION_SHA256.txt | tail -5
	@echo ">> verify complete."

clean: ## Remove downloaded actas + manifest (ASKS), keeps universe CSV
	@printf "Delete data/actas + manifest (keep mesa_universe.csv)? type 'yes': " && read ans && [ "$$ans" = "yes" ] || (echo "aborted." && exit 1)
	@rm -rf data/manifest.db* data/actas data/failed.csv logs/results.jsonl dist
	@echo "cleaned."

distclean: clean ## Also remove the universe/dictionary/index CSVs and dist
	@rm -f data/mesa_universe.csv data/divipol_dictionary.csv data/index.csv
	@echo "distcleaned."
