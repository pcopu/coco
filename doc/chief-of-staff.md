# Chief of Staff GitHub ingress

The Chief of Staff integration consumes immutable directive files from the
private `pcopu/chief-of-staff` repository.

## Security model

- GitHub reaches only a path-specific HTTPS Funnel route.
- The HTTP receiver binds `127.0.0.1:8788` and validates the raw request body
  against `X-Hub-Signature-256` with a private HMAC-SHA256 secret.
- Only `push` events for the exact repository and `refs/heads/main` are valid.
- Only newly added `directives/YYYY/MM/dir_*.json` files are accepted.
- Modifying or deleting a directive causes the delivery to be rejected.
- SQLite persistence happens before the HTTP `202` response.
- Repository files are fetched through fixed-argument `gh api` calls and are
  parsed as data. No repository code or workflow is executed.
- Delivery into CoCo uses the existing authenticated controller RPC secret.
- The external durable inbox retains work while General is busy. An RPC result
  that may have been dispatched but lost is placed in `manual_review` rather
  than replayed.

## Environment

Copy `deploy/systemd/chief-of-staff.env.example` to
`~/.coco/chief-of-staff.env`, generate a webhook secret, and set mode `0600`.
The CoCo cluster secret remains in `~/.coco/.env`.

## Operations

```bash
python -m coco.chief_of_staff status
python -m coco.chief_of_staff reconcile
python -m coco.chief_of_staff drain
curl http://127.0.0.1:8788/healthz
```

The production Funnel route is path-specific and must preserve existing routes:

```bash
tailscale funnel --bg --set-path /chief-of-staff http://127.0.0.1:8788
```

With that route, GitHub posts to:

```text
https://userver.tail1adb41.ts.net/chief-of-staff/github/push
```

The hourly reconciliation timer is a safety net for missed push deliveries; it
does not execute repo-defined Actions or require a self-hosted runner.
