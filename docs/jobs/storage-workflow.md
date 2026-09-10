# Getting Files Into a Batch Job

The first storage-backed workflow uses the `HomeStorage` Samba share. It keeps
projects and datasets out of browser upload forms while preserving the same
PBS-style job contract that a future dedicated NAS will use.

## Share layout

```text
HomeStorage/
├── projects/   project folders containing submit.hp and called scripts
├── inputs/     private workload inputs for the current single-user system
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
4. Choose **HomeStorage project folder** and enter its share-relative path,
   such as `projects/cohort-analysis`.
5. Leave the entrypoint as `submit.hp`, unless the project uses another safe
   project-relative name.
6. For every `#HP --input NAME` declaration, add one line under HomeStorage
   input bindings:

   ```text
   cohort=inputs/cohort.csv
   reference=shared/reference.fa
   ```

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
- The current deployment has one owner account, not isolated member homes.
- Do not rename or edit an input after submission. If its bytes no longer match
  the recorded digest, the worker fails safely instead of running changed data.
- Because the present SSD is physically attached to the coordinator, its
  authenticated API serves selected file bytes to workers. They are streamed
  rather than placed in SQLite or copied to microSD staging. A dedicated NAS
  should later resolve the same logical reference directly to workers.
- Project archives remain limited to 20 MiB compressed, 100 MiB expanded, and
  1,000 entries. Put large data in named inputs, not inside the project.

The authenticated storage browse API already lists safe entries, but the first Job Desk
form uses explicit relative paths. A visual picker is the next usability pass.
