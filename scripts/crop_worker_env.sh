# Source before cropping on a worker machine. Example dept slice for PC 2:
#   source scripts/crop_worker_env.sh
export E14_CROP_WORKERS="${E14_CROP_WORKERS:-12}"   # ~ nproc - 4
export E14_DEPT_FROM="${E14_DEPT_FROM:-21}"
export E14_DEPT_TO="${E14_DEPT_TO:-33}"
export E14_WORKER_ID="${E14_WORKER_ID:-$(hostname -s)}"
export E14_CROP_OUTPUT_DIR="${E14_CROP_OUTPUT_DIR:-data/detector_national}"
export E14_CROP_INPUT_DIR="${E14_CROP_INPUT_DIR:-data/actas}"
