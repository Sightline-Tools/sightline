# Sightline release notes

Each public release uses the reviewed Markdown file named for its tag, such as
`v0.1.2.md`. The public release workflow publishes that file verbatim instead
of generating a changelog from Gatekeeper transport pull requests.

Release notes should:

- begin with a `## Highlights` section and at least one useful bullet;
- explain user-visible features, fixes, and distribution changes in plain
  language;
- include `## Fixes` or `## Distribution notes` only when those sections add
  useful context;
- describe the actual public diff from the previous release; and
- avoid internal repository details, operator identities, placeholders, and
  automated publication pull-request titles.

The release orchestrator validates the versioned file before creating a public
tag. Missing, stale, or placeholder notes fail the release before publication.
