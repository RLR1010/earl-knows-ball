import logging

# Lazy load routers - import inline to avoid circular imports at module level
logger = logging.getLogger(__name__)
logger.info("Routers package loaded")
