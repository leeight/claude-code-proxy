import logging
import os
from logging.handlers import RotatingFileHandler
from src.core.config import config

# Parse log level - extract just the first word to handle comments
log_level = config.log_level.split()[0].upper()

# Validate and set default if invalid
valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
if log_level not in valid_levels:
    log_level = 'INFO'

# Create logs directory if it doesn't exist
log_dir = os.path.dirname(config.log_file_path)
if log_dir and not os.path.exists(log_dir):
    os.makedirs(log_dir, exist_ok=True)

# Create logger
logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, log_level))

# Create formatter
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

# File handler with rotation
file_handler = RotatingFileHandler(
    config.log_file_path,
    maxBytes=config.log_file_max_bytes,
    backupCount=config.log_file_backup_count,
    encoding='utf-8'
)
file_handler.setLevel(getattr(logging, log_level))
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Console handler (optional, based on config)
if config.log_to_console:
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, log_level))
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

# Also configure root logger for other modules
logging.basicConfig(
    level=getattr(logging, log_level),
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[file_handler] + ([console_handler] if config.log_to_console else [])
)

# Configure uvicorn to be quieter
for uvicorn_logger in ["uvicorn", "uvicorn.access", "uvicorn.error"]:
    logging.getLogger(uvicorn_logger).setLevel(logging.WARNING)