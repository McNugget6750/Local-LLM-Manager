# Implementation Plan: Telegram Bot Interface

## 1. Overview
This plan outlines the implementation of a Telegram Bot proxy that allows remote interaction with the Eli Backend (`localhost:1237`). The bot will handle user authentication via tokens stored in a local SQLite database and proxy chat requests to the backend using an asynchronous HTTP client.

## 2. Detailed Tasks

### Phase 1: Project Setup & Config
- [ ] Initialize directory structure and `.gitignore` [ref: SDD#1] [activity: setup]
- [ ] Configure virtual environment and install `aiogram`, `aiosqlite`, `httpx`, `python-dotenv` [ref: SDD#1] [activity: setup]
- [ ] Create `.env.example` and `.env` for `BOT_TOKEN` and `BACKEND_URL` [ref: PRD#3.3] [activity: setup]
- [ ] Implement basic `main.py` entry point with logging [ref: PRD#3.2] [activity: implement]

### Phase 2: Auth Layer (SQLite & Token verification)
- [ ] Define SQLite schema for `users` table [ref: SDD#3] [activity: prime]
- [ ] Implement `UserManager` for async DB operations (upsert user, get token) [ref: SDD#3] [activity: implement]
- [ ] Write unit tests for `UserManager` using an in-memory SQLite DB [ref: SDD#3] [activity: test]
- [ ] Validate DB operations with a test script [ref: SDD#3] [activity: validate]

### Phase 3: Proxy Layer (HTTPX bridge to localhost:1237)
- [ ] Implement `BackendClient` class with `httpx.AsyncClient` [ref: SDD#1, SDD#2] [activity: prime]
- [ ] Implement `send_message` method mapping to `POST /chat` [ref: SDD#4.1] [activity: implement]
- [ ] Implement `check_status` method mapping to `GET /status` [ref: SDD#4.2] [activity: implement]
- [ ] Implement error handling for 503 (Busy) and timeouts [ref: SDD#5.1, SDD#5.2] [activity: implement]
- [ ] Write integration tests for `BackendClient` using `respx` to mock backend responses [ref: SDD#4, SDD#5] [activity: test]
- [ ] Validate proxy layer against a mock server [ref: SDD#4] [activity: validate]

### Phase 4: Bot Layer (aiogram handlers & session management)
- [ ] Setup `aiogram` Dispatcher and Bot initialization [ref: SDD#1] [activity: prime]
- [ ] Implement `/start` command handler [ref: SDD#5.4] [activity: implement]
- [ ] Implement `/set_token` command handler to update `UserManager` [ref: PRD#2.1, SDD#5.4] [activity: implement]
- [ ] Implement main message handler:
    - Check token in `UserManager` [ref: SDD#5.4] [activity: implement]
    - Trigger "typing" action [ref: SDD#5.3] [activity: implement]
    - Call `BackendClient.send_message` [ref: PRD#2.2, SDD#4.1] [activity: implement]
    - Send response to user [ref: PRD#2.3] [activity: implement]
- [ ] Implement specific error handlers for "Busy" (503) and "Unauthorized" [ref: PRD#2.4, SDD#5.1, SDD#5.4] [activity: implement]
- [ ] Write bot handler tests using `aiogram` test utilities [ref: SDD#5] [activity: test]
- [ ] Validate handler flow with a test bot account [ref: SDD#5] [activity: validate]

### Phase 5: Integration & E2E Validation
- [ ] Perform E2E test: New user $\rightarrow$ `/set_token` $\rightarrow$ Send message $\rightarrow$ Receive response [ref: PRD#2] [activity: validate]
- [ ] Perform E2E test: Valid user $\rightarrow$ Backend Busy (503) $\rightarrow$ Receive busy message [ref: PRD#2.4, SDD#5.1] [activity: validate]
- [ ] Perform E2E test: Valid user $\rightarrow$ Backend Timeout $\rightarrow$ Receive timeout message [ref: SDD#5.2] [activity: validate]
- [ ] Perform E2E test: Unauthenticated user $\rightarrow$ Send message $\rightarrow$ Prompt for token [ref: SDD#5.4] [activity: validate]

## 3. Validation Checklist
- [ ] Bot starts without errors and connects to Telegram API.
- [ ] `/set_token` correctly persists token in SQLite.
- [ ] Messages are correctly proxied to `localhost:1237` with the stored token.
- [ ] LLM responses are delivered back to the Telegram user.
- [ ] 503 responses from backend result in the "Backend is busy" message.
- [ ] Request timeouts are handled gracefully with a user-facing message.
- [ ] Typing indicator is visible while waiting for the backend.
- [ ] Unauthenticated users are prompted to set a token.