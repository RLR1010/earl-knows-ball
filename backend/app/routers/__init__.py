import logging

# Lazy load routers - import inline to avoid circular imports at module level
logger = logging.getLogger("uvicorn")
logger.info("Routers package loaded")
