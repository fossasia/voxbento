## Summary
A clear and concise description of what this pull request accomplishes.

## Changes Made
- [ ] Added feature X
- [ ] Fixed bug Y
- [ ] Updated documentation Z

## Testing Performed
Please describe the tests that you ran to verify your changes.
- [ ] Unit Tests
- [ ] Manual testing in interpreter booth
- [ ] Manual testing for WHEP/HLS listener
- [ ] E2E validation

## Screenshots (if applicable)
Add screenshots to help explain visual changes or new features.

## Deployment Impact
- [ ] This PR introduces new Environment Variables.
- [ ] This PR changes Docker Compose structure.
- [ ] This PR updates MediaMTX configuration.
- [ ] This PR introduces changes to the self-hosted Jitsi setup.
*If any of the above are checked, please describe the deployment impact.*

## Database Changes
- [ ] This PR modifies the SQLAlchemy models.
- [ ] This PR includes an Alembic migration.
*If yes, ensure `uv run alembic upgrade head` has been tested locally.*

## Security Considerations
- [ ] This PR introduces new API endpoints (Ensure proper JWT/Role auth is applied).
- [ ] This PR modifies user authentication or WebSocket authorization.
*If yes, explain how security has been validated.*

## Checklist
- [ ] I have read the `agents.md` guidelines.
- [ ] I have updated the `ARCHITECTURE.md` where relevant.
- [ ] I have verified that standard browser-facing JS was used (no React/Vue).
- [ ] Code is formatted and passes linting.
