import logging
import sys
from logging.handlers import RotatingFileHandler

def setup_logger(log_file):
    logger = logging.getLogger('my_app_logger')
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        # Rotate at 10 MB, keep 5 backups
        file_handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8')
        file_handler.setLevel(logging.INFO)

        # threadName lets you grep per-chunk lines when multiple threads run simultaneously
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s')
        file_handler.setFormatter(formatter)

        logger.addHandler(file_handler)

        console_handler = logging.StreamHandler(stream=open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False))
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger

logger = setup_logger('prod_live_35.log')
