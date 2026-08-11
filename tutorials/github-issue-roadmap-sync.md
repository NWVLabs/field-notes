# Durable GitHub Issue-to-Roadmap Synchronization

GitHub Projects is an excellent view, but issue bodies and labels make a more
durable planning record. This pattern keeps authoritative workflow state in the
repository and treats a Project as a rebuildable mirror.

## Data contract

Give every managed issue one status label and a marker-owned date block:

```markdown
<!-- roadmap-dates:start -->
## Roadmap dates

| Field | Date |
|---|---|
| Issue Created | 2026-01-15 |
| Estimated Start | 2026-02-01 |
| Actual Start |  |
| Target Date | 2026-02-28 |
| Actual Completion |  |

<!-- roadmap-dates:end -->
```

Use mutually exclusive labels such as `status: backlog`, `status: todo`,
`status: in progress`, `status: blocked`, and `status: done`. Normalize labels
on issue events; closing an issue should select `status: done` and populate
Actual Completion without erasing the other dates.

## One-way synchronization

Start with one direction:

```text
issue body + labels -> workflow -> Project fields
```

The workflow should list managed issues, parse only the marker-owned block,
look up Project field and option IDs by name, add a missing issue once, and
update Status plus the five dates. Query before mutation so reruns do not
create duplicate items.

Use `workflow_dispatch` with a dry-run input. A useful summary reports scanned,
added, changed, unchanged, skipped, and failed counts. In apply mode, stop on
ambiguous fields or malformed dates instead of partially inventing state.

## Permissions and ownership

Repository issue permissions do not automatically grant access to an
organization- or user-owned Project. Prefer a GitHub App or fine-grained token
when it supports the required ownership model. If a classic token is required,
document its broader scope, store it only as an encrypted Actions secret, and
rotate it. Never print credentials or authorization headers.

## Planned versus actual views

Build two views from the same durable record:

- Planned Roadmap: Estimated Start and Target Date
- Actual Delivery: Actual Start and Actual Completion

Do not overwrite estimates when work begins; preserving both pairs makes drift
and forecasting quality visible.

## Validation checklist

Test in a disposable repository and Project. Run dry-run, apply, and apply
again; the second apply must add nothing and change nothing. Then test missing
items, malformed dates, status changes, issue closure, user-owned and
organization-owned Projects, revoked credentials, and recovery after a partial
API failure.

## Build it without painting yourself into a corner

### Phase 1: establish the issue contract

Create the five status labels first. Put the date block in one test issue and
manually edit it twice. Stop if another template, bot, or workflow rewrites the
same markers; two owners for one block will eventually destroy data.

The parser should enforce `YYYY-MM-DD`, allow a blank value, reject duplicate
rows, and leave all text outside the markers byte-for-byte unchanged. Missing
markers should mean “unmanaged,” not “replace the whole issue body.”

### Phase 2: normalize status labels

On `issues` events, remove every recognized status label except the selected
one. Use this precedence for repair runs:

```text
closed issue -> done
blocked -> blocked
in progress -> in progress
todo -> todo
backlog or no status -> backlog
```

Ignore pull requests and the workflow's own metadata issue. Add a default
assignee only when an issue has none. This makes the normalizer safe to rerun.

### Phase 3: resolve Project metadata before writing

GitHub Projects uses opaque node and option IDs. Query the Project by owner and
number, then build maps from field names and single-select option names. Fail
before mutation if Status, any date field, or any required option is missing.

Do not paste IDs into source. They change when a Project or field is recreated.
Also distinguish user-owned and organization-owned Projects at configuration
time; querying the wrong owner type often looks like a permissions failure.

### Phase 4: plan, then apply

For each issue, produce a plan object before making API calls:

```json
{
  "issue": 17,
  "projectItem": "existing-or-null",
  "changes": {
    "Status": "In progress",
    "Actual Start": "2026-02-03"
  },
  "warnings": []
}
```

Dry-run prints these plans. Apply mode adds a missing item once, then updates
only fields whose current values differ. Keep item lookup by issue content ID
in a map so pagination does not create duplicates.

### Workflow skeleton

```yaml
name: Synchronize roadmap

on:
  workflow_dispatch:
    inputs:
      apply:
        description: Apply changes (false is a dry run)
        type: boolean
        default: false
  issues:
    types: [opened, edited, labeled, unlabeled, closed, reopened]

permissions:
  contents: read
  issues: read

jobs:
  synchronize:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 24
      - run: node tools/sync-roadmap.mjs
        env:
          GH_TOKEN: ${{ secrets.ROADMAP_TOKEN }}
          ROADMAP_APPLY: ${{ inputs.apply || github.event_name != 'workflow_dispatch' }}
          ROADMAP_OWNER: <owner>
          ROADMAP_OWNER_TYPE: organization
          ROADMAP_PROJECT_NUMBER: "<number>"
```

The default `GITHUB_TOKEN` is repository-scoped and may not be able to update a
separately owned Project. Start with read-only permissions in the workflow and
grant the external credential only what the tested ownership model requires.

### Audit summary and recovery

Always write a job summary containing counts and per-issue failures. If apply
fails halfway through, fix the cause and rerun; idempotence should converge the
remaining items without duplicating successful ones. Never “recover” by
deleting all Project items or rewriting all issue bodies.

| Symptom | Likely cause | First check |
|---|---|---|
| Project not found | wrong owner type or number | query owner and Project separately |
| Resource not accessible | credential scope/installation | test a read-only Project query |
| Status will not update | option name or ID mismatch | print resolved field/option map |
| Duplicate cards | add performed before existing-item scan | paginate and map content IDs |
| Dates disappear | blank treated as delete unintentionally | distinguish absent, blank, and set |
| Workflow loops | bot edits retrigger unconditionally | skip writes when desired state matches |

### Final acceptance gate

Run on at least three test issues: unmanaged, fully managed, and malformed.
Capture the first dry-run, first apply, and second apply summaries. The second
apply must report zero additions and zero field changes. Revoke the credential
temporarily and confirm failure is explicit and no issue body is damaged.

> Publication review: validate the reusable workflow example against the
> current GitHub Projects GraphQL schema and credential model before moving
> this field note to `main`.
