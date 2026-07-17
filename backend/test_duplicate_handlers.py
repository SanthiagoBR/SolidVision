from app.infrastructure.logging.logger import get_logger

logger1 = get_logger("app.test")
logger2 = get_logger("app.test")
logger3 = get_logger("app.test")

logger1.info("Only one line should be written.")