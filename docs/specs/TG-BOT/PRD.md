# Product Requirements Document (PRD): Telegram Bot Interface

## 1. Goal
The goal of the Telegram Bot Interface is to provide a mobile-friendly proxy that allows users to interact with the Eli GUI/Backend (running on `localhost:1237`) via the Telegram messaging platform. This enables remote access to the LLM capabilities of the backend without needing a direct web connection to the local machine.

## 2. User Stories
- **Authentication via Token**: As a user, I want to provide a unique API token to the bot so that my Telegram account is securely linked to my backend session.
- **Sending Messages**: As a user, I want to send text messages to the bot, which are then forwarded to the Eli backend for processing.
- **Receiving Responses**: As a user, I want to receive the LLM's responses in the Telegram chat, maintaining a conversational flow.
- **Handling 'Busy' States**: As a user, I want to be notified if the backend is currently processing another request or is unavailable, so I know why my message isn't being answered immediately.

## 3. Constraints
- **Single-User Lock**: The backend currently supports a single-user lock. The bot must handle the scenario where the backend is "Busy" (e.g., returning a 503 status) and inform the user accordingly.
- **Asynchronous Requirements**: The bot must be built using asynchronous frameworks to ensure it remains responsive to multiple users even while waiting for long-running LLM responses.
- **Localhost Dependency**: The bot assumes the backend is reachable at `localhost:1237` (or a configured environment variable).

## 4. Future Scope
- **Multimodal Support**: Ability to send and receive images, documents, and voice notes.
- **Multi-User Queuing**: Implementation of a request queue on the bot side to manage multiple users waiting for the single-user backend.
- **Session Management**: Ability to clear chat history or switch profiles via Telegram commands.