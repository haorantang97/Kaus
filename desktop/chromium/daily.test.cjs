const test = require('node:test');
const assert = require('node:assert/strict');
const d = require('./daily.cjs');

test('origin stays on loopback http', () => {
  assert.equal(d.origin(undefined), 'http://127.0.0.1:8877');
  assert.equal(d.origin('http://127.0.0.1:3187'), 'http://127.0.0.1:3187');
  for (const bad of ['https://127.0.0.1:3187', 'http://localhost:3187', 'http://example.com:8877', 'http://u@127.0.0.1:1234', 'http://127.0.0.1:8877/x', 'nonsense'])
    assert.equal(d.origin(bad), 'http://127.0.0.1:8877');
  assert.equal(d.startPath('/conversations/c1'), '/conversations/c1');
  for (const bad of ['//evil.test', 'https://evil.test', '/\\evil', '/a b', undefined]) assert.equal(d.startPath(bad), '/');
});

test('menu follows the macOS language; both languages name every item', () => {
  assert.equal(d.menuLanguage(['zh-Hans-CN', 'en-GB']), 'zh');
  assert.equal(d.menuLanguage(['en-GB', 'zh-Hans-CN']), 'en');
  assert.equal(d.menuLanguage([]), 'en');
  assert.deepEqual(Object.keys(d.MENU_TEXT.zh).sort(), Object.keys(d.MENU_TEXT.en).sort());
});

test('zoom steps and layout scaling', () => {
  assert.equal(d.nextZoom(1, 1), 1.1); assert.equal(d.nextZoom(1, -1), 0.9);
  assert.equal(d.nextZoom(2, 1), 2); assert.equal(d.nextZoom(0.75, -1), 0.75); assert.equal(d.nextZoom(1.5, 0), 1);
  assert.deepEqual(d.scaleBounds({ x: 10, y: 20, width: 100, height: 50 }, 1.25), { x: 12.5, y: 25, width: 125, height: 62.5 });
  assert.equal(d.scaleBounds(null, 2), null);
});

// Same cases as the former Python capture_session tests (kernel/drivers/explicit_quota/tests/test_stepfun.py).
const jwt = claims => `h.${Buffer.from(JSON.stringify(claims)).toString('base64url')}.s`;
const session = (activated = true) => ({ access: jwt({ activated, oasis_id: 'acct-1' }), refresh: jwt({ oasis_id: 'acct-1', device_id: 'dev-1' }) });
const cookieFor = s => ({ name: 'Oasis-Token', value: `${s.access}...${s.refresh}`, domain: '.stepfun.ai', path: '/', secure: true });

test('quota sign-in reads only this site’s secure Oasis-Token after activation', () => {
  const s = session(), cookie = cookieFor(s), page = d.stepfunLoginUrl('ai');
  assert.deepEqual(d.stepfunTokens('ai', page, [cookie]), s);
  assert.equal(d.stepfunTokens('com', d.stepfunLoginUrl('com'), [cookie]), null);
  assert.equal(d.stepfunTokens('ai', 'https://platform.stepfun.ai.evil.test/', [cookie]), null);
  assert.equal(d.stepfunTokens('ai', 'https://platform.stepfun.ai:444/', [cookie]), null);
  for (const change of [{ domain: '.evil.test' }, { name: 'other' }, { secure: false }, { path: '/other' }])
    assert.equal(d.stepfunTokens('ai', page, [{ ...cookie, ...change }]), null);
  assert.equal(d.stepfunTokens('ai', page, [cookieFor(session(false))]), null);
  const noDevice = { access: s.access, refresh: jwt({ oasis_id: 'acct-1' }) };
  assert.equal(d.stepfunTokens('ai', page, [cookieFor(noDevice)]), null);
  const otherAccount = { access: s.access, refresh: jwt({ oasis_id: 'acct-2', device_id: 'dev-1' }) };
  assert.equal(d.stepfunTokens('ai', page, [cookieFor(otherAccount)]), null);
  assert.equal(d.stepfunTokens('evil', page, [cookie]), null);
  assert.ok(d.LOGIN_ID.test('a'.repeat(32))); assert.ok(!d.LOGIN_ID.test('short')); assert.ok(!d.LOGIN_ID.test('a'.repeat(31) + '/'));
});
