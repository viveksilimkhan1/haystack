from abc import ABC, abstractmethod
import json
import logging
from typing import Any, Optional, Dict, Union, List


from haystack.errors import AWSConfigurationError, AmazonBedrockConfigurationError, AmazonBedrockInferenceError
from haystack.lazy_imports import LazyImport
from haystack.nodes.prompt.invocation_layer.aws_base import AWSBaseInvocationLayer
from haystack.nodes.prompt.invocation_layer.handlers import DefaultPromptHandler

logger = logging.getLogger(__name__)


with LazyImport(message="Run 'pip install farm-haystack[aws]'") as boto3_import:
    from botocore.exceptions import ClientError


class BedrockModelAdapter(ABC):
    def __init__(self, model_kwargs: Dict[str, Any], max_length: Optional[int]) -> None:
        self.model_kwargs = model_kwargs
        self.max_length = max_length

    @abstractmethod
    def prepare_body(self, prompt: str, **inference_kwargs) -> Dict[str, Any]:
        pass

    @abstractmethod
    def get_responses(self, response_body: Dict[str, Any]) -> List[str]:
        pass

    def _get_params(self, inference_kwargs: Dict[str, Any], default_params: Dict[str, Any]) -> Dict[str, Any]:
        """put the param in the params if it's in kwargs and not None (e.g. it is actually defined)
        endpoint doesn't tolerate None values, send only the params that are defined
        """
        kwargs = self.model_kwargs.copy()
        kwargs.update(inference_kwargs)
        return {
            param: kwargs.get(param, default)
            for param, default in default_params.items()
            if param in kwargs or default is not None
        }


class AnthropicModelAdapter(BedrockModelAdapter):
    def prepare_body(self, prompt: str, **inference_kwargs) -> Dict[str, Any]:
        default_params = {
            "max_tokens_to_sample": self.max_length,
            "stop_sequences": ["\n\nHuman:"],
            "temperature": None,
            "top_p": None,
            "top_k": None,
        }
        params = self._get_params(inference_kwargs, default_params)

        body = {"prompt": f"\n\nHuman: {prompt}\n\nAssistant:", **params}
        return body

    def get_responses(self, response_body: Dict[str, Any]) -> List[str]:
        responses = [response_body["completion"]]
        return responses


class CohereModelAdapter(BedrockModelAdapter):
    def prepare_body(self, prompt: str, **inference_kwargs) -> Dict[str, Any]:
        default_params = {
            "max_tokens": self.max_length,
            "stop_sequences": None,
            "temperature": None,
            "p": None,
            "k": None,
            "return_likelihoods": None,
            "stream": None,
        }
        params = self._get_params(inference_kwargs, default_params)

        body = {"prompt": prompt, **params}
        return body

    def get_responses(self, response_body: Dict[str, Any]) -> List[str]:
        responses = [generation["text"] for generation in response_body["generations"]]
        return responses


class AI21ModelAdapter(BedrockModelAdapter):
    def prepare_body(self, prompt: str, **inference_kwargs) -> Dict[str, Any]:
        default_params = {
            "maxTokens": self.max_length,
            "stopSequences": None,
            "temperature": None,
            "topP": None,
            "countPenalty": None,
            "presencePenalty": None,
            "frequencyPenalty": None,
        }
        params = self._get_params(inference_kwargs, default_params)

        body = {"prompt": prompt, **params}
        return body

    def get_responses(self, response_body: Dict[str, Any]) -> List[str]:
        responses = [completion["data"]["text"] for completion in response_body["completions"]]
        return responses


class TitanModelAdapter(BedrockModelAdapter):
    def prepare_body(self, prompt: str, **inference_kwargs) -> Dict[str, Any]:
        default_params = {"maxTokenCount": self.max_length, "stopSequences": None, "temperature": None, "topP": None}
        params = self._get_params(inference_kwargs, default_params)

        body = {"inputText": prompt, "textGenerationConfig": params}
        return body

    def get_responses(self, response_body: Dict[str, Any]) -> List[str]:
        responses = [result["outputText"] for result in response_body["results"]]
        return responses


class AmazonBedrockBaseInvocationLayer(AWSBaseInvocationLayer):
    """
    Base class for Amazon Bedrock based invocation layers.
    """

    SUPPORTED_MODELS = {
        "amazon.titan-text-express-v1": TitanModelAdapter,
        "amazon.titan-text-lite-v1": TitanModelAdapter,
        "amazon.titan-text-agile-v1": TitanModelAdapter,
        "ai21.j2-ultra-v1": AI21ModelAdapter,
        "ai21.j2-mid-v1": AI21ModelAdapter,
        "cohere.command-text-v14": CohereModelAdapter,
        "anthropic.claude-v1": AnthropicModelAdapter,
        "anthropic.claude-v2": AnthropicModelAdapter,
        "anthropic.claude-instant-v1": AnthropicModelAdapter,
    }

    def __init__(
        self,
        model_name_or_path: str,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        aws_session_token: Optional[str] = None,
        aws_region_name: Optional[str] = None,
        aws_profile_name: Optional[str] = None,
        max_length: Optional[int] = 100,
        **kwargs,
    ):
        super().__init__(model_name_or_path, **kwargs)
        self.max_length = max_length

        try:
            session = self.get_aws_session(
                aws_access_key_id=aws_access_key_id,
                aws_secret_access_key=aws_secret_access_key,
                aws_session_token=aws_session_token,
                aws_region_name=aws_region_name,
                aws_profile_name=aws_profile_name,
            )
            self.client = session.client("bedrock-runtime")
        except Exception as e:
            raise AmazonBedrockConfigurationError(
                "Could not connect to Amazon Bedrock. Make sure the AWS environment is configured correctly."
            ) from e

        model_input_kwargs = kwargs
        # We pop the model_max_length as it is not sent to the model
        # but used to truncate the prompt if needed
        model_max_length = kwargs.get("model_max_length", 4096)

        # Truncate prompt if prompt tokens > model_max_length-max_length
        # (max_length is the length of the generated text)
        # It is hard to determine which tokenizer to use for the SageMaker model
        # so we use GPT2 tokenizer which will likely provide good token count approximation
        self.prompt_handler = DefaultPromptHandler(
            model_name_or_path="gpt2", model_max_length=model_max_length, max_length=self.max_length or 100
        )

        self.model_adapter: BedrockModelAdapter = self.SUPPORTED_MODELS[self.model_name_or_path](
            model_kwargs=model_input_kwargs, max_length=self.max_length
        )

    def _ensure_token_limit(self, prompt: Union[str, List[Dict[str, str]]]) -> Union[str, List[Dict[str, str]]]:
        # the prompt for this model will be of the type str
        if isinstance(prompt, List):
            raise ValueError("SageMaker invocation layer doesn't support a dictionary as prompt, only a string.")

        resize_info = self.prompt_handler(prompt)
        if resize_info["prompt_length"] != resize_info["new_prompt_length"]:
            logger.warning(
                "The prompt has been truncated from %s tokens to %s tokens so that the prompt length and "
                "answer length (%s tokens) fit within the max token limit (%s tokens). "
                "Shorten the prompt to prevent it from being cut off.",
                resize_info["prompt_length"],
                max(0, resize_info["model_max_length"] - resize_info["max_length"]),  # type: ignore
                resize_info["max_length"],
                resize_info["model_max_length"],
            )
        return str(resize_info["resized_prompt"])

    @classmethod
    def supports(cls, model_name_or_path, **kwargs):
        supported_model_ids = cls.SUPPORTED_MODELS.keys()
        model_supported = model_name_or_path in supported_model_ids
        if not model_supported or not cls.aws_configured(**kwargs):
            return False

        try:
            session = cls.get_aws_session(**kwargs)
            bedrock = session.client("bedrock")
            foundation_models_response = bedrock.list_foundation_models(byOutputModality="TEXT")
            available_model_ids = [entry["modelId"] for entry in foundation_models_response.get("modelSummaries", [])]
        except AWSConfigurationError as e:
            raise AmazonBedrockConfigurationError(message=e.message) from e
        except Exception as e:
            raise AmazonBedrockConfigurationError(
                "Could not connect to Amazon Bedrock. Make sure the AWS environment is configured correctly."
            ) from e

        model_available = model_name_or_path in available_model_ids
        if not model_available:
            raise AmazonBedrockConfigurationError(
                f"The model {model_name_or_path} is not available in Amazon Bedrock. "
                f"Please make sure the model is available in the configured AWS region and you've been granted access."
            )

        return model_supported

    def invoke(self, *args, **kwargs):
        kwargs = kwargs.copy()
        prompt: Any = kwargs.pop("prompt", None)
        if not prompt or not isinstance(prompt, (str, list)):
            raise ValueError(
                f"No valid prompt provided. Model {self.model_name_or_path} requires a valid prompt."
                f"Make sure to provide a prompt in the format that the model expects."
            )

        body = self.model_adapter.prepare_body(prompt=prompt, **kwargs)
        try:
            response = self.client.invoke_model(
                body=json.dumps(body),
                modelId=self.model_name_or_path,
                accept="application/json",
                contentType="application/json",
            )
        except ClientError as e:
            raise AmazonBedrockInferenceError(
                f"Could not connect to Amazon Bedrock model {self.model_name_or_path}. Make sure the AWS environment is configured correctly."
            ) from e

        response_body = json.loads(response.get("body").read().decode("utf-8"))
        responses = self.model_adapter.get_responses(response_body=response_body)

        return responses