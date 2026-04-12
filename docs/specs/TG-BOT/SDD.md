# Solution Design Document (SDD): Telegram Bot Interface

## 1. Technical Stack
- **Framework**: `aiogram 3.x` (Asynchronous Telegram Bot API wrapper)
- **Database**: `aiosqlite` (Asynchronous SQLite wrapper for local user/token storage)
- **HTTP Client**: `httpx` (Asynchronous HTTP client for proxying requests to the backend)
- **Environment**: Python 3.10+

## 2. Architecture
The bot acts as a thin proxy layer between the Telegram API and the Eli RemoteChatServer.

**Request Flow:**
`Telegram User` $\rightarrow$ `Telegram API` $\rightarrow$ `Bot Layer (aiogram)` $\rightarrow$ `Auth Layer (aiosqlite)` $\rightarrow$ `Proxy Layer (httpx)` $\rightarrow$ `RemoteChatServer (localhost:1237)`

## 3. Data Model
A local SQLite database will be used to map Telegram `user_id` to the required `api_token` for backend authentication.

### Table: `users`
| Column | Type | Description |
| :--- | :--- | :--- |
| `user_id` | INTEGER (PK) | Telegram unique user identifier |
| `api_token` | TEXT | Token used to authenticate with the Eli backend |
| `status` | TEXT | Current user state (e.g., 'authenticated', 'pending') |
| `created_at` | DATETIME | Timestamp of first interaction |
| `last_used` | DATETIME | Timestamp of last successful request |

## 4. API Interaction
The bot will communicate with the backend via a REST API.

### `POST /chat`
- **Purpose**: Send a user message and receive a response.
- **Payload**: `{ "token": "...", "message": "..." }`
- **Expected Response**: `{ "response": "..." }`

### `GET /status`
- **Purpose**: Check if the backend is available or busy.
- **Expected Response**: `200 OK` (Available) or `503 Service Unavailable` (Busy).

## 5. Error Handling & UX
- **503 Busy Handling**: If the backend returns a 503, the bot will reply: *"The backend is currently busy processing another request. Please try again in a moment."*
- **Timeouts**: `httpx` will be configured with a generous timeout (e.g., 60-120s) to accommodate long LLM generation times. If a timeout occurs, a "Request timed out" message is sent.
- **Typing Indicator**: The bot will trigger the `sendChatAction(action="typing")` method immediately after forwarding the message to the backend and will maintain it until the response is received or an error occurs.
- **Auth Flow**: If a user sends a message without a registered token, the bot will prompt them to set their token using a command like `/set_token <token>`.