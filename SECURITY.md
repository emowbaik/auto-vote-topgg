# Security Policy

## Supported Version

Security fixes apply to latest commit on `master`.

## Reporting a Vulnerability

Do not open public issue containing credentials, cookies, screenshots, or exploit details. Use [GitHub private vulnerability reporting](https://github.com/emowbaik/auto-vote-topgg/security/advisories/new) for this repository.

Include affected commit, reproduction steps without live credentials, impact, and suggested mitigation if known.

## Credential Handling

This project handles Discord user tokens and top.gg Auth.js session cookies.

- Store credentials only in GitHub Actions Secrets or local secret files.
- Never commit `.env`, screenshots, browser profiles, or cookie exports.
- Use only ephemeral, single-tenant GitHub-hosted runners.
- Do not run on persistent/shared self-hosted runners.
- Rotate credentials immediately after suspected exposure.

Workflow secrets are handed to Python through mode-`0600` temporary files. Python unlinks those files, removes credential environment variables, then launches Chrome. Each browser uses temporary profile explicitly deleted after termination.

## Security Controls

- Immutable GitHub Action SHAs.
- Hash-locked Python dependencies.
- Weekly Dependabot updates.
- OSV dependency audit in CI.
- Secret scanning and push protection.
- Browser sandbox for non-root runners.
- Every Auth.js cookie forced to `Secure` during injection.
- Every Auth.js session-token cookie forced to `HttpOnly` during injection.
- Credential redaction in normal diagnostics.
- Separate write-capable workflow cleanup job without user credentials.

## Audit Log

### 2026-08-07

Fixed browser credential inheritance, profile retention, weak cookie attributes, Requests CVE-2026-25645, transitive dependency integrity, and excess browser-job permissions.

Repository protection uses pull requests and required CI. Independent human approval remains unavailable while repository has only one trusted collaborator; add second trusted collaborator before requiring one approval.
