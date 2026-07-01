# A2A Task-Type Registry (Contract 3)

> **Normative.** This file is the published task-type registry referenced by
> [`a2a/task-request.schema.json`](./task-request.schema.json). It is the single source of truth for
> the set of legal values of the `task_type` field on A2A task-request messages.

## Registry rule

- The `task_type` field on every A2A task request **MUST** be drawn from the table below.
- A request whose `task_type` is **not** in this registry is rejected by the receiver with a
  `VALIDATION_ERROR` (Contract 2 error format). Receivers do not silently accept or coerce unknown
  task types.
- **Adding a task type requires a PR** against this registry. The PR must add a row to the table
  (name, description, input shape note, output shape note). Task types are append-only within a
  contract major version — an existing task type's name and semantics are never repurposed; a
  breaking change to a task type is a contract version bump (`v1` → `v2`).
- `task_type` is matched **case-sensitively** against the `Task type` column (lower-case, with
  `-` separators where shown, e.g. `code-review`).
- The `input` / `output` shape notes below are advisory contracts for each task type. The
  request-level `input` object is still bounded by the Contract 3 rule that its total serialized
  size **MUST NOT exceed 256 KiB**.

## First-cycle task types

| Task type     | Description                                                        | Input shape (note)                                                              | Output shape (note)                                                                 |
|---------------|--------------------------------------------------------------------|---------------------------------------------------------------------------------|-------------------------------------------------------------------------------------|
| `research`    | Investigate a question across sources and return findings.         | `{ "query": string, "sources"?: string[], "depth"?: "shallow"\|"deep" }`        | `{ "findings": string, "citations": [{ "title": string, "url": string }] }`         |
| `summarise`   | Condense a body of text into a shorter summary.                    | `{ "text": string, "max_words"?: integer }`                                     | `{ "summary": string }`                                                             |
| `code-review` | Review a code diff or file and return structured comments.         | `{ "diff": string, "language"?: string }`                                       | `{ "comments": [{ "line"?: integer, "severity": string, "message": string }] }`     |
| `generate`    | Produce new content (text/code) from a prompt.                     | `{ "prompt": string, "format"?: string }`                                       | `{ "content": string }`                                                             |
| `classify`    | Assign one or more labels from a label set to an input.            | `{ "text": string, "labels": string[] }`                                        | `{ "label": string, "scores"?: { [label: string]: number } }`                       |
| `extract`     | Pull structured fields out of unstructured input.                  | `{ "text": string, "fields": string[] }`                                        | `{ "extracted": { [field: string]: string } }`                                      |
| `plan`        | Decompose a goal into an ordered set of executable steps.          | `{ "goal": string, "constraints"?: string[] }`                                  | `{ "steps": [{ "step": string, "depends_on"?: integer[] }] }`                       |
| `chat`        | Single conversational turn over a message history.                 | `{ "messages": [{ "role": "user"\|"assistant"\|"system", "content": string }] }` | `{ "message": { "role": "assistant", "content": string } }`                         |

## Notes

- The `input`/`output` shape columns describe the conventional structure carried inside the
  Contract 3 `input` object (request) and the Contract 3 `output` object (response). They are not
  separately schema-validated in Phase 0, but services SHOULD honour them so that task output can
  flow into downstream task input without translation.
- `?` denotes an optional field; `|` denotes an enum of allowed string values.
