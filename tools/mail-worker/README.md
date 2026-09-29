# Odysseus mail listener (Cloudflare Email Worker)

Mail sent to an address you route here, for example `submissions@clevernode.org`, is:

1. forwarded to your Gmail, like a normal Email Routing rule;
2. saved in the R2 bucket `odysseus-mail`, where Odysseus picks it up within a minute.

Odysseus downloads each message, deletes it from the bucket, and handles it by
your rule in **Settings › Email › Inbound mail**:

- **notify**: posts it in a "Mail" chat and sends a notification;
- **task**: also has the agent work on it, following your instructions for that address.

Odysseus is only reachable on your tailnet, so the Worker can't push mail to it.
Odysseus calls the Worker instead, with a secret.

## Setup (once, about 10 minutes)

From this folder, logged in to Cloudflare (`npx wrangler login`):

```sh
npx wrangler r2 bucket create odysseus-mail
# Set FORWARD_TO in wrangler.toml to your Gmail address, then:
npx wrangler deploy
openssl rand -hex 32            # copy this; it is the pull secret
npx wrangler secret put PULL_SECRET
```

`wrangler deploy` prints the Worker URL, `https://odysseus-mail.<you>.workers.dev`.

In the Cloudflare dashboard:

1. Go to **clevernode.org › Email › Email Routing › Destination addresses**. Check your Gmail is listed and verified.
2. Go to **Routing rules › Create address**. Enter `submissions`, choose the action **Send to a Worker**, and pick `odysseus-mail`.
3. Optional: go to **R2 › odysseus-mail › Settings › Object lifecycle rules** and delete objects after 30 days. This only matters for mail Odysseus never collected, for example while it was off for a long time.

In Odysseus, go to **Settings › Email › Inbound mail** and:

1. paste the Worker URL and the secret;
2. check the rules;
3. click **Save**, then **Check now**.

## Tests

```sh
node --test worker.test.mjs
```
