# Script Authoring Guides

This page is retained so existing links continue to work. Job authoring is now
organized by job type:

- [Python script job](jobs/python-script.md) — the implemented `python_batch`
  contract: one Python file, one CSV input, and a fixed scientific runtime.
- [Batch script job](jobs/batch-script.md) — the live numeric-array `batch`
  contract: a PBS-like `#HP` header, Bash entrypoint, project bundle, multiple
  logical inputs, named runtime, arrays, and dependencies.

See the [job types index](jobs/README.md) for help choosing a contract.
