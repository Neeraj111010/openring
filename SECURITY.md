# Security policy

OpenRing handles phone calls, provider API keys and call transcripts, so security reports are welcome
and taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Report it privately instead, either way:

- On GitHub: **Security → Report a vulnerability** in this repository.
- By email: **alan.rocha@realvalorai.com.br**.

Include what is affected, how to reproduce it and what an attacker could do. You'll get an answer within
3 business days, and we'll agree with you on when to publish the fix and the advisory. Reporters are
credited unless they prefer otherwise.

## Supported versions

Only the latest release gets security fixes while OpenRing is below 1.0.

## Running OpenRing safely

- Keep the server on `127.0.0.1` (the default) unless you need it reachable. If you expose it, set a
  strong dashboard password (`OPENRING_PASSWORD`) and a long `OPENRING_API_KEY`, and serve it over HTTPS.
  Without a key, the API answers only the computer it runs on; example keys like `change-me` are refused.
- Keep `TWILIO_AUTH_TOKEN` set: OpenRing uses it to reject webhooks that don't come from Twilio. Never set
  `VALIDATE_TWILIO_SIGNATURE=false` outside local tests.
- Provider keys live in `~/.openring/.env`; treat that file like a password.
- Treat what an assistant's tools receive as untrusted: the model fills in the arguments during a conversation
  with a stranger, and callers can steer it (prompt injection). Your tool server should check who is calling
  (`call.customer.number`), what they may do, and how much, and verify the `X-Vapi-Secret` you set as
  `server.secret`. Don't put secrets in the system prompt: a caller can talk the model into repeating it.
- OpenRing never calls cloud metadata or link-local addresses (169.254.169.254 and the like) for URLs that
  come from assistants, tools, phone numbers or imports.
