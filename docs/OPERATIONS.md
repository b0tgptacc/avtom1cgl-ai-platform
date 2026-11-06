# Operations

## Supported workflow

1. Make changes in a branch.
2. Run unit tests and image builds locally or in CI.
3. Merge only reviewed changes with a green CI run.
4. Create a versioned release artifact through the manual release workflow.
5. Verify the artifact signature/integrity before host deployment.
6. Run health checks and an approved smoke test after deployment.

## Required operational controls

- Monitor service readiness, error rate, restart count, memory, disk and backup success.
- Keep only the approved host ports exposed.
- Use encrypted off-host backups and test restore regularly.
- Maintain a documented rollback release and incident procedure.
- Treat agent evaluations, uploaded documents and generated files as governed corporate data with retention rules.

## Explicitly out of scope for the initial release workflow

The repository's release workflow does not SSH into or automatically modify production. Automatic deployment will be added only after deployment identity, protected environments, rollback and release verification are approved.
