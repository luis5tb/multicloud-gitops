"""Unit tests for ChatVLLMReasoning provider."""

from unittest.mock import Mock, patch

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from ols.src.llms.providers.chat_vlllm_reasoning import ChatVLLMReasoning


@pytest.fixture
def chat_vllm_reasoning():
    """Create a ChatVLLMReasoning instance."""
    with patch(
        "ols.src.llms.providers.chat_vlllm_reasoning.ChatOpenAI.__init__",
        return_value=None,
    ):
        instance = ChatVLLMReasoning()
        return instance


class TestExtractReasoning:
    """Tests for _extract_reasoning static method."""

    def test_extract_reasoning_content_field(self):
        """Test extraction from reasoning_content field."""
        message_dict = {"reasoning_content": "some reasoning"}
        result = ChatVLLMReasoning._extract_reasoning(message_dict)
        assert result == "some reasoning"

    def test_extract_reasoning_field(self):
        """Test extraction from reasoning field."""
        message_dict = {"reasoning": "some reasoning"}
        result = ChatVLLMReasoning._extract_reasoning(message_dict)
        assert result == "some reasoning"

    def test_extract_reasoning_content_takes_precedence(self):
        """Test that reasoning_content takes precedence over reasoning."""
        message_dict = {
            "reasoning_content": "from reasoning_content",
            "reasoning": "from reasoning",
        }
        result = ChatVLLMReasoning._extract_reasoning(message_dict)
        assert result == "from reasoning_content"

    def test_extract_reasoning_missing(self):
        """Test extraction when no reasoning field present."""
        message_dict = {"content": "some content"}
        result = ChatVLLMReasoning._extract_reasoning(message_dict)
        assert result is None

    def test_extract_reasoning_empty_dict(self):
        """Test extraction from empty dict."""
        result = ChatVLLMReasoning._extract_reasoning({})
        assert result is None

    def test_extract_reasoning_empty_string(self):
        """Test extraction of empty string."""
        message_dict = {"reasoning_content": ""}
        result = ChatVLLMReasoning._extract_reasoning(message_dict)
        assert result == ""


class TestGetRawMessage:
    """Tests for _get_raw_message method."""

    def test_get_raw_message_from_dict_response(self, chat_vllm_reasoning):
        """Test extracting raw message from dict response."""
        response = {
            "choices": [
                {"message": {"content": "test", "reasoning_content": "reasoning"}}
            ]
        }
        raw_choices = response["choices"]
        result = chat_vllm_reasoning._get_raw_message(response, 0, raw_choices)
        assert result == {"content": "test", "reasoning_content": "reasoning"}

    def test_get_raw_message_out_of_bounds(self, chat_vllm_reasoning):
        """Test extracting when index is out of bounds."""
        response = {"choices": [{"message": {"content": "test"}}]}
        raw_choices = response["choices"]
        result = chat_vllm_reasoning._get_raw_message(response, 5, raw_choices)
        assert result == {}

    def test_get_raw_message_from_object_with_model_dump(self, chat_vllm_reasoning):
        """Test extracting from object response with model_dump method."""
        mock_message = Mock()
        mock_message.model_dump.return_value = {
            "content": "test",
            "reasoning": "reasoning",
        }

        mock_choice = Mock()
        mock_choice.message = mock_message

        response = Mock()
        raw_choices = [mock_choice]

        result = chat_vllm_reasoning._get_raw_message(response, 0, raw_choices)
        assert result == {"content": "test", "reasoning": "reasoning"}
        mock_message.model_dump.assert_called_once()

    def test_get_raw_message_from_object_dict_message(self, chat_vllm_reasoning):
        """Test extracting from object response with dict message."""
        mock_choice = Mock()
        mock_choice.message = {"content": "test", "reasoning": "reasoning"}

        response = Mock()
        raw_choices = [mock_choice]

        result = chat_vllm_reasoning._get_raw_message(response, 0, raw_choices)
        assert result == {"content": "test", "reasoning": "reasoning"}

    def test_get_raw_message_none_message(self, chat_vllm_reasoning):
        """Test extracting when message is None."""
        mock_choice = Mock()
        mock_choice.message = None

        response = Mock()
        raw_choices = [mock_choice]

        result = chat_vllm_reasoning._get_raw_message(response, 0, raw_choices)
        assert result == {}

    def test_get_raw_message_object_out_of_bounds(self, chat_vllm_reasoning):
        """Test extracting from object response when index out of bounds."""
        response = Mock()
        raw_choices = []

        result = chat_vllm_reasoning._get_raw_message(response, 0, raw_choices)
        assert result == {}


class TestCreateChatResult:
    """Tests for _create_chat_result method."""

    def test_create_chat_result_preserves_reasoning_content(self, chat_vllm_reasoning):
        """Test that reasoning_content is preserved in additional_kwargs."""
        # Mock parent's _create_chat_result
        ai_message = AIMessage(content="response")
        chat_generation = ChatGeneration(message=ai_message)
        mock_result = ChatResult(generations=[chat_generation])

        with patch.object(
            ChatVLLMReasoning.__bases__[0],
            "_create_chat_result",
            return_value=mock_result,
        ):
            response = {
                "choices": [
                    {
                        "message": {
                            "content": "response",
                            "reasoning_content": "test reasoning",
                        }
                    }
                ]
            }

            result = chat_vllm_reasoning._create_chat_result(response)

            assert len(result.generations) == 1
            assert (
                result.generations[0].message.additional_kwargs["reasoning_content"]
                == "test reasoning"
            )

    def test_create_chat_result_preserves_reasoning_field(self, chat_vllm_reasoning):
        """Test that reasoning field is preserved."""
        ai_message = AIMessage(content="response")
        chat_generation = ChatGeneration(message=ai_message)
        mock_result = ChatResult(generations=[chat_generation])

        with patch.object(
            ChatVLLMReasoning.__bases__[0],
            "_create_chat_result",
            return_value=mock_result,
        ):
            response = {
                "choices": [
                    {
                        "message": {
                            "content": "response",
                            "reasoning": "test reasoning",
                        }
                    }
                ]
            }

            result = chat_vllm_reasoning._create_chat_result(response)

            assert (
                result.generations[0].message.additional_kwargs["reasoning_content"]
                == "test reasoning"
            )

    def test_create_chat_result_multiple_generations(self, chat_vllm_reasoning):
        """Test handling multiple generations."""
        ai_message1 = AIMessage(content="response1")
        chat_generation1 = ChatGeneration(message=ai_message1)
        ai_message2 = AIMessage(content="response2")
        chat_generation2 = ChatGeneration(message=ai_message2)
        mock_result = ChatResult(generations=[chat_generation1, chat_generation2])

        with patch.object(
            ChatVLLMReasoning.__bases__[0],
            "_create_chat_result",
            return_value=mock_result,
        ):
            response = {
                "choices": [
                    {
                        "message": {
                            "content": "response1",
                            "reasoning_content": "reasoning1",
                        }
                    },
                    {
                        "message": {
                            "content": "response2",
                            "reasoning_content": "reasoning2",
                        }
                    },
                ]
            }

            result = chat_vllm_reasoning._create_chat_result(response)

            # Both generations should have reasoning preserved
            assert (
                result.generations[0].message.additional_kwargs["reasoning_content"]
                == "reasoning1"
            )
            assert (
                result.generations[1].message.additional_kwargs["reasoning_content"]
                == "reasoning2"
            )

    def test_create_chat_result_no_reasoning(self, chat_vllm_reasoning):
        """Test handling when no reasoning present."""
        ai_message = AIMessage(content="response")
        chat_generation = ChatGeneration(message=ai_message)
        mock_result = ChatResult(generations=[chat_generation])

        with patch.object(
            ChatVLLMReasoning.__bases__[0],
            "_create_chat_result",
            return_value=mock_result,
        ):
            response = {"choices": [{"message": {"content": "response"}}]}

            result = chat_vllm_reasoning._create_chat_result(response)

            # Should not add reasoning_content if no reasoning present
            assert (
                "reasoning_content"
                not in result.generations[0].message.additional_kwargs
            )


class TestConvertChunkToGenerationChunk:
    """Tests for _convert_chunk_to_generation_chunk method."""

    def test_convert_chunk_with_reasoning_content(self, chat_vllm_reasoning):
        """Test converting chunk with reasoning_content field."""
        ai_chunk = AIMessageChunk(content="response")
        mock_chunk_result = ChatGenerationChunk(message=ai_chunk)

        with patch.object(
            ChatVLLMReasoning.__bases__[0],
            "_convert_chunk_to_generation_chunk",
            return_value=mock_chunk_result,
        ):
            # Mock delta with reasoning_content
            mock_delta = Mock()
            mock_delta.reasoning_content = "test reasoning"

            mock_choice = Mock()
            mock_choice.delta = mock_delta

            mock_chunk = Mock()
            mock_chunk.choices = [mock_choice]

            result = chat_vllm_reasoning._convert_chunk_to_generation_chunk(
                mock_chunk, ChatGenerationChunk
            )

            assert (
                result.message.additional_kwargs["reasoning_content"]
                == "test reasoning"
            )

    def test_convert_chunk_with_reasoning_field(self, chat_vllm_reasoning):
        """Test converting chunk with reasoning field."""
        ai_chunk = AIMessageChunk(content="response")
        mock_chunk_result = ChatGenerationChunk(message=ai_chunk)

        with patch.object(
            ChatVLLMReasoning.__bases__[0],
            "_convert_chunk_to_generation_chunk",
            return_value=mock_chunk_result,
        ):
            mock_delta = Mock()
            mock_delta.reasoning_content = None
            mock_delta.reasoning = "test reasoning"

            mock_choice = Mock()
            mock_choice.delta = mock_delta

            mock_chunk = Mock()
            mock_chunk.choices = [mock_choice]

            result = chat_vllm_reasoning._convert_chunk_to_generation_chunk(
                mock_chunk, ChatGenerationChunk
            )

            assert (
                result.message.additional_kwargs["reasoning_content"]
                == "test reasoning"
            )

    def test_convert_chunk_with_model_extra(self, chat_vllm_reasoning):
        """Test converting chunk with reasoning in model_extra."""
        ai_chunk = AIMessageChunk(content="response")
        mock_chunk_result = ChatGenerationChunk(message=ai_chunk)

        with patch.object(
            ChatVLLMReasoning.__bases__[0],
            "_convert_chunk_to_generation_chunk",
            return_value=mock_chunk_result,
        ):
            mock_delta = Mock()
            mock_delta.reasoning_content = None
            mock_delta.reasoning = None
            mock_delta.model_extra = {"reasoning_content": "test reasoning"}

            mock_choice = Mock()
            mock_choice.delta = mock_delta

            mock_chunk = Mock()
            mock_chunk.choices = [mock_choice]

            result = chat_vllm_reasoning._convert_chunk_to_generation_chunk(
                mock_chunk, ChatGenerationChunk
            )

            assert (
                result.message.additional_kwargs["reasoning_content"]
                == "test reasoning"
            )

    def test_convert_chunk_no_choices(self, chat_vllm_reasoning):
        """Test converting chunk with no choices."""
        ai_chunk = AIMessageChunk(content="response")
        mock_chunk_result = ChatGenerationChunk(message=ai_chunk)

        with patch.object(
            ChatVLLMReasoning.__bases__[0],
            "_convert_chunk_to_generation_chunk",
            return_value=mock_chunk_result,
        ):
            mock_chunk = Mock()
            mock_chunk.choices = []

            result = chat_vllm_reasoning._convert_chunk_to_generation_chunk(
                mock_chunk, ChatGenerationChunk
            )

            # Should return unchanged since delta is None
            assert result == mock_chunk_result

    def test_convert_chunk_no_reasoning(self, chat_vllm_reasoning):
        """Test converting chunk with no reasoning."""
        ai_chunk = AIMessageChunk(content="response")
        mock_chunk_result = ChatGenerationChunk(message=ai_chunk)

        with patch.object(
            ChatVLLMReasoning.__bases__[0],
            "_convert_chunk_to_generation_chunk",
            return_value=mock_chunk_result,
        ):
            mock_delta = Mock()
            mock_delta.reasoning_content = None
            mock_delta.reasoning = None
            mock_delta.model_extra = None

            mock_choice = Mock()
            mock_choice.delta = mock_delta

            mock_chunk = Mock()
            mock_chunk.choices = [mock_choice]

            result = chat_vllm_reasoning._convert_chunk_to_generation_chunk(
                mock_chunk, ChatGenerationChunk
            )

            # Should not add reasoning_content if no reasoning
            assert "reasoning_content" not in result.message.additional_kwargs
