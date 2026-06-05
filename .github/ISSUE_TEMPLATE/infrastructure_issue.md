---
name: Infrastructure / Deployment Issue
about: Report issues related to AWS, Docker Compose, MediaMTX, or Jitsi deployment
title: '[INFRA] '
labels: infrastructure
assignees: ''

---

## Describe the Infrastructure Issue
A clear and concise description of the issue affecting the deployment or infrastructure.

## Component Affected
- [ ] Docker Compose
- [ ] MediaMTX
- [ ] Jitsi / JVB
- [ ] FastAPI Backend
- [ ] Database
- [ ] Networking (DNS, SSL, Elastic IP, Ports)

## Deployment Environment
- Cloud Provider (e.g., AWS, Hetzner, Local):
- OS:
- Docker / Docker Compose Version:

## Steps to Reproduce
Steps to reproduce the deployment issue:
1. Command ran: `...`
2. Configuration applied (redact secrets): `...`
3. Error encountered: `...`

## Logs
Please provide relevant logs (e.g., `docker compose logs mediamtx` or `docker compose logs portal`).
**Important**: Ensure you redact any sensitive information like `SECRET_KEY` or `JWT_SECRET`.

## Additional Context
Add any other context about the problem here.
