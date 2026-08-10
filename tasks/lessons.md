# Lessons

- Before marking a repository task complete, verify open PR count, pending/failed required checks, and open security alerts. Background completion is not task completion.
- GitHub may redact a numeric timestamp job output when secret scanning classifies it as sensitive. For cross-job control state, use an artifact and verify the downstream job receives the value during a live run; artifact upload alone is insufficient proof.
- GitHub workflow disable is not idempotent and disabled workflows may not resolve by display name in `gh run list`. Read current state before disabling and use stable workflow filenames/IDs for lifecycle cleanup.
