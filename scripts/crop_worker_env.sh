# Source before cropping on a worker machine.
# Fleet mode (recommended): set E14_WORKER_ID to your WSL Tailscale name (e.g. legion-1).
#   source scripts/crop_worker_env.sh
#   nohup bash scripts/start_crop_fleet_worker.sh >> logs/crop_supervisor.log 2>&1 & disown
# Static slice (legacy): set E14_DEPT_FROM / E14_DEPT_TO and use start_crop_worker.sh
export E14_CROP_WORKERS="${E14_CROP_WORKERS:-12}"   # ~ nproc - 4
export E14_DEPT_FROM="${E14_DEPT_FROM:-21}"
export E14_DEPT_TO="${E14_DEPT_TO:-33}"
export E14_WORKER_ID="${E14_WORKER_ID:-$(hostname -s)}"
export E14_FLEET_WORKERS="${E14_FLEET_WORKERS:-ryzen9-1,legion-1}"
export E14_FLEET_COORDINATOR="${E14_FLEET_COORDINATOR:-ryzen9-1}"
export E14_CROP_OUTPUT_DIR="${E14_CROP_OUTPUT_DIR:-data/detector_national}"
export E14_CROP_INPUT_DIR="${E14_CROP_INPUT_DIR:-data/actas}"
