# Documentation

- [Architecture](architecture.md) — system design, job lifecycle, leases,
  scheduling behaviour, isolation model, trust boundaries, failure behaviour.
- [Job and API contract](job-contract.md) — job types, state machine, worker
  protocol, endpoints, authentication, resource limits.
- [Configuration](configuration.md) — every environment variable, what it does,
  and which ones matter.

These documents describe design and reasoning only. They intentionally contain
no hostnames, addresses, accounts, or filesystem paths — anything tied to a
particular deployment lives outside this repository.

## Conventions

- Describe behaviour as **implemented**, **deployed**, **live-verified**, or
  **pending**. These are not interchangeable, and planned architecture must not
  be written as though it already runs.
- Never commit credentials, addresses, hostnames, hardware identifiers, or
  machine inventory.
