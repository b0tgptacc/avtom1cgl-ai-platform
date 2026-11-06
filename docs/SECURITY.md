# Security Baseline

## Secrets and sensitive data

Never commit `.env`, certificates, private keys, uploads, database dumps, agent data, logs, reports or backup archives. CI checks the tracked snapshot for common secret-bearing paths and private-key headers.

Secrets must be injected at deployment time. `.env.example` may contain variable names and placeholders only.

## Agent controls

Agent runtimes are high-risk execution environments. They must use non-root identities, no Docker socket, no public egress by default, least-privilege mounts, resource limits, and a documented approval policy.

## PII handling

Text must traverse the PII guard before outbound inference. A masking outage must block outbound requests rather than bypass privacy controls. Non-text inputs must be locally transformed to safe text or rejected.

## Releases and recovery

Only versioned, reviewed releases may be deployed. Release/update/restore bundles must be integrity-authenticated before extraction, build or execution. Backups contain sensitive material and require encryption, off-host retention and restore drills.

## Vulnerability management

Before production promotion, run an image/dependency scan and record remediation or risk acceptance. CI is not a substitute for a vulnerability-management process.
