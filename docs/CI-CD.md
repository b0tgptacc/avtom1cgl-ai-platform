# Initial CI/CD

## Continuous integration

`.github/workflows/ci.yml` runs on pull requests, pushes to `main` and manual dispatch. It:

- blocks tracked secret-bearing paths and private-key headers;
- validates Python syntax and shell syntax;
- renders Docker Compose with non-secret CI placeholders;
- builds custom service images. Their Dockerfiles run their embedded unit tests.

The CI workflow does not use production credentials and does not contact the corporate model endpoint.

## Initial continuous delivery foundation

`.github/workflows/release.yml` is manual only. It packages the exact selected Git revision and creates a GitHub release artifact. It does not deploy.

A production deployment workflow requires a separate approval after these prerequisites exist:

- protected GitHub environment;
- dedicated deployment identity and host-key pinning;
- encrypted secrets in GitHub Actions or an external secret manager;
- verified release artifact and deploy manifest;
- backup/rollback gate;
- post-deploy health and acceptance verification.
