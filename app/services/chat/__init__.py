"""Chat service package — V2 Semantic Engine pipeline.

Public API:
    from app.services.chat import chat, execute_with_params, run_via_rest, run_streaming
"""
from app.services.chat.chat_service import chat
from app.services.chat.execute_service import execute_with_params
from app.services.chat.streaming_service import run_streaming, run_via_rest

__all__ = ["chat", "execute_with_params", "run_via_rest", "run_streaming"]
