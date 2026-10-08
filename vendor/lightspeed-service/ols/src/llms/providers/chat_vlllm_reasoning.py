"""ChatVLLMReasoning for preserving reasoning fields in vLLM responses."""

from typing import Any, Dict, Optional, Union

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI


class ChatVLLMReasoning(ChatOpenAI):
    """ChatOpenAI subclass that preserves vLLM/DeepSeek reasoning fields.

    Preserves vLLM/DeepSeek reasoning fields (``reasoning_content`` or
    ``reasoning``) in ``message.additional_kwargs["reasoning_content"]``.
    """

    @staticmethod
    def _extract_reasoning(message_dict: Dict[str, Any]) -> Optional[str]:
        """Extract reasoning text from vLLM-style message dicts."""
        if "reasoning_content" in message_dict:
            return message_dict["reasoning_content"]
        if "reasoning" in message_dict:
            return message_dict["reasoning"]
        return None

    def _get_raw_message(
        self, response: Union[dict, Any], idx: int, raw_choices: list
    ) -> Dict[str, Any]:
        """Extract raw message from response choices."""
        raw_message: Dict[str, Any] = {}
        if isinstance(response, dict):
            raw_message = (
                raw_choices[idx].get("message", {}) if idx < len(raw_choices) else {}
            )
        else:
            choice = raw_choices[idx] if idx < len(raw_choices) else None
            if choice is not None:
                msg = getattr(choice, "message", None)
                if msg is not None:
                    if hasattr(msg, "model_dump"):
                        raw_message = msg.model_dump()
                    elif isinstance(msg, dict):
                        raw_message = msg
        return raw_message

    def _create_chat_result(  # pylint: disable=arguments-differ,signature-differs
        self,
        response: Any,
        generation_info: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Create chat result, preserving reasoning fields."""
        result = super()._create_chat_result(
            response, generation_info=generation_info, **kwargs
        )

        raw_choices = (
            response.get("choices", [])
            if isinstance(response, dict)
            else getattr(response, "choices", [])
        )

        for idx, generation in enumerate(result.generations):
            if not isinstance(generation, ChatGeneration):
                continue

            message = generation.message
            if not isinstance(message, AIMessage):
                continue

            raw_message = self._get_raw_message(response, idx, raw_choices)
            reasoning = self._extract_reasoning(raw_message)
            if reasoning:
                if message.additional_kwargs is None:
                    message.additional_kwargs = {}
                message.additional_kwargs["reasoning_content"] = reasoning

        return result

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: Any,
        default_chunk_class: type,
        base_generation_info: Optional[Dict[str, Any]] = None,
    ) -> ChatGenerationChunk:
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )

        delta = chunk.choices[0].delta if chunk.choices else None
        if delta is None:
            return generation_chunk

        reasoning: Optional[str] = None

        if hasattr(delta, "reasoning_content") and delta.reasoning_content is not None:
            reasoning = delta.reasoning_content
        elif hasattr(delta, "reasoning") and delta.reasoning is not None:
            reasoning = delta.reasoning
        else:
            model_extra = getattr(delta, "model_extra", None) or {}
            reasoning = model_extra.get("reasoning_content") or model_extra.get(
                "reasoning"
            )

        if reasoning:
            msg = generation_chunk.message
            if isinstance(msg, AIMessageChunk):
                if msg.additional_kwargs is None:
                    msg.additional_kwargs = {}
                msg.additional_kwargs["reasoning_content"] = reasoning

        return generation_chunk
