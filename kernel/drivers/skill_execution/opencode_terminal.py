"""Apply a non-secret Skill overlay without persisting the native configuration."""
import json
import os
import sys


def merge(left, right):
    result = dict(left) if isinstance(left, dict) else {}
    for key, value in right.items():
        result[key] = merge(result.get(key), value) if isinstance(value, dict) else value
    return result


if __name__ == '__main__':
    with open(sys.argv[1]) as stream:
        data = json.load(stream)
    config = json.loads(os.environ.get('OPENCODE_CONFIG_CONTENT') or '{}')
    permissions = json.loads(os.environ.get('OPENCODE_PERMISSION') or '{}')
    config = merge(config, data['overlay'])
    plugins = list(config.get('plugin') or [])
    for plugin in data.get('plugins', []):
        if plugin not in plugins:
            plugins.append(plugin)
    config['plugin'] = plugins
    config['permission'] = merge(config.get('permission'), {'skill': data['skill']})
    permissions['skill'] = data['skill']
    os.environ['OPENCODE_CONFIG_CONTENT'] = json.dumps(config)
    os.environ['OPENCODE_PERMISSION'] = json.dumps(permissions)
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
