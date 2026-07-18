# Encounter lifecycle architecture note

The encounter engine is intentionally driven by **committed `SessionLogLine` rows** (accepted parser output), never by raw OCR frame visibility.

## Event flow

1. OCR frame is deduplicated and parsed.
2. Newly committed lines are persisted to `session_log_lines`.
3. Each committed accepted line is passed to `EncounterEngine.process_committed_line`.
4. The engine reconciles timeout, starts/updates/ends encounters, and writes `events` linked by `session_log_line_id`.

## Lifecycle state diagram

```text
[No Active Encounter]
  --(damage_out|damage_in|miss_out|miss_in)--> [Active Encounter]

[Active Encounter]
  --(combat activity event)--> [Active Encounter] (refresh last_activity_at)
  --(death_self)-------------> [Ended: death]
  --(manual end)-------------> [Ended: manual]
  --(now - last_activity >= timeout)--> [Ended: timeout]

[Ended Encounter]
  --(merge with others)------> [Merged Encounter record (recomputed via events)]
  --(delete)------------------> [Removed encounter row, events cascade deleted]
```

## Merge/delete behavior

- Merge reassigns all `events.encounter_id` rows from secondaries into a primary encounter and recalculates aggregates at read time.
- Delete removes the encounter and visibility preferences only; continuous log (`session_log_lines`) remains intact.

## Charm state and attribution

- Accepted `charm_start` and `charm_end` lines update parser-session-scoped `charm_windows` before encounter filtering. Charm state therefore survives pre-combat gaps, encounter timeouts, and encounter splits.
- Window boundaries use both timestamps and source line IDs, so events captured in the same OCR frame retain log order. Overlapping same-name starts remain separate; each end closes one active window.
- Solo and party filtering retains damage involving an active Charm window. Generic NPC-versus-NPC damage cannot start a party encounter; while a valid party encounter is active, it may be retained only as a weak candidate and does not refresh encounter activity.
- Encounter details report tagged and active-window damage as attributed, apply a 50% midpoint only to same-name collisions, and expose unrelated NPC combat separately under `potential_charm_damage`. Potential candidates are not included in the Charm estimate.
- `CharmWindow` rows are reconstructed idempotently from accepted historical session lines at database startup. Encounter rows also snapshot the capture filter mode for provenance.

## Replay compatibility

Replay uses the same parser session and line-commit path, so replay frames feed the **same** encounter engine logic as live capture.
