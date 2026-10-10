---
name: apple-artwork-operator
description: Inspect, download, and resume public Apple Music visual artwork with the installed apple-artwork CLI. Use for operating the tool, not for modifying its source.
---

# Operate Apple Music Artwork

Use this tool for public playlist, album, artist, and song images or supported motion artwork. Keep the resource, region, language, quality, and output location requested by the user. It does not download music, authenticate a private library, bypass DRM, or transcode videos.

## Discover the installed interface

Run `apple-artwork capabilities --json` and check the version, `data.protocol`, command arguments, and exit codes. This guide targets protocol 1. Prefer complete option names. `apple-music-artwork` and `python -m apple_music_artwork` are equivalent entry points.

Run `apple-artwork doctor --json` to check local capabilities. Static downloads need Pillow; motion inspection also needs the optional `motion` dependency; motion download needs FFmpeg and ffprobe. The optional `ui` dependency changes presentation only. Use the user's existing environment; installing dependencies is a separate environment change.

## Inspect before writing

To discover fields without downloading media or creating task state:

```sh
apple-artwork 'APPLE_MUSIC_URL' --list --json --no-input
```

To plan the requested export:

```sh
apple-artwork plan 'APPLE_MUSIC_URL' --all-media --library output --json --no-input
```

Replace `APPLE_MUSIC_URL` with the real public URL. Preserve an explicit `l` language parameter and the link's storefront. Omit `--all-media` for the rule-selected primary image; use `--all-static`, `--all-motion`, `--asset-id`, or `--semantic` only when the requested scope warrants it.

The plan is a schema 1 envelope; its `data` contains the plan. Preflight may fetch resource metadata, bounded HLS text, and HEAD responses, but no media body. A complete existing task can be checked and reused without network. Plan output is a description of proposed actions, not permission to make unrequested changes.

## Execute an authorized plan

Save the plan JSON in a caller-managed file, then use:

```sh
apple-artwork download --plan plan.json --json --no-input
```

When a plan offers decisions, choose only an offered action consistent with the user's authorization. Copy the exact `plan_id`, `decision_id`, `action`, and offered `params` into a decisions file:

```json
{"protocol_version":1,"choices":[]}
```

Populate `choices` with the complete choice objects. Submit it using `--decisions decisions.json` with the saved plan or the specific resume command. Do not invent IDs, infer consent from explanatory text, reuse an expired choice, or silently lower quality. If the plan changed, inspect the new plan and obtain any newly required user choice.

`--library DIR` organizes resources and task versions below that root. Ordinary reruns continue the latest explicitly created version, including an unfinished version. `--new-version` requests fresh retrieval while retaining prior versions. `-o DIR` instead means that exact task directory and is mutually exclusive with `--library`; do not create an adjacent directory without an explicit destination. Never delete locks or alter task state to force a rerun.

## Read outcomes and resume

Check both the process exit code and the single stdout JSON. Download results retain schema 2 and include `control`. Exit 0 means the command succeeded, not that every possible visual asset is available: also inspect warnings, errors, `coverage`, individual downloads, and `manifest_report`.

An explicit `skip` resolves that choice and may allow exit 0 with `control.lifecycle: completed`. Check the skipped count and each `status: skipped`: the original selection remains recorded and coverage stays incomplete. Earlier failures belong to history or skip reasons; do not report skipped media as downloaded or reused.

- Exit 8: a decision is required and the pending state was saved. Inspect `control.decisions` or `tasks show`; ask the user only for choices their request did not already authorize.
- Exit 9: the user explicitly chose to defer, and that state was saved. Respect the recorded retry time.
- Exit 130: cancelled. Completed files remain; let the process finish its shutdown before resuming.
- Exit 1–7: the reported input, authentication, network, selection, file, or validation failure. Do not claim that state was saved unless the result proves it.

Use `apple-artwork tasks list --library output --json`, then `apple-artwork tasks show TASK --library output --json` or `apple-artwork resume TASK --library output --json --no-input`. Use the returned full task reference. For an exact directory, use `resume --task-dir DIR`. Reuse verifies existing files; unfinished files restart from byte zero. Do not overwrite a modified file or weaken validation to obtain success.

Task listing is read-only and includes resource names, singular types, region/language, selection scope, snapshot time, and full `taskID@version` references. Unknown snapshot times are null. Filter only `tasks list` with `--query TEXT` (case-insensitive name or resource/task ID substring), `--type playlist|album|artist|song`, repeated `--status STATUS` (union), and `--latest` (highest explicit version per task before filtering). By default all versions are listed. Inspect both row errors and `data.errors`: damaged registrations do not mean the library is empty, and damaged rows cannot be selected.

With all three standard streams on an interactive terminal, `resume --library output` offers numbered selection; the wizard's third option uses the same entry. Numbers bind to the exact version in that displayed list and identity is rechecked before execution. A valid pending metadata recovery is marked `recovery_required`; it can be selected. Empty lists, q, blank input, EOF, or a two-minute timeout execute nothing. JSON, pipes, `--no-input`, and `--events-jsonl` require a full task reference or exact task directory and return input error 2 when missing. Internal selection bindings are not public JSON fields.

`apple-artwork diagnostics --task-dir DIR --json` returns bounded, sanitized diagnostics. Do not request tokens, cookies, raw HTTP dumps, or arbitrary tool stderr. `--events-jsonl` emits progress only on stderr as JSONL and keeps stdout as one final JSON document. It disables interactive input, as do `--json`, `--no-input`, and redirected standard streams. Human menus time out after two minutes without making a choice; this differs from an explicit defer.

## Compare versions and check remote declarations

Use `apple-artwork tasks diff LEFT RIGHT --library output --json` for a read-only offline comparison of the same task. Both references must include the complete task ID and an explicit `@version`. A successful comparison returns 0 even when differences or pure comparison ambiguities exist.

Use `apple-artwork tasks check-updates TASK --library output --json` or replace the reference with `--task-dir DIR`; never provide both locators. The task's original provider, region, language, selection and quality options remain fixed. For a catalog task, `--token-env NAME` selects this invocation's credential environment variable; it does not change the provider, persist credentials or change task identity. There are no response/provider/selection/quality overrides. An offline imported source returns input error 2 with `limitations` reason `offline_source`, without switching provider.

These queries use schema 1. Read `data.outcome` (`observed_change`, `no_observed_change`, `indeterminate`), `changes`, `ambiguities`, all `limitations`, `network_scope`, and envelope errors. A failed query retains available comparison data and its mapped nonzero exit code. Each change has `kind`, `semantic_key`, `field`, and `before/after:{available,present,value}`. Unavailable observations, absent records, null and empty values are different. Missing scan-completion evidence cannot prove absence. Known differences remain visible when other evidence is indeterminate.

Field states cover the target response; assets and motion cover the original selected scope, excluding related assets. Exact identity or unique semantic pairing is required; address-bearing IDs alone do not prove additions or deletions. Address changes do not prove content changes, unchanged declarations do not prove identical media bytes, and request failures do not mean deletion. Metadata guards block declared or recognized media before reading, but cannot rule out a server mislabelling media as allowed text.

Update checks request only resource metadata and necessary HLS/HEAD. They do not request media bodies, decode media, allocate versions or modify saved tasks. `network_scope` reflects requests actually attempted; the remote right side contains an observation time, not an invented persistent version. Unresolved exact selection does not inspect a replacement motion source; primary selection does not follow a new fallback. To retrieve a new version, use the original requested options with `plan --new-version`, then `download --plan`. Ordinary resume continues its saved snapshot without new freshness requests; existing pending HLS/HEAD completion remains possible.

For an offline parsing check, use a caller-provided synthetic JSON response with `--response FILE --list --json`. `--response` replaces resource metadata only: adding motion inspection or downloading may still access HLS and media URLs. Never call a fabricated media URL a successful download test.
