from types import SimpleNamespace
import yaml
import pytest

from drivers.account_quota import QuotaUnavailable
from drivers.dsh import quota


def value(summary):
    return {'code': 0, 'data': {'biz_code': 0, 'biz_data': summary}}


def test_wallet_currencies_and_grants_remain_separate():
    row = quota.normalize_wallets(value({'normal_wallets': [{'currency': 'CNY', 'balance': '2.5'}, {'currency': 'USD', 'balance': '0E-16'}], 'bonus_wallets': [{'currency': 'CNY', 'balance': '.5'}]}), key='test')
    assert [(b.remaining,b.currency) for b in row.balances] == [(2.5,'CNY'),(0,'USD'),(.5,'CNY')]
    assert row.balances[0].label != row.balances[2].label


@pytest.mark.parametrize('payload', [{}, value({}), value({'normal_wallets': [], 'bonus_wallets': []}), value({'normal_wallets': [{'currency':'CNY','balance':None}], 'bonus_wallets': []})])
def test_missing_wallet_is_unknown(payload):
    with pytest.raises(QuotaUnavailable): quota.normalize_wallets(payload,key='test')


def test_business_auth_refusal():
    with pytest.raises(QuotaUnavailable) as exc: quota.normalize_wallets({'code':0,'data':{'biz_code':40003}},key='test')
    assert exc.value.reason == 'credential_expired'


def connection(tmp_path, issuer='https://platform.deepseek.com'):
    root=tmp_path/'native';root.mkdir()
    path=root/'.credentials.yaml'
    path.write_text(yaml.safe_dump({'version':1,'records':{quota.RECORD:{'kind':'grant','payload':{'version':1,'issuer':issuer,'token':'test-token'}}}}))
    driver=SimpleNamespace(agent_spec=SimpleNamespace(build_env=lambda:{'HOME':str(tmp_path),'DSH_HOME':str(root)}))
    return driver,path


async def test_native_grant_query_is_readonly(tmp_path,monkeypatch):
    driver,path=connection(tmp_path);before=path.read_bytes()
    async def query(method,url,**kwargs):
        assert method=='GET' and url=='https://platform.deepseek.com/api/v0/users/get_user_summary'
        assert kwargs['headers']['x-dsh-auth-token']=='test-token'
        assert 'Authorization' not in kwargs['headers']
        return value({'normal_wallets':[{'currency':'CNY','balance':'4'}],'bonus_wallets':[]})
    monkeypatch.setattr(quota,'request_json',query)
    result=await quota.read_account_quota(driver)
    assert result.balances[0].remaining==4 and path.read_bytes()==before
    assert 'test-token' not in repr(result)


@pytest.mark.parametrize('issuer',['https://relay.example','http://platform.deepseek.com','https://platform.deepseek.com.evil.test','https://name@platform.deepseek.com'])
async def test_non_official_issuer_never_receives_token(tmp_path,monkeypatch,issuer):
    driver,_=connection(tmp_path,issuer)
    async def never(*args,**kwargs): raise AssertionError('network request')
    monkeypatch.setattr(quota,'request_json',never)
    with pytest.raises(QuotaUnavailable) as exc: await quota.read_account_quota(driver)
    assert exc.value.reason=='unsupported_provider'


async def test_api_key_only_user_has_no_duplicate_signin_issue(tmp_path):
    driver,path=connection(tmp_path)
    path.write_text('version: 1\nrefs:\n  DEEPSEEK_API_KEY: fixture\n')
    result=await quota.read_account_quota(driver)
    assert result.accounts==result.issues==()
