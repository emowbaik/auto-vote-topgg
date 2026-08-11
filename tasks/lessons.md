# Lessons

- Before marking a repository task complete, verify open PR count, pending/failed required checks, and open security alerts. Background completion is not task completion.
- GitHub may redact a numeric timestamp job output when secret scanning classifies it as sensitive. For cross-job control state, use an artifact and verify the downstream job receives the value during a live run; artifact upload alone is insufficient proof.
- GitHub workflow disable is not idempotent and disabled workflows may not resolve by display name in `gh run list`. Read current state before disabling and use stable workflow filenames/IDs for lifecycle cleanup.
- A cleanup claim must state its scope. Before calling workflow history clean, count runs repository-wide and verify every workflow covered by the stated retention policy—not only the workflow that owns the cleanup job.
- Before relying on a dependency's convenience method, inspect its installed implementation and production logs for undeclared optional runtime requirements. nodriver 0.50.3 `verify_cf()` calls OpenCV-based `template_location()`; without hash-locked `opencv-python-headless`, no checkbox coordinates or click are produced.
