# Telegram Bot Integration

This bot provides a Telegram interface for the Eli Backend chat endpoint, allowing users to interact with the backend directly through Telegram.

## Quick Start

1. **Configure Environment**: Create a `.env` file in the project root with your `BOT_TOKEN`.
2. **Run the Bot**:
   ```bash
   .venv\Scripts\python.exe -m telegram_bot.main
   ```
3. **Authenticate**: In Telegram, send the following command to the bot to link your backend account:
   `/set_token <your_backend_api_token>`

## Usage

Once the bot is running and your token is set, you can send messages directly to the bot to interact with the Eli Backend.

### Commands
- `/set_token <token>`: Sets the backend API token for your Telegram account.
- `/start`: Initializes the bot session.
- `/help`: Displays available commands.

## Configuration

The bot is configured via environment variables in a `.env` file.

| Variable | Description | Required | Default |
| :--- | :--- | :---: | :--- |
| `BOT_TOKEN` | API token provided by [@BotFather](https://t.me/BotFather) | Yes | N/A |
| `BACKEND_URL` | URL of the Eli Backend chat endpoint | No | `http://localhost:1237/chat` |
| `ADMIN_ID` | Telegram User ID for administrative access | No | N/A |
| `DB_PATH` | Path to the SQLite database for storing user tokens | No | `telegram_bot/bot_auth.db` |

## Setup Instructions

### Prerequisites
- Python virtual environment (`.venv`) configured.
- Access to a Telegram Bot token.

### Installation
1. Clone the repository.
2. Create a `.env` file in the root directory:
   ```env
   BOT_TOKEN=your_bot_token_here
   BACKEND_URL=http://localhost:1237/chat
   ADMIN_ID=your_telegram_id
   DB_PATH=telegram_bot/bot_auth.db
   ```
3. Launch the bot:
   ```bash
   .venv\Scripts\python.exe -m telegram_bot.main
   ```