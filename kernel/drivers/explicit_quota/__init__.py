"""Explicit billing connections, separate from inference credentials."""
from .catalog import catalog, validate


async def read_connection(provider, config, secrets):
    if provider in ('volcengine', 'tencent'):
        from .cloud import read_connection as read
    elif provider in ('aihubmix', 'openrouter'):
        from .management import read_connection as read
    elif provider == 'stepfun-plan':
        from .stepfun import read_connection as read
    else:
        from .custom import read_connection as read
    return await read(provider, config, secrets)
