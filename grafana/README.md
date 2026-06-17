# pat — Grafana integration

Visualise the `pat export` metrics (and `pat annotate` markers) in Grafana.

## Dashboard

[`pat-dashboard.json`](pat-dashboard.json) is importable directly
(**Dashboards → New → Import**). It covers forecast, cost, and per-layer views in
one board: observed-vs-predicted demand (with the ARIMA CI band), recommended
replicas per layer, response time, throughput gain, and cost-per-request.

## Provisioning bundle (auto-load, no clicking)

[`provisioning/`](provisioning/) holds Grafana file-provisioning configs so the
datasource and dashboard load automatically on startup — point Grafana at them
instead of importing by hand.

Mount paths:

| Repo path | Mount in Grafana |
|---|---|
| `grafana/provisioning/` | `/etc/grafana/provisioning/` |
| `grafana/pat-dashboard.json` | `/var/lib/grafana/dashboards/pat/` |

Example (docker):

```bash
docker run -d --name grafana -p 3000:3000 \
  -e PROMETHEUS_URL=http://prometheus:9090 \
  -v "$PWD/grafana/provisioning:/etc/grafana/provisioning:ro" \
  -v "$PWD/grafana/pat-dashboard.json:/var/lib/grafana/dashboards/pat/pat-dashboard.json:ro" \
  grafana/grafana:latest
```

The datasource URL defaults to `http://prometheus:9090` and honours
`PROMETHEUS_URL`. Run `pat export` (with `--predict` and
`--cost-per-replica-hour` for the full board) and scrape it from Prometheus.

## Annotations

`pat annotate` posts the current recommendation to Grafana's annotation API so it
appears as a marker on the dashboards:

```bash
export PAT_GRAFANA_TOKEN=...   # editor token; env only, never a flag
pat annotate -r 500 -l 80 -c container --grafana https://grafana.example.com
```

`pat` only ever posts an informational annotation — it never changes
infrastructure. The token is read from `PAT_GRAFANA_TOKEN` only, HTTPS is
required (use `--insecure-http` for localhost), and `--dry-run` previews the
payload without posting.
