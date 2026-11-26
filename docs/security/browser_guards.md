# Browser guards, and what the planning report's security items look like on main

**Depends on nothing** (based on main). Two new switches, both OFF by default; no migration. Not deployed.

## What was checked against main
| Item | On main | What this change does |
|---|---|---|
| State-changing routes reject a foreign Origin (CSRF) | **Not present.** The session cookie is `SameSite=None` (the frontend is on another site). CORS stops a foreign page reading answers and a JSON body forces a preflight, but a request with no body or a `text/plain` body is sent anyway. | New `CSRF_ORIGIN_CHECK` (off). |
| Login throttle and rate limits | Present (`LOGIN_THROTTLE_ENABLED`, `RATE_LIMIT_ENABLED`, both off by default, set by the owner). Tests existed. | No code change. The test names are pinned by `test_every_other_security_item_still_has_its_named_tests`, so deleting one fails the suite. |
| Telegram secret-token check | Present (`TELEGRAM_WEBHOOK_SECRET`, unset by default = accepts every request). Tests existed. | No change. Pinned the same way. |
| API docs off outside development | The switch exists (`DISABLE_API_DOCS`), **default is docs ON**, and a test covers the off case. | Not defaulted off (that would change today's behaviour). A start-up warning is logged when the docs are served and `FRONTEND_ORIGIN` is https; a test covers it. Set `DISABLE_API_DOCS=true` in production. |
| Single-instance lock | Present (`API_SINGLE_INSTANCE_LOCK`, and `main.py`). Tests existed. | No change. Pinned. |
| Security headers | **Not present** in the app. | New `SECURITY_HEADERS` (off). |

## `CSRF_ORIGIN_CHECK=true`
For POST, PUT, PATCH and DELETE (other than the three provider callbacks, which are server-to-server and have their own signature or secret):
* an `Origin` that is not `FRONTEND_ORIGIN` (compared without case or trailing slash) or is `null` gets 403;
* no `Origin` but `Sec-Fetch-Site: cross-site` gets 403;
* no `Origin` and no `Sec-Fetch-Site` passes (a command-line client or a server cannot carry a victim's cookie).
Reads and the CORS preflight are not touched. Needs `FRONTEND_ORIGIN` to be exactly the frontend's origin (scheme, host, port): a wrong value locks the real frontend out of every write, so check it with one login and one save after turning it on. Rollback: unset it and restart.

## `SECURITY_HEADERS=true`
Adds, on every answer including errors and refusals (never overwriting a header a route set): `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'` (not on `/docs`, `/redoc`, `/openapi.json`, which need their own scripts), `Cache-Control: no-store` for `/api/` and `Strict-Transport-Security` when the request arrived over https. These are API answers only; the frontend's own headers are set where it is hosted (Vercel). Behind Nginx uvicorn sees plain http unless proxy headers say otherwise, so HSTS is better set once in Nginx (below). Rollback: unset and restart.

## Owner steps by hand (nothing here was applied)
**1. The real client address (already applied on 25 Oct per `README.md`; re-check).** In the Nginx API location the line must be an overwrite: `proxy_set_header X-Forwarded-For $remote_addr;` (not `$proxy_add_x_forwarded_for`). Behind Cloudflare, `conf.d/cloudflare-realip.conf` needs `real_ip_header CF-Connecting-IP;` plus one `set_real_ip_from <range>;` per current Cloudflare range (https://www.cloudflare.com/ips/). uvicorn must trust forwarded headers only from `127.0.0.1` (its default `--forwarded-allow-ips`). The test in `README.md` ("Runbook entry") proves it: a fake `X-Forwarded-For` sent straight to the origin must not appear as the logged address. Re-check after any Cloudflare range notice.

**2. Cloudflare SSL/TLS mode Full (strict).** In the Cloudflare dashboard for the zone: SSL/TLS, Overview, mode **Full (strict)**. This needs a valid certificate on the origin (a Let's Encrypt certificate, or a Cloudflare Origin CA certificate installed in Nginx), otherwise visitors see a 526 error: install the certificate and test `curl -I https://<api-host>` from a machine that is not behind Cloudflare using `--resolve` BEFORE switching the mode. Optional hardening in the same place: Always Use HTTPS, Minimum TLS 1.2, and HSTS (Cloudflare's own switch, or in Nginx: `add_header Strict-Transport-Security "max-age=15552000; includeSubDomains" always;`). "Flexible" mode leaves the hop from Cloudflare to the server in plain text and must not be used.

**3. Order.** Turn the guards on one at a time, in a quiet period: `SECURITY_HEADERS=true`, restart, load the site; then `CSRF_ORIGIN_CHECK=true`, restart, sign in and change one setting. Both can be switched off the same way.

## Not proven
Nginx, Cloudflare and real browsers were not involved: the tests use the FastAPI test client. The Origin rule assumes the browser sends `Origin` on cross-site unsafe requests, which current browsers do.
