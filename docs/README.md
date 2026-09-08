# Documentation

- [Architecture](architecture.md) — system design, job lifecycle, leases,
  scheduling behaviour, isolation model, trust boundaries, failure behaviour.
- [Job and API contract](job-contract.md) — job types, state machine, worker
  protocol, endpoints, authentication, resource limits.
- [Writing batch scripts](script-authoring.md) — how contributors read inputs,
  write artifacts, choose parallelism, and stay within execution limits.
- [Configuration](configuration.md) — every environment variable, what it does,
  and which ones matter.
- [Web interfaces](interfaces.md) — current dashboard and Job Desk behaviour,
  responsive design goals, and the next UI scope.

These documents describe design and reasoning only. They intentionally contain
no private deployment identifiers, addresses, accounts, or machine paths.
Generic application defaults may appear where they are part of the public
configuration contract; live deployment details stay outside version control.

## Conventions

- Describe behaviour as **implemented**, **deployed**, **live-verified**, or
  **pending**. These are not interchangeable, and planned architecture must not
  be written as though it already runs.
- Never commit credentials, addresses, hostnames, hardware identifiers, or
  machine inventory.
