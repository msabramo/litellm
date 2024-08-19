# +-------------------------------------------------------------+
#
#           Use AporioAI for your LLM calls
#
# +-------------------------------------------------------------+
#  Thank you users! We ❤️ you! - Krrish & Ishaan

import sys
import os

sys.path.insert(
    0, os.path.abspath("../..")
)  # Adds the parent directory to the system path
from typing import Optional, Literal, Union, Any
import litellm, traceback, sys, uuid
from litellm.caching import DualCache
from litellm.proxy._types import UserAPIKeyAuth
from litellm.integrations.custom_logger import CustomLogger
from fastapi import HTTPException
from litellm._logging import verbose_proxy_logger
from litellm.proxy.guardrails.guardrail_helpers import should_proceed_based_on_metadata
from litellm.litellm_core_utils.logging_utils import (
    convert_litellm_response_object_to_str,
)
from typing import List
from datetime import datetime
import aiohttp, asyncio
from litellm._logging import verbose_proxy_logger
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
import httpx
import json

litellm.set_verbose = True

GUARDRAIL_NAME = "aporio"


class _ENTERPRISE_Aporio(CustomLogger):
    def __init__(self, api_key: Optional[str] = None, api_base: Optional[str] = None):
        self.async_handler = AsyncHTTPHandler(
            timeout=httpx.Timeout(timeout=600.0, connect=5.0)
        )
        self.aporio_api_key = api_key or os.environ["APORIO_API_KEY"]
        self.aporio_api_base = api_base or os.environ["APORIO_API_BASE"]

    #### CALL HOOKS - proxy only ####
    def transform_messages(self, messages: List[dict]) -> List[dict]:
        supported_openai_roles = ["system", "user", "assistant"]
        default_role = "other"  # for unsupported roles - e.g. tool
        new_messages = []
        for m in messages:
            if m.get("role", "") in supported_openai_roles:
                new_messages.append(m)
            else:
                new_messages.append(
                    {
                        "role": default_role,
                        **{key: value for key, value in m.items() if key != "role"},
                    }
                )

        return new_messages

    async def prepare_aporia_request(
        self, new_messages: List[dict], response_string: Optional[str] = None
    ) -> dict:
        data: dict[str, Any] = {}
        if new_messages is not None:
            data["messages"] = new_messages
        if response_string is not None:
            data["response"] = response_string

        # Set validation target
        if new_messages and response_string:
            data["validation_target"] = "both"
        elif new_messages:
            data["validation_target"] = "prompt"
        elif response_string:
            data["validation_target"] = "response"

        verbose_proxy_logger.debug("Aporia AI request: %s", data)
        return data

    async def make_aporia_api_request(
        self, new_messages: List[dict], response_string: Optional[str] = None
    ):
        data = await self.prepare_aporia_request(
            new_messages=new_messages, response_string=response_string
        )

        _json_data = json.dumps(data)

        """
        export APORIO_API_KEY=<your key>
        curl https://gr-prd-trial.aporia.com/some-id \
            -X POST \
            -H "X-APORIA-API-KEY: $APORIO_API_KEY" \
            -H "Content-Type: application/json" \
            -d '{
                "messages": [
                    {
                    "role": "user",
                    "content": "This is a test prompt"
                    }
                ],
                }
'
        """

        response = await self.async_handler.post(
            url=self.aporio_api_base + "/validate",
            data=_json_data,
            headers={
                "X-APORIA-API-KEY": self.aporio_api_key,
                "Content-Type": "application/json",
            },
        )
        verbose_proxy_logger.debug("Aporia AI response: %s", response.text)
        if response.status_code == 200:
            # check if the response was flagged
            _json_response = response.json()
            action: str = _json_response.get(
                "action"
            )  # possible values are modify, passthrough, block, rephrase
            if action == "block":
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": "Violated guardrail policy",
                        "aporio_ai_response": _json_response,
                    },
                )

    async def async_post_call_success_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        response,
    ):
        """
        Use this for the post call moderation with Guardrails
        """
        response_str: Optional[str] = convert_litellm_response_object_to_str(response)
        if response_str is not None:
            await self.make_aporia_api_request(
                response_string=response_str, new_messages=[]
            )

        pass

    async def async_moderation_hook(  ### 👈 KEY CHANGE ###
        self,
        data: dict,
        user_api_key_dict: UserAPIKeyAuth,
        call_type: Literal["completion", "embeddings", "image_generation"],
    ):

        if (
            await should_proceed_based_on_metadata(
                data=data,
                guardrail_name=GUARDRAIL_NAME,
            )
            is False
        ):
            return

        new_messages: Optional[List[dict]] = None
        if "messages" in data and isinstance(data["messages"], list):
            new_messages = self.transform_messages(messages=data["messages"])

        if new_messages is not None:
            await self.make_aporia_api_request(new_messages=new_messages)
        else:
            verbose_proxy_logger.warning(
                "Aporia AI: not running guardrail. No messages in data"
            )
            pass
