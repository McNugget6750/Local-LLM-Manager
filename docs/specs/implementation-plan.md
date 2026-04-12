# Implementation Plan: Token Validation for Telegram Bot

## Validation Checklist
- [x] Every PRD requirement mapped to at least one task.
- [x] Every SDD component covered by phases.
- [x] Each phase follows the Prime $\rightarrow$ Test $\rightarrow$ Implement $\rightarrow$ Validate flow.
- [x] All specification file paths are correct and exist.

## Specification Compliance Guidelines
- **Deviation Protocol**: Any changes to the `BackendClient` API must be documented. If the backend does not support a dedicated `/validate` endpoint, a "ping" message will be used.

## Metadata Reference
- `[parallel: true]`: Tasks that can run concurrently.
- `[component: name]`: Component being modified.
- `[ref: file; lines: X-Y]`: Link to existing code.
- `[activity: type]`: Specialist agent hint.

## Context Priming
- **Spec Paths**: `telegram_bot/backend_client.py`, `telegram_bot/handlers/user.py`, `telegram_bot/user_manager.py`.
- **Key Interface**: `BackendClient.send_message` currently handles authentication via the backend response.
- **Goal**: Prevent the storage of invalid tokens in `bot_auth.db` by verifying them with the backend during the `/set_token` command.

---

## Implementation Phases

### Phase 1: Discovery & Test Definition
1. **Prime Context**: Analyze `BackendClient` and `handlers/user.py` to determine the most efficient way to validate a token without side effects. `[ref: telegram_bot/backend_client.py; lines: 35-91]` `[activity: code-researcher]`
2. **Define Failure Tests**: Add test cases to `tests/test_e2e.py` that attempt to use `/set_token` with a known invalid token and assert that the bot returns an error message instead of success. `[activity: test-writer]`
3. **Define Success Tests**: Add test cases to `tests/test_e2e.py` that use a valid token and assert success. `[activity: test-writer]`

### Phase 2: Backend Client Enhancement
1. **Implement `validate_token`**: Add a method to `BackendClient` to verify token validity. If a dedicated endpoint doesn't exist, implement a minimal "ping" request. `[component: BackendClient]` `[ref: telegram_bot/backend_client.py]` `[activity: expert_coder]`
2. **Validate Client Method**: Create a standalone script to verify `validate_token` returns `True` for valid tokens and `False` for invalid ones. `[activity: run-tests]`

### Phase 3: Handler Integration
1. **Update `/set_token` Logic**: Modify the handler to call `backend_client.validate_token` before calling `user_manager.upsert_user`. `[component: handlers]` `[ref: telegram_bot/handlers/user.py; lines: 34-58]` `[activity: expert_coder]`
2. **Implement Error Messaging**: Ensure the user receives a clear "Invalid Token" message when validation fails. `[activity: expert_coder]`

### Phase 4: Final Validation
1. **Run E2E Suite**: Execute all tests in `tests/test_e2e.py` to ensure no regressions and that new validation logic works. `[activity: run-tests]`
2. **Manual Verification**: Verify the flow in the Telegram GUI:
    - Valid token $\rightarrow$ Success message $\rightarrow$ Message proxy works.
    - Invalid token $\rightarrow$ Error message $\rightarrow$ Database remains unchanged.
3. **Compliance Gate**: Verify that the implementation matches the goal of preventing invalid token storage. `[activity: review-code]`
