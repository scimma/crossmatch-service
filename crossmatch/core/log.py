import structlog


def get_logger(name):
    return structlog.get_logger(name)
