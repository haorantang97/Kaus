"""Per-process memory policy; reuse the installed ACP adapter and its routing."""


def supported(spec):
    return True


def prepare(spec, role):
    # Keep the configuration directory, provider variables and login intact.
    # This documented switch disables both automatic memory reading and writing.
    return spec.with_env(CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
