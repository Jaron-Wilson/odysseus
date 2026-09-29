// node --test tools/mail-worker/worker.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { handleEmail, handleFetch, secretMatches, newKey } from './worker.js';

function fakeBucket() {
  const m = new Map();
  return {
    m,
    async put(k, v, o) { m.set(k, { body: v, customMetadata: o.customMetadata, size: v.byteLength }); },
    async get(k) { const o = m.get(k); return o ? { body: o.body } : null; },
    async delete(k) { m.delete(k); },
    async list() { return { objects: [...m].map(([key, o]) => ({ key, size: o.size, customMetadata: o.customMetadata })), truncated: false }; },
  };
}

function fakeMessage(text, { failForward = false } = {}) {
  const msg = {
    from: 'gateway@clevernode.org', to: 'submissions@clevernode.org',
    headers: new Map([['subject', 'New pages']]),
    raw: new Blob([text]).stream(),
    forwarded: [], rejected: null,
    async forward(to) { if (failForward) throw new Error('no'); msg.forwarded.push(to); },
    setReject(r) { msg.rejected = r; },
  };
  return msg;
}

const req = (path, method = 'GET', token = 's3cret') => new Request(`https://w.example${path}`, {
  method, headers: token ? { authorization: `Bearer ${token}` } : {} });

test('keys sort by arrival time', () => {
  assert.ok(newKey(1000) < newKey(2000));
  assert.match(newKey(), /^\d{15}-[0-9a-f-]{36}\.eml$/);
});

test('secret compare', () => {
  assert.ok(secretMatches('abc', 'abc'));
  assert.ok(!secretMatches('abc', 'abd'));
  assert.ok(!secretMatches('abc', 'abcd'));
  assert.ok(!secretMatches('abc', ''));
  assert.ok(!secretMatches('', ''));
});

test('email is stored and forwarded to Gmail', async () => {
  const env = { MAIL: fakeBucket(), FORWARD_TO: 'me@gmail.com' };
  const msg = fakeMessage('Subject: New pages\r\n\r\nhello');
  const r = await handleEmail(msg, env);
  assert.ok(r.stored);
  assert.deepEqual(msg.forwarded, ['me@gmail.com']);
  const o = env.MAIL.m.get(r.key);
  assert.equal(o.customMetadata.to, 'submissions@clevernode.org');
  assert.equal(o.customMetadata.subject, 'New pages');
  assert.equal(new TextDecoder().decode(o.body), 'Subject: New pages\r\n\r\nhello');
  assert.equal(msg.rejected, null);
});

test('a failed forward still keeps the copy; nothing kept and nothing forwarded rejects', async () => {
  const env = { MAIL: fakeBucket(), FORWARD_TO: 'me@gmail.com' };
  const msg = fakeMessage('x', { failForward: true });
  await handleEmail(msg, env);
  assert.equal(env.MAIL.m.size, 1);
  assert.equal(msg.rejected, null);
  const broken = { MAIL: { async put() { throw new Error('r2 down'); } }, FORWARD_TO: 'me@gmail.com' };
  const msg2 = fakeMessage('x', { failForward: true });
  await handleEmail(msg2, broken);
  assert.ok(msg2.rejected);
});

test('pull API needs the secret, then lists, downloads and deletes', async () => {
  const env = { MAIL: fakeBucket(), PULL_SECRET: 's3cret' };
  const { key } = await handleEmail(fakeMessage('body one'), env);
  assert.equal((await handleFetch(req('/messages', 'GET', null), env)).status, 401);
  assert.equal((await handleFetch(req('/messages', 'GET', 'wrong'), env)).status, 401);
  const list = await (await handleFetch(req('/messages'), env)).json();
  assert.equal(list.messages.length, 1);
  assert.equal(list.messages[0].key, key);
  assert.equal(list.messages[0].from, 'gateway@clevernode.org');
  const one = await handleFetch(req(`/messages/${key}`), env);
  assert.equal(await one.text(), 'body one');
  assert.equal((await handleFetch(req('/messages/..%2Fsecret'), env)).status, 400);
  assert.equal((await handleFetch(req(`/messages/${key}`, 'DELETE'), env)).status, 200);
  assert.equal(env.MAIL.m.size, 0);
  assert.equal((await handleFetch(req('/other'), env)).status, 404);
});

test('no secret configured refuses everything', async () => {
  const r = await handleFetch(req('/messages'), { MAIL: fakeBucket() });
  assert.equal(r.status, 503);
});
