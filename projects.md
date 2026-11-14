# Project profiles

> These three projects are fictional examples. Replace them with your own projects before the
> first real run. The whole file is sent to the model as-is, so write it for a reader who knows
> nothing about your work.

A repo is "worth trying" only if it fits one of these categories:
- security: an advisory or exploit affecting something listed under a project's Stack
- replace: a mature library or tool that replaces code I hand-rolled (see "Hand-rolled")
- pain: it addresses a listed pain point
- capability: it adds something listed under Wishlist to an existing project

Last full refresh: 2026-01-01.

## invoice-app

Invoicing and time-tracking SaaS for freelancers. Single Postgres database, multi-tenant by
workspace id.
- Stack: Next.js 15 (App Router), React, TypeScript, Tailwind CSS, Prisma, PostgreSQL (managed),
  Auth.js with email magic links, Paddle for subscriptions, hosted on Vercel.
- Hand-rolled: PDF invoice rendering from HTML templates; a fixed-window rate limiter in
  middleware; CSV import of time entries from other tools.
- Pain points: PDF generation is slow and times out on large invoices; end-to-end tests are
  flaky; tax rules for EU VAT are a growing pile of if-statements.
- Wishlist: multi-currency invoices; an audit log of who changed what; a customer portal for
  paying invoices.

## weather-pipeline

Nightly Python batch job that downloads public weather station data, cleans it and publishes
aggregated tables for a small analytics site.
- Stack: Python 3.12, Polars, DuckDB, Prefect for scheduling, S3-compatible object storage,
  runs as a container on a single VM.
- Hand-rolled: retry and backoff wrapper around the HTTP downloads; schema drift detection that
  compares column names against a JSON file; data quality checks written as plain asserts.
- Pain points: memory spikes on the largest yearly files; quality checks give no report, only
  a crash; no way to see which output table came from which input file.
- Wishlist: declarative data validation with readable reports; lightweight lineage tracking;
  incremental loads instead of full reloads.

## homelab

Self-hosted services on one mini PC at home, reachable from outside only through a VPN.
- Stack: Proxmox VE, Debian LXC containers, Jellyfin, Nextcloud, Home Assistant, Caddy as reverse
  proxy, WireGuard, restic backups to an off-site bucket, Uptime Kuma.
- Hand-rolled: shell scripts for backup rotation and for checking that backups can be restored;
  a cron job that updates containers one by one.
- Pain points: alerts are spread across three tools; the update cron job has broken Nextcloud
  twice with no easy way back; no record of what changed on the host.
- Wishlist: automatic updates with rollback; a small log aggregation setup that fits in 1 GB of
  RAM; single sign-on for the web services.
