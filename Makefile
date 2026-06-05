# E-14 Acta Scraper — convenience targets.
# Resume is the default (safe, idempotent). `make fresh` wipes & re-runs (asks first).
#
# Tunables (override on the CLI, e.g. `make run CONCURRENCY=96 RATE=200`):
CONCURRENCY ?= 64
RATE        ?= 120
RETRY       ?= 5
PY          := .venv/bin/python
CLI         := $(PY) -m e14.cli
DETECTOR    := .venv/bin/e14detector

DETECTOR_OUTPUT ?= data/detector
SAMPLE_LIMIT    ?= 38
QWEN_CONCURRENCY ?= 12   # VLM pass is network-bound; serial (1) was the main slowdown
REPORT_HOST     ?= 127.0.0.1
REPORT_PORT     ?= 8001
PDF             ?=
DOC_ID          ?=

DL_FLAGS = --concurrency $(CONCURRENCY) --rate $(RATE) --auto-retry $(RETRY)
# Pipe only stdout (the summary) to the run log; leave stderr on the terminal so
# the tqdm progress bar animates live. Detailed logs persist to logs/e14.log and
# every acta is recorded in logs/results.jsonl regardless.
LOG      = | tee logs/run_$$(date +%Y%m%d_%H%M%S).log

.DEFAULT_GOAL := help
.PHONY: help setup universe run resume fresh retry stats dictionary package verify clean distclean detector-sample detector-vlm detector-add detector-serve detector-crop-progress

help: ## Show this help
	@echo "E-14 Acta Scraper — targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Tunables: CONCURRENCY=$(CONCURRENCY) RATE=$(RATE) RETRY=$(RETRY)"
	@echo "Example:  make fresh CONCURRENCY=96 RATE=200"
	@echo
	@echo "Detector tunables: SAMPLE_LIMIT=$(SAMPLE_LIMIT) DETECTOR_OUTPUT=$(DETECTOR_OUTPUT) QWEN_CONCURRENCY=$(QWEN_CONCURRENCY)"
	@echo "Examples:"
	@echo "  make detector-sample SAMPLE_LIMIT=38"
	@echo "  make detector-add PDF=data/actas/29/022/000/01/E14_PRE_29_022_000_01_003_delegados.pdf DOC_ID=E14_PRE_29_022_000_01_003_delegados"
	@echo "  make detector-serve REPORT_PORT=8001"

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

detector-sample: ## Reprocess the detector sample, then run Qwen on queued candidate rows
	$(DETECTOR) process --input-dir data/actas --output-dir $(DETECTOR_OUTPUT) --limit $(SAMPLE_LIMIT) --workers 4 --force
	$(DETECTOR) vlm-review --provider qwen --output-dir $(DETECTOR_OUTPUT) --concurrency $(QWEN_CONCURRENCY)

detector-vlm: ## Run Qwen review on queued candidate rows (optionally DOC_ID=...)
	@if [ -n "$(DOC_ID)" ]; then \
	  $(DETECTOR) vlm-review --provider qwen --output-dir $(DETECTOR_OUTPUT) --document-id "$(DOC_ID)" --concurrency $(QWEN_CONCURRENCY); \
	else \
	  $(DETECTOR) vlm-review --provider qwen --output-dir $(DETECTOR_OUTPUT) --concurrency $(QWEN_CONCURRENCY); \
	fi

detector-add: ## Add one acta: make detector-add PDF=... DOC_ID=...
	@test -n "$(PDF)" || (echo "Set PDF=path/to/E14_PRE_..._delegados.pdf" && exit 1)
	@test -n "$(DOC_ID)" || (echo "Set DOC_ID=E14_PRE_..._delegados" && exit 1)
	$(DETECTOR) process-one --pdf "$(PDF)" --output-dir $(DETECTOR_OUTPUT)
	$(DETECTOR) vlm-review --provider qwen --output-dir $(DETECTOR_OUTPUT) --document-id "$(DOC_ID)" --concurrency $(QWEN_CONCURRENCY)

detector-serve: ## Serve the Spanish anomaly review report
	$(DETECTOR) serve --host $(REPORT_HOST) --port $(REPORT_PORT) --output-dir $(DETECTOR_OUTPUT)

# --- National "drop CV, Gemma on a sample" pipeline -------------------------
# Cropping runs on ALL files (fast: no CV analysis); Gemma pre-screens only
# LLM_SAMPLE_RATE of documents. The crowd poll + live Gemma do the rest.
LLM_SAMPLE_RATE ?= 0.05

detector-crop-progress: ## Crop progress + per-dept %% (CROP_PROGRESS_BY_DEPT=0, DEPT_LIMIT=N)
	@DETECTOR_OUTPUT=$(DETECTOR_OUTPUT) bash scripts/crop_progress.sh

detector-crop-all: ## Crop-only pass over ALL actas (no CV) — fast national first pass
	$(DETECTOR) process --input-dir data/actas --output-dir $(DETECTOR_OUTPUT) --workers 8 --crop-only --force

detector-gemma-sample: ## Pre-screen LLM_SAMPLE_RATE of documents with Gemma (OpenRouter)
	$(DETECTOR) vlm-review --provider openrouter --output-dir $(DETECTOR_OUTPUT) --sample-rate $(LLM_SAMPLE_RATE) --concurrency $(QWEN_CONCURRENCY)

clean: ## Remove downloaded actas + manifest (ASKS), keeps universe CSV
	@printf "Delete data/actas + manifest (keep mesa_universe.csv)? type 'yes': " && read ans && [ "$$ans" = "yes" ] || (echo "aborted." && exit 1)
	@rm -rf data/manifest.db* data/actas data/failed.csv logs/results.jsonl dist
	@echo "cleaned."

distclean: clean ## Also remove the universe/dictionary/index CSVs and dist
	@rm -f data/mesa_universe.csv data/divipol_dictionary.csv data/index.csv
	@echo "distcleaned."
