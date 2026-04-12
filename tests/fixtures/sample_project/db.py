import config


def connect(settings=None):
    if settings is None:
        settings = config.load()
    return f"db://{settings['host']}:{settings['port']}"
