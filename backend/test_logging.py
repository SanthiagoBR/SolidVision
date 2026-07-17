from app.infrastructure.logging.logger import get_logger

logger = get_logger(__name__)

try:
    1 / 0
except ZeroDivisionError:
    logger.exception("Division by zero")