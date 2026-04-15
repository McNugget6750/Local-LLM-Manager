import httpx
import logging
import json
from typing import Dict, Any
from telegram_bot.config import settings

logger = logging.getLogger(__name__)

class BackendError(Exception):
    """Base exception for backend communication errors."""
    pass

class BackendBusyError(BackendError):
    """Raised when the backend returns a 503 Service Unavailable."""
    pass

class BackendTimeoutError(BackendError):
    """Raised when the backend request times out."""
    pass

class BackendClient:
    """
    Proxy client to communicate with the Eli RemoteChatServer.
    """
    def __init__(self, base_url: str = str(settings.BACKEND_URL)):
        # The config.py has BACKEND_URL as "http://localhost:1237/chat"
        # We need the base URL for the client, so we strip the /chat part if present
        self.base_url = base_url.rsplit('/chat', 1)[0]
        if not self.base_url.endswith('/'):
            self.base_url += '/'
        
        self.timeout = httpx.Timeout(900.0, connect=5.0)
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)

    async def send_message(self, token: str, message: str) -> str:
        """
        Sends a message to the backend and returns the response.
        
        Args:
            token: User's API token for authentication.
            message: The message content to send.
            
        Returns:
            The response text from the backend.
            
        Raises:
            BackendBusyError: If backend returns 503.
            BackendTimeoutError: If the request times out.
            BackendError: For other HTTP errors.
        """
        payload = {
            "token": token,
            "message": message
        }
        try:
            response = await self._client.post("/chat", json=payload)
            
            if response.status_code == 503:
                raise BackendBusyError("Backend is currently busy")
                
            response.raise_for_status()
            
            try:
                data = response.json()
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse backend JSON response: {e}")
                raise BackendError("Backend returned an invalid JSON response") from e
            
            if not isinstance(data, dict):
                logger.error(f"Backend response is not a JSON object: {type(data)}")
                raise BackendError("Backend returned an unexpected response format")
            
            if "response" in data:
                return str(data["response"])
            if "text" in data:
                return str(data["text"])

            logger.error(f"Backend response missing both 'response' and 'text' keys: {data}")
            raise BackendError("Backend response missing required 'response' or 'text' field")
            
        except (BackendBusyError, BackendTimeoutError):
            raise

        except httpx.TimeoutException as e:
            logger.error(f"Backend timeout: {e}")
            raise BackendTimeoutError("Request to backend timed out") from e
        except httpx.HTTPStatusError as e:
            logger.error(f"Backend HTTP error: {e}")
            raise BackendError(f"Backend returned error: {e.response.status_code}") from e
        except Exception as e:
            logger.exception(f"Unexpected error during backend communication: {e}")
            raise BackendError(f"An unexpected error occurred: {e}") from e

    async def validate_token(self, token: str) -> bool:
        """
        Validates the provided API token by sending a minimal request to the backend.
        
        Args:
            token: The API token to validate.
            
        Returns:
            True if the token is valid, False otherwise.
        """
        try:
            payload = {"token": token, "message": "ping"}
            response = await self._client.post("/chat", json=payload)
            return response.status_code == 200
        except Exception as e:
            logger.debug(f"Token validation failed: {e}")
            return False

    async def post_approve(self, response: str) -> dict:
        """
        Send an approval response to the bridge's /approve endpoint.

        Args:
            response: "1" (allow once), "2" (allow session), or "3" (deny).

        Returns:
            Parsed JSON dict from bridge, e.g. {"ok": true} or {"ok": false, "reason": "..."}.
        """
        try:
            resp = await self._client.post("/approve", json={"response": response})
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"post_approve failed: {e}")
            return {"ok": False, "reason": str(e)}

    async def check_status(self) -> bool:
        """
        Checks if the backend is available.
        
        Returns:
            True if available (200 OK), False if busy (503) or unavailable.
        """
        try:
            response = await self._client.get("/status")
            return response.status_code == 200
        except Exception as e:
            logger.warning(f"Backend status check failed: {e}")
            return False

    async def close(self):
        """Close the underlying HTTP client."""
        await self._client.aclose()
