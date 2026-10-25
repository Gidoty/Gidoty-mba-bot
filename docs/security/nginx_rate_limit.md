# Nginx rate limits (proposed, not applied)

> Update 2026-10-25: the owner has already applied the Cloudflare real-IP and `X-Forwarded-For` overwrite described in `README.md` ("Runbook entry"). The zone and `location` sketches below are still only a proposal.

Nothing in this file has been applied. The owner edits Nginx. Adapt names to your existing site file (I have not seen it). Keep your existing `proxy_pass` and
`proxy_set_header` lines inside each `location`; the sketches below show only what is added.

## 1. Know where the real client address comes from

* If the API subdomain is **not** behind Cloudflare, `$binary_remote_addr` is the client.
* If it **is** behind Cloudflare, Nginx sees Cloudflare's addresses. Add `real_ip_header CF-Connecting-IP;` and one `set_real_ip_from <range>;` line per current Cloudflare range
  (https://www.cloudflare.com/ips/) in the `http` or `server` block, or every visitor shares a few addresses and the limit hits real customers.

## 2. Zones (in the `http {}` context, for example a new file `/etc/nginx/conf.d/mba_rate_limit.conf`)

```
limit_req_zone $binary_remote_addr zone=mba_auth:10m rate=10r/m;
limit_req_zone $binary_remote_addr zone=mba_api:10m  rate=120r/m;
limit_req_status 429;
```

## 3. Locations (in the existing `server {}` for the API; place the exact-match ones before the generic `/api/` one)

```
location = /api/auth/login  { limit_req zone=mba_auth burst=5 nodelay; <your existing proxy_pass and proxy_set_header lines> }
location = /api/auth/signup { limit_req zone=mba_auth burst=5 nodelay; <your existing proxy_pass and proxy_set_header lines> }
location = /api/billing/webhook  { <existing lines, NO limit_req> }
location = /api/telegram/webhook { <existing lines, NO limit_req> }
location /api/ { limit_req zone=mba_api burst=60 nodelay; <your existing proxy_pass and proxy_set_header lines> }
```

## 4. Apply (owner, one step at a time)

1. `sudo cp -a /etc/nginx/sites-enabled /root/nginx-sites-backup-$(date +%F)` and the same for `conf.d`.
2. Make the edit. 3. `sudo nginx -t` (must say syntax is ok). 4. `sudo systemctl reload nginx` (reload, not restart).
5. Test: `for i in $(seq 1 20); do curl -s -o /dev/null -w "%{http_code} " -X POST https://<api>/api/auth/login -H 'content-type: application/json' -d '{"email":"x@example.com","password":"wrongwrong1"}'; done` should show 401s, then 429s.
6. Test the webhooks still answer (a signed Paystack test event, or the Telegram link flow).
Rollback: restore the backed-up files, `sudo nginx -t`, `sudo systemctl reload nginx`.

## 5. Before turning on the in-app limiter

The app keys on the address uvicorn reports. Check it is the real one: first run `curl -s https://<api>/api/health` from your own machine, then `journalctl -u mba-bot -n 20 | grep "GET /api/health"`; the address in front of the request line must be a real client or
monitoring address, **not** `127.0.0.1`. If it is `127.0.0.1`, Nginx is not forwarding `X-Forwarded-For` (or uvicorn does not trust it): fix that first, or leave `RATE_LIMIT_ENABLED` off and rely on Nginx.
