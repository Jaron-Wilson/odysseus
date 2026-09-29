// Odysseus mail listener: a Cloudflare Email Worker.
//
// Asked for: "can i get it to have a listener on the website and also email
// routing to my gmail too?" Odysseus is only reachable on the tailnet, so
// mail cannot be pushed to it. This Worker takes the mail instead:
//
//   email  -> forwards it to FORWARD_TO (your Gmail) as Email Routing did,
//             and keeps a copy in the MAIL R2 bucket;
//   fetch  -> lets Odysseus list, download and delete those copies, with the
//             PULL_SECRET as a bearer token. Odysseus pulls every minute.
//
// Bindings (wrangler.toml): MAIL (R2 bucket), FORWARD_TO (a verified
// destination address), PULL_SECRET (set with `wrangler secret put`).

const KEY_RE = /^\d{15}-[0-9a-f-]{36}\.eml$/;
const LIST_LIMIT = 100;

export function newKey(now = Date.now()) {
  return `${String(now).padStart(15, '0')}-${crypto.randomUUID()}.eml`;
}

// Compare without stopping at the first differing byte.
export function secretMatches(expected, supplied) {
  if (!expected || !supplied) return false;
  const a = new TextEncoder().encode(expected);
  const b = new TextEncoder().encode(supplied);
  let diff = a.length ^ b.length;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ (b[i % (b.length || 1)] || 0);
  return diff === 0;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
  });
}

export async function handleEmail(message, env) {
  const raw = await new Response(message.raw).arrayBuffer();
  const key = newKey();
  const meta = {
    from: String(message.from || '').slice(0, 300),
    to: String(message.to || '').slice(0, 300),
    subject: String((message.headers && message.headers.get('subject')) || '').slice(0, 300),
  };
  let stored = false;
  try {
    await env.MAIL.put(key, raw, { customMetadata: meta });
    stored = true;
  } catch (e) {
    console.log(`could not store ${key}: ${e}`);
  }
  if (env.FORWARD_TO) {
    try {
      await message.forward(env.FORWARD_TO);
    } catch (e) {
      console.log(`could not forward to ${env.FORWARD_TO}: ${e}`);
      // Kept for Odysseus, so it is not lost; otherwise let the sender know.
      if (!stored) message.setReject('Temporary failure, please try again later');
    }
  } else if (!stored) {
    message.setReject('Temporary failure, please try again later');
  }
  return { key, stored };
}

export async function handleFetch(request, env) {
  if (!env.PULL_SECRET) return json({ error: 'PULL_SECRET is not set on this Worker' }, 503);
  const auth = request.headers.get('authorization') || '';
  const token = auth.toLowerCase().startsWith('bearer ') ? auth.slice(7).trim() : '';
  if (!secretMatches(env.PULL_SECRET, token)) return json({ error: 'unauthorized' }, 401);

  const url = new URL(request.url);
  const parts = url.pathname.replace(/\/+$/, '').split('/').filter(Boolean);
  if (parts[0] !== 'messages' || parts.length > 2) return json({ error: 'not found' }, 404);

  if (parts.length === 1) {
    if (request.method !== 'GET') return json({ error: 'method not allowed' }, 405);
    const listed = await env.MAIL.list({ limit: LIST_LIMIT, include: ['customMetadata'] });
    const messages = (listed.objects || [])
      .filter((o) => KEY_RE.test(o.key))
      .map((o) => ({ key: o.key, size: o.size, ...(o.customMetadata || {}) }))
      .sort((a, b) => a.key.localeCompare(b.key));
    return json({ messages, truncated: !!listed.truncated });
  }

  const key = decodeURIComponent(parts[1]);
  if (!KEY_RE.test(key)) return json({ error: 'bad key' }, 400);
  if (request.method === 'GET') {
    const obj = await env.MAIL.get(key);
    if (!obj) return json({ error: 'not found' }, 404);
    return new Response(obj.body, { headers: { 'content-type': 'message/rfc822', 'cache-control': 'no-store' } });
  }
  if (request.method === 'DELETE') {
    await env.MAIL.delete(key);
    return json({ deleted: key });
  }
  return json({ error: 'method not allowed' }, 405);
}

export default {
  async email(message, env) { await handleEmail(message, env); },
  async fetch(request, env) { return handleFetch(request, env); },
};
