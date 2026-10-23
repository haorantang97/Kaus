"""Early ACP checks for native OpenCode-family Skill slash commands."""
from __future__ import annotations

import re
import shutil

from drivers.base import UnsupportedCapabilityError


def remember(plan, catalog, config, *, aliases=False):
    # Native command definitions take precedence over same-named Skills.
    # Partial Kilo command overrides do not replace the Skill template.
    configured = config.get('command', {})
    explicit = {name for name, value in (configured.items() if isinstance(configured, dict) else ())
                if isinstance(value, dict) and isinstance(value.get('template'), str)}
    explicit.update({'init', 'review', 'compact'})
    if aliases:
        explicit.update({'goal', 'resume-claude', 'resume-codex'})
    plan.native_skill_commands = set(getattr(plan, 'native_skill_commands', ())) | {
        row['name'] for row in catalog if isinstance(row, dict) and isinstance(row.get('name'), str)}
    plan.native_user_commands = set(getattr(plan, 'native_user_commands', ())) | explicit
    plan.native_skill_aliases = aliases


def blocked(plan, name):
    if getattr(plan, 'native_skill_aliases', False) and name.endswith(':skill'):
        return name[:-6] not in plan.names
    return (name not in getattr(plan, 'native_user_commands', ())
            and name in getattr(plan, 'native_skill_commands', ()) and name not in plan.names)


def filter_update(plan, update):
    if update.get('sessionUpdate') != 'available_commands_update':
        return update
    commands = update.get('availableCommands')
    if not isinstance(commands, list):
        return update
    return {**update, 'availableCommands': [entry for entry in commands
            if not isinstance(entry, dict) or not blocked(plan, str(entry.get('name', '')))]}


async def validate(plan, prompt, cwd, *, inspect):
    # This mirrors the native ACP detector: join text blocks without a separator,
    # trim outer whitespace, then take the first whitespace-delimited token.
    text = ''.join(part.get('text', '') for part in prompt
                   if isinstance(part, dict) and part.get('type') == 'text'
                   and isinstance(part.get('text'), str))
    # JavaScript trim/\s also accept BOM; Python str.strip/re.\s do not.
    text = re.sub(r'^[\s\ufeff]+|[\s\ufeff]+$', '', text)
    if not text.startswith('/'):
        return
    name = re.split(r'[\s\ufeff]+', text[1:], maxsplit=1)[0]
    if not name:
        return
    if blocked(plan, name):
        raise UnsupportedCapabilityError('技能不在当前项目范围内')
    if name in plan.names or name in getattr(plan, 'native_user_commands', ()):
        return
    # Only unrecognized slash commands require a fresh scan. This catches a
    # newly installed Skill without adding CLI work to ordinary conversation.
    executable = shutil.which(plan.spec.command[0], path=plan.spec.build_env().get('PATH'))
    if not executable:
        raise UnsupportedCapabilityError('无法核验当前技能命令')
    config = await inspect(executable, plan.spec, cwd, 'config')
    catalog = await inspect(executable, plan.spec, cwd, 'skill')
    remember(plan, catalog, config, aliases=getattr(plan, 'native_skill_aliases', False))
    if blocked(plan, name):
        raise UnsupportedCapabilityError('技能不在当前项目范围内')
