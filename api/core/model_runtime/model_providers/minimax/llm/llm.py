from typing import Generator, List, Optional, Union
from core.model_runtime.model_providers.minimax.llm.errors import BadRequestError, InvalidAPIKeyError, \
    InternalServerError, RateLimitReachedError, InvalidAuthenticationError, InsufficientAccountBalanceError
from core.model_runtime.model_providers.minimax.llm.types import MinimaxMessage
from core.model_runtime.model_providers.minimax.llm.chat_completion import MinimaxChatCompletion
from core.model_runtime.model_providers.minimax.llm.chat_completion_pro import MinimaxChatCompletionPro

from core.model_runtime.entities.llm_entities import LLMResult, LLMUsage, LLMResultChunk, LLMResultChunkDelta, LLMMode
from core.model_runtime.entities.message_entities import PromptMessage, PromptMessageTool, AssistantPromptMessage, UserPromptMessage, SystemPromptMessage
from core.model_runtime.entities.model_entities import AIModelEntity, ParameterRule, ParameterType, FetchFrom, ModelType
from core.model_runtime.model_providers.__base.large_language_model import LargeLanguageModel
from core.model_runtime.errors.invoke import InvokeConnectionError, InvokeServerUnavailableError, InvokeRateLimitError, \
    InvokeAuthorizationError, InvokeBadRequestError, InvokeError
from core.model_runtime.errors.validate import CredentialsValidateFailedError

class MinimaxLargeLanguageModel(LargeLanguageModel):
    model_apis = {
        'abab5.5-chat': MinimaxChatCompletionPro,
        'abab5-chat': MinimaxChatCompletion
    }

    def _invoke(self, model: str, credentials: dict, prompt_messages: list[PromptMessage], 
                model_parameters: dict, tools: list[PromptMessageTool] | None = None, 
                stop: List[str] | None = None, stream: bool = True, user: str | None = None) \
        -> LLMResult | Generator:
        return self._generate(model, credentials, prompt_messages, model_parameters, tools, stop, stream, user)

    def validate_credentials(self, model: str, credentials: dict) -> None:
        """
        Validate credentials for Baichuan model
        """
        if model not in self.model_apis:
            raise CredentialsValidateFailedError(f'Invalid model: {model}')

        if not credentials.get('minimax_api_key'):
            raise CredentialsValidateFailedError('Invalid API key')

        if not credentials.get('minimax_group_id'):
            raise CredentialsValidateFailedError('Invalid group ID')
        
        # ping
        instance = MinimaxChatCompletionPro()
        try:
            instance.generate(
                model=model, api_key=credentials['minimax_api_key'], group_id=credentials['minimax_group_id'],
                prompt_messages=[
                    MinimaxMessage(content='ping', role='USER')
                ],
                model_parameters={},
                tools=[], stop=[],
                stream=False,
                user=''
            )
        except InvalidAuthenticationError as e:
            raise CredentialsValidateFailedError(f"Invalid API key: {e}")

    def get_num_tokens(self, model: str, credentials: dict, prompt_messages: list[PromptMessage],
                       tools: list[PromptMessageTool] | None = None) -> int:
        return self._num_tokens_from_messages(prompt_messages, tools)

    def _num_tokens_from_messages(self, messages: List[PromptMessage], tools: list[PromptMessageTool]) -> int:
        """
            Calculate num tokens for minimax model

            not like ChatGLM, Minimax has a special prompt structure, we could not find a proper way
            to caculate the num tokens, so we use str() to convert the prompt to string

            Minimax does not provide their own tokenizer of adab5.5 and abab5 model
            therefore, we use gpt2 tokenizer instead
        """
        messages_dict = [self._convert_prompt_message_to_minimax_message(m).to_dict() for m in messages]
        return self._get_num_tokens_by_gpt2(str(messages_dict))

    def _generate(self, model: str, credentials: dict, prompt_messages: list[PromptMessage], 
                model_parameters: dict, tools: list[PromptMessageTool] | None = None, 
                stop: List[str] | None = None, stream: bool = True, user: str | None = None) \
        -> LLMResult | Generator:
        """
            use MinimaxChatCompletionPro as the type of client, anyway,  MinimaxChatCompletion has the same interface
        """
        client: MinimaxChatCompletionPro = self.model_apis[model]()

        response = client.generate(
            model=model,
            api_key=credentials['minimax_api_key'],
            group_id=credentials['minimax_group_id'],
            prompt_messages=[self._convert_prompt_message_to_minimax_message(message) for message in prompt_messages],
            model_parameters=model_parameters,
            tools=tools,
            stop=stop,
            stream=stream,
            user=user
        )

        if stream:
            return self._handle_chat_generate_stream_response(model=model, prompt_messages=prompt_messages, credentials=credentials, response=response)
        return self._handle_chat_generate_response(model=model, prompt_messages=prompt_messages, credentials=credentials, response=response)

    def _convert_prompt_message_to_minimax_message(self, prompt_message: PromptMessage) -> MinimaxMessage:
        """
            convert PromptMessage to MinimaxMessage so that we can use MinimaxChatCompletionPro interface
        """
        if isinstance(prompt_message, SystemPromptMessage):
            return MinimaxMessage(role=MinimaxMessage.Role.SYSTEM.value, content=prompt_message.content)
        elif isinstance(prompt_message, UserPromptMessage):
            return MinimaxMessage(role=MinimaxMessage.Role.USER.value, content=prompt_message.content)
        elif isinstance(prompt_message, AssistantPromptMessage):
            return MinimaxMessage(role=MinimaxMessage.Role.ASSISTANT.value, content=prompt_message.content)
        else:
            raise NotImplementedError(f'Prompt message type {type(prompt_message)} is not supported')

    def _handle_chat_generate_response(self, model: str, prompt_messages: list[PromptMessage], credentials: dict, response: MinimaxMessage) -> LLMResult:
        usage = self._calc_response_usage(model=model, credentials=credentials, 
                                          prompt_tokens=response.usage['prompt_tokens'], 
                                          completion_tokens=response.usage['completion_tokens']
                                        )
        return LLMResult(
            model=model,
            prompt_messages=prompt_messages,
            message=AssistantPromptMessage(
                content=response.content,
                tool_calls=[],
            ),
            usage=usage,
        )

    def _handle_chat_generate_stream_response(self, model: str, prompt_messages: list[PromptMessage], 
                                              credentials: dict, response: Generator[MinimaxMessage, None, None]) \
        -> Generator[LLMResultChunk, None, None]:
        for message in response:
            if message.usage:
                usage = self._calc_response_usage(
                    model=model, credentials=credentials, 
                    prompt_tokens=message.usage['prompt_tokens'], 
                    completion_tokens=message.usage['completion_tokens']
                )
                yield LLMResultChunk(
                    model=model,
                    prompt_messages=prompt_messages,
                    delta=LLMResultChunkDelta(
                        index=0,
                        message=AssistantPromptMessage(
                            content=message.content,
                            tool_calls=[]
                        ),
                        usage=usage,
                        finish_reason=message.stop_reason if message.stop_reason else None,
                    ),
                )
            else:
                yield LLMResultChunk(
                    model=model,
                    prompt_messages=prompt_messages,
                    delta=LLMResultChunkDelta(
                        index=0,
                        message=AssistantPromptMessage(
                            content=message.content,
                            tool_calls=[]
                        ),
                        finish_reason=message.stop_reason if message.stop_reason else None,
                    ),
                )

    @property
    def _invoke_error_mapping(self) -> dict[type[InvokeError], list[type[Exception]]]:
        """
        Map model invoke error to unified error
        The key is the error type thrown to the caller
        The value is the error type thrown by the model,
        which needs to be converted into a unified error type for the caller.

        :return: Invoke error mapping
        """
        return {
            InvokeConnectionError: [
            ],
            InvokeServerUnavailableError: [
                InternalServerError
            ],
            InvokeRateLimitError: [
                RateLimitReachedError
            ],
            InvokeAuthorizationError: [
                InvalidAuthenticationError,
                InsufficientAccountBalanceError,
                InvalidAPIKeyError,
            ],
            InvokeBadRequestError: [
                BadRequestError,
                KeyError
            ]
        }

