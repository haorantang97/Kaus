"""HTTP and ACP use one native provider-discovery capability."""
from types import SimpleNamespace
from drivers.acp.client import AcpAgentSpec
from drivers.acp.presets import PRESETS
from drivers.hermes.driver import HermesDriver
from drivers.provider_quota import discover_provider_sources


def test_http_and_acp_read_the_same_profile_account(tmp_path):
    root = tmp_path / 'native'
    root.mkdir()
    (root / 'config.yaml').write_text('model:\n  provider: deepseek\n')
    (root / '.env').write_text('DEEPSEEK_API_KEY=profile-account\n')
    spec = AcpAgentSpec(command=('unused',), env={'HOME': str(tmp_path), 'HERMES_HOME': str(root), 'DEEPSEEK_API_KEY': 'old-parent'}, inherit_env=False)
    http = HermesDriver(hermes_root=root)
    http.agent_spec = spec
    acp = SimpleNamespace(preset=PRESETS['hermes-acp'], agent_spec=spec)
    assert discover_provider_sources(http) == discover_provider_sources(acp)
    assert discover_provider_sources(http)[0].api_key == 'profile-account'
