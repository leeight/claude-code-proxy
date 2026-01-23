import logging
import os
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
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

# Custom namer function for hourly log files
def hourly_namer(default_name):
    """
    Custom namer for hourly log files.
    Converts default name to format: yyyy-mm-dd-hh.proxy.log
    """
    # Get the base directory and filename from config
    base_dir = os.path.dirname(config.log_file_path)
    base_name = os.path.basename(config.log_file_path)

    # Extract the timestamp from the default rotated filename
    # TimedRotatingFileHandler adds .YYYY-MM-DD_HH-MM-SS to the filename
    if '.' in default_name and default_name != config.log_file_path:
        # This is a rotated file
        try:
            # Parse the rotation time from the filename
            # Format is typically: logs/proxy.log.2026-01-23_15-00-00
            parts = default_name.split('.')
            if len(parts) >= 3:
                timestamp_str = parts[-1]  # Get the last part (timestamp)
                # Parse YYYY-MM-DD_HH-MM-SS format
                dt = datetime.strptime(timestamp_str, '%Y-%m-%d_%H-%M-%S')
                # Format as yyyy-mm-dd-hh
                formatted_time = dt.strftime('%Y-%m-%d-%H')
                # Create new filename: logs/yyyy-mm-dd-hh.proxy.log
                new_name = os.path.join(base_dir, f"{formatted_time}.{base_name}")
                return new_name
        except (ValueError, IndexError):
            pass

    # Return default name if parsing fails
    return default_name

# File handler with time-based rotation (hourly)
# Keep logs for 1 week (7 days * 24 hours = 168 hours)
file_handler = TimedRotatingFileHandler(
    config.log_file_path,
    when='H',  # Rotate hourly
    interval=1,  # Every 1 hour
    backupCount=168,  # Keep last 7 days (168 hours)
    encoding='utf-8',
    utc=False  # Use local time
)
file_handler.namer = hourly_namer
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

# Configure httpx and httpcore to suppress DEBUG logs
# These libraries log CancelledError at DEBUG level, which is normal operation
# when clients disconnect or requests timeout, not actual errors
for http_logger in ["httpx", "httpcore"]:
    logging.getLogger(http_logger).setLevel(logging.WARNING)