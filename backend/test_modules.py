from app.infrastructure.logging.logger import get_logger

logger_a = get_logger("app.domain")
logger_b = get_logger("app.application")
logger_c = get_logger("app.infrastructure")

logger_a.info("Domain")
logger_b.info("Application")
logger_c.info("Infrastructure")