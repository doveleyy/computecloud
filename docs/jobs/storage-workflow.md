# Getting Files Into a Batch Job

> Administrator storage workflow. Member-safe Home/Shared browsing is
> implemented behind a disabled feature flag and must remain disabled until
> the Synology ACL acceptance test passes.

The first storage-backed workflow uses the `HomeStorage` Samba share. It keeps
projects and datasets out of browser upload forms while preserving the same
PBS-style job contract that a future dedicated NAS will use.

This Pi-hosted share is a migration bridge. In the end state the dedicated NAS
is the only SMB server; the same logical project/input references resolve there
without requiring a second general file share on the coordinator.

## Share layout

```text
HomeStorage/
├── projects/   project folders containing submit.hp and called scripts
├── inputs/     administrator-managed workload inputs during the transition
├── shared/     deliberately reusable household files
└── artifacts/  completed job output; read-only through Samba
```

Connect to the share with Finder, Windows Explorer, or the iOS/iPadOS Files app
using the private server name supplied by the operator. Uploads happen through
SMB, not through Job Desk, so ordinary file-copy tools handle large files and
folders.

## Submit from Job Desk

1. Copy the complete project folder into `projects/`.
2. Copy each external input file into `inputs/` or `shared/`.
3. Open Job Desk and choose **PBS-style project array**.
4. Choose **HomeStorage project folder**, select **Browse**, and choose the
   project directory.
5. Leave the entrypoint as `submit.hp`, unless the project uses another safe
   project-relative name.
6. For every `#HP --input NAME` declaration, enter the declared name, choose
   **Add file**, and select the matching regular file:

   `cohort` may point to `inputs/cohort.csv`, for example, while `reference`
   may point to `shared/reference.fa`.

7. Choose automatic placement or a specific worker and submit.

The platform packages the project folder into a bounded immutable ZIP. Input
files are not copied into upload staging. Each becomes a reference containing
the logical storage ID, safe relative path, exact size, and SHA-256. Workers
verify those fields before making the file visible at
`$HOME_PLATFORM_INPUT_DIR/NAME`.

## Submit from the CLI

The project may still be local while its data is already in HomeStorage:

```bash
pixi run client submit-batch ./cohort-analysis \
  --input-storage cohort=inputs/cohort.csv \
  --input-storage reference=shared/reference.fa
```

CLI and Job Desk create the same `BatchSubmissionCreate` contract and the same
parent/child records.

## Current boundaries

- HomeStorage inputs are regular files. Directory inputs are the next storage
  contract extension.
- Application member accounts are owner-scoped. Until Synology ACLs pass the
  two-user denial test, their storage routes return `503` and the picker is
  disabled. After the operator enables the feature, members see virtual
  `Home/...` and `Shared/...` paths; the server maps `Home` to the signed-in
  account's stable UUID and rejects paths outside those roots.
- Do not rename or edit an input after submission. If its bytes no longer match
  the recorded digest, the worker fails safely instead of running changed data.
- Because the present SSD is physically attached to the coordinator, its
  authenticated API serves selected file bytes to workers. They are streamed
  rather than placed in SQLite or copied to microSD staging. A dedicated NAS
  should later resolve the same logical reference directly to workers.
- Project archives remain limited to 20 MiB compressed, 100 MiB expanded, and
  1,000 entries. Put large data in named inputs, not inside the project.

Job Desk uses the authenticated storage browse API for a visual folder/file
picker. Paths are still validated and resolved by the server; hiding a path in
the browser is never treated as an authorization boundary.
