"""Connection forms and validation for the billing capability."""
from copy import deepcopy
import math
import re
from urllib.parse import urlsplit


class ConnectionInvalid(ValueError):
    def __init__(self, reason='invalid_fields'):
        self.reason = reason
        super().__init__(reason)


def field(key, label, kind='text', required=True, **extra):
    return dict(key=key, label=label, type=kind, required=required, **extra)


def options(*pairs):
    return [dict(value=value, label=label) for value, label in pairs]


PROVIDERS = [
    dict(id='stepfun-plan', label='StepFun Plan', kind='plan',
         fields=[field('site', '站点', 'select', options=options(('com', '中国站'), ('ai', '国际站')), default='com')],
         login=dict(kind='embedded', label='登录 StepFun'), docsUrl='https://platform.stepfun.com/step-plan'),
    dict(id='volcengine', label='火山方舟', kind='plan', fields=[
        field('region', '区域', default='cn-beijing'),
        field('plan', '计划', 'select', options=options(('both', '全部计划'), ('coding', 'Coding Plan'), ('agent', 'Agent Plan')), default='both'),
        field('accessKeyId', 'Access Key ID', 'password'), field('secretAccessKey', 'Secret Access Key', 'password')],
        docsUrl='https://console.volcengine.com/iam/keymanage/'),
    dict(id='tencent', label='腾讯 TokenHub', kind='plan', fields=[
        field('region', '区域', 'select', options=options(('ap-guangzhou', '中国大陆'), ('ap-singapore', '国际站')), default='ap-guangzhou'),
        field('teamId', '团队 ID', required=False, placeholder='自动读取'),
        field('secretId', 'SecretId', 'password'), field('secretKey', 'SecretKey', 'password')],
        docsUrl='https://console.cloud.tencent.com/cam/capi'),
    dict(id='aihubmix', label='AiHubMix', kind='balance', fields=[field('accessToken', '管理令牌', 'password')],
         docsUrl='https://console.aihubmix.com/setting'),
    dict(id='openrouter', label='OpenRouter', kind='balance', fields=[field('managementKey', '管理密钥', 'password')],
         docsUrl='https://openrouter.ai/settings/management-keys'),
    dict(id='custom', label='自定义余额接口', kind='balance', fields=[
        field('url', '查询地址', placeholder='https://example.com/api/balance'),
        field('remainingPath', '余额字段', default='data.balance'),
        field('currency', '币种', 'select', options=options(('CNY', '人民币'), ('USD', '美元'), ('EUR', '欧元')), default='CNY'),
        field('multiplier', '换算比例', required=False, default='1', advanced=True),
        field('authScheme', '认证方式', 'select', options=options(('bearer', 'Bearer Token'), ('raw', 'Authorization'), ('x-api-key', 'X-API-Key'), ('none', '无需认证')), default='bearer', advanced=True),
        field('userId', '用户 ID', required=False, advanced=True),
        field('method', '请求方式', 'select', options=options(('GET', 'GET'), ('POST', 'POST')), default='GET', advanced=True),
        field('apiKey', '查询密钥', 'password', required=False)],
        docsUrl=None),
]
SECRET_FIELDS = {'stepfun-plan': {'access', 'refresh', 'webId', 'account'}}
SECRET_FIELDS.update({p['id']: {f['key'] for f in p['fields'] if f['type'] == 'password'} for p in PROVIDERS if p['id'] != 'stepfun-plan'})


def catalog():
    return {'providers': deepcopy(PROVIDERS)}


def provider_spec(provider):
    return next((p for p in PROVIDERS if p['id'] == provider), None)


def validate(provider, config, secrets, *, login=False):
    spec = provider_spec(provider)
    if not spec or not isinstance(config, dict) or not isinstance(secrets, dict):
        raise ConnectionInvalid()
    allowed = {f['key'] for f in spec['fields'] if f['type'] != 'password'}
    if set(config) - allowed or set(secrets) - SECRET_FIELDS[provider]:
        raise ConnectionInvalid()
    cleaned = {}
    for f in spec['fields']:
        source = secrets if f['type'] == 'password' else config
        value = source.get(f['key'], f.get('default', ''))
        if not isinstance(value, str) or len(value) > (16384 if f['type'] == 'password' else 2048):
            raise ConnectionInvalid()
        value = value.strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ConnectionInvalid()
        if f['required'] and not value:
            raise ConnectionInvalid()
        if f.get('options') and value not in {o['value'] for o in f['options']}:
            raise ConnectionInvalid()
        if f['type'] != 'password':
            cleaned[f['key']] = value
    if provider == 'stepfun-plan' and not login:
        if not secrets.get('access') or not secrets.get('refresh'):
            raise ConnectionInvalid('sign_in_required')
        if any(not isinstance(v, str) or len(v) > 16384 or any(ord(c) < 32 for c in v) for v in secrets.values()):
            raise ConnectionInvalid()
    if provider == 'custom':
        try:
            url = urlsplit(cleaned['url'])
            if (url.scheme != 'https' or not url.hostname or url.username or url.password
                    or url.query or url.fragment or url.port not in (None, 443)):
                raise ValueError()
            factor = float(cleaned.get('multiplier') or '1')
            if not math.isfinite(factor) or factor <= 0 or factor > 1e12:
                raise ValueError()
        except ValueError:
            raise ConnectionInvalid() from None
        if not re.fullmatch(r'[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*', cleaned['remainingPath']):
            raise ConnectionInvalid()
        if cleaned['userId'] and not re.fullmatch(r'[0-9]{1,20}', cleaned['userId']):
            raise ConnectionInvalid()
        if cleaned['userId']:
            cleaned['userId'] = str(int(cleaned['userId']))
        cleaned['multiplier'] = str(factor)
        if cleaned['authScheme'] != 'none' and not secrets.get('apiKey'):
            raise ConnectionInvalid()
    return cleaned
