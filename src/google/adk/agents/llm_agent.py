# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any, Awaitable
from typing import AsyncGenerator
from typing import Callable
from typing import Literal
from typing import Optional
from typing import Union

from google.genai import types
from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator
from typing_extensions import override
from typing_extensions import TypeAlias

from ..code_executors.base_code_executor import BaseCodeExecutor
from ..events.event import Event
from ..examples.base_example_provider import BaseExampleProvider
from ..examples.example import Example
from ..flows.llm_flows.auto_flow import AutoFlow
from ..flows.llm_flows.base_llm_flow import BaseLlmFlow
from ..flows.llm_flows.single_flow import SingleFlow
from ..models.base_llm import BaseLlm
from ..models.llm_request import LlmRequest
from ..models.llm_response import LlmResponse
from ..models.registry import LLMRegistry
from ..planners.base_planner import BasePlanner
from ..tools.base_tool import BaseTool
from ..tools.function_tool import FunctionTool
from ..tools.tool_context import ToolContext
from .base_agent import BaseAgent
from .callback_context import CallbackContext
from .invocation_context import InvocationContext
from .readonly_context import ReadonlyContext

logger = logging.getLogger(__name__)


BeforeModelCallback: TypeAlias = Callable[
    [CallbackContext, LlmRequest], Optional[LlmResponse]
]
AfterModelCallback: TypeAlias = Callable[
    [CallbackContext, LlmResponse],
    Optional[LlmResponse],
]
BeforeToolCallback: TypeAlias = Callable[
    [BaseTool, dict[str, Any], ToolContext],
    Optional[dict],
]
AfterToolCallback: TypeAlias = Callable[
    [BaseTool, dict[str, Any], ToolContext, dict],
    Optional[dict],
]

InstructionProvider: TypeAlias = Callable[[ReadonlyContext], str]

ToolUnion: TypeAlias = Union[Callable, BaseTool]
ExamplesUnion = Union[list[Example], BaseExampleProvider]


def _convert_tool_union_to_tool(
    tool_union: ToolUnion,
) -> BaseTool:
  return (
      tool_union
      if isinstance(tool_union, BaseTool)
      else FunctionTool(tool_union)
  )


class LlmAgent(BaseAgent):
  """LLM-based Agent."""

  model: Union[str, BaseLlm] = ''
  """The model to use for the agent.

  When not set, the agent will inherit the model from its ancestor.
  """

  instruction: Union[str, InstructionProvider] = ''
  """Instructions for the LLM model, guiding the agent's behavior."""

  global_instruction: Union[str, InstructionProvider] = ''
  """Instructions for all the agents in the entire agent tree.

  global_instruction ONLY takes effect in root agent.

  For example: use global_instruction to make all agents have a stable identity
  or personality.
  """

  tools: list[ToolUnion] = Field(default_factory=list)
  """Tools available to this agent."""

  exit_stack: Optional[AsyncExitStack] = None
  """An AsyncExitStack instance used within the async_setup hook to manage 
  asynchronous resources and connections during the agent's setup phase.
  """

  generate_content_config: Optional[types.GenerateContentConfig] = None
  """The additional content generation configurations.

  NOTE: not all fields are usable, e.g. tools must be configured via `tools`,
  thinking_config must be configured via `planner` in LlmAgent.

  For example: use this config to adjust model temperature, configure safety
  settings, etc.
  """

  # LLM-based agent transfer configs - Start
  disallow_transfer_to_parent: bool = False
  """Disallows LLM-controlled transferring to the parent agent."""
  disallow_transfer_to_peers: bool = False
  """Disallows LLM-controlled transferring to the peer agents."""
  # LLM-based agent transfer configs - End

  include_contents: Literal['default', 'none'] = 'default'
  """Whether to include contents in the model request.

  When set to 'none', the model request will not include any contents, such as
  user messages, tool results, etc.
  """

  # Controlled input/output configurations - Start
  input_schema: Optional[type[BaseModel]] = None
  """The input schema when agent is used as a tool."""
  output_schema: Optional[type[BaseModel]] = None
  """The output schema when agent replies.

  NOTE: when this is set, agent can ONLY reply and CANNOT use any tools, such as
  function tools, RAGs, agent transfer, etc.
  """
  output_key: Optional[str] = None
  """The key in session state to store the output of the agent.

  Typically use cases:
  - Extracts agent reply for later use, such as in tools, callbacks, etc.
  - Connects agents to coordinate with each other.
  """
  # Controlled input/output configurations - End

  # Advance features - Start
  planner: Optional[BasePlanner] = None
  """Instructs the agent to make a plan and execute it step by step.

  NOTE: to use model's built-in thinking features, set the `thinking_config`
  field in `google.adk.planners.built_in_planner`.

  """

  code_executor: Optional[BaseCodeExecutor] = None
  """Allow agent to execute code blocks from model responses using the provided
  CodeExecutor.

  Check out available code executions in `google.adk.code_executor` package.

  NOTE: to use model's built-in code executor, don't set this field, add
  `google.adk.tools.built_in_code_execution` to tools instead.
  """
  # Advance features - End

  # TODO: remove below fields after migration. - Start
  # These fields are added back for easier migration.
  examples: Optional[ExamplesUnion] = None
  # TODO: remove above fields after migration. - End

  # Callbacks - Start
  before_model_callback: Optional[BeforeModelCallback] = None
  """Called before calling the LLM.
  Args:
    callback_context: CallbackContext,
    llm_request: LlmRequest, The raw model request. Callback can mutate the
    request.

  Returns:
    The content to return to the user. When present, the model call will be
    skipped and the provided content will be returned to user.
  """
  after_model_callback: Optional[AfterModelCallback] = None
  """Called after calling LLM.

  Args:
    callback_context: CallbackContext,
    llm_response: LlmResponse, the actual model response.

  Returns:
    The content to return to the user. When present, the actual model response
    will be ignored and the provided content will be returned to user.
  """
  before_tool_callback: Optional[BeforeToolCallback] = None
  """Called before the tool is called.

  Args:
    tool: The tool to be called.
    args: The arguments to the tool.
    tool_context: ToolContext,

  Returns:
    The tool response. When present, the returned tool response will be used and
    the framework will skip calling the actual tool.
  """
  after_tool_callback: Optional[AfterToolCallback] = None
  """Called after the tool is called.

  Args:
    tool: The tool to be called.
    args: The arguments to the tool.
    tool_context: ToolContext,
    tool_response: The response from the tool.

  Returns:
    When present, the returned dict will be used as tool result.
  """
  # Callbacks - End

  async_setup: Optional[Callable[[LlmAgent], Awaitable[Optional[AsyncExitStack]]]] = None
  """
  An asynchronous setup hook to initialize resources or connections during the agent's startup phase. 
  For example, the following function can be used to initialize MCP server tools and manage connections:"
  ```
  async def get_mcp_server_tools(agent: LlmAgent) -> AsyncExitStack:
      stack = AsyncExitStack()
      tools, _ = await MCPToolset.from_server(connection_params=parameters, async_exit_stack=stack)
      agent.tools.extend(tools)
      return stack
  
  # Example usage of async_setup in LlmAgent
  root_agent = LlmAgent(
      model="your model",
      name="my_agent",
      instruction="You are a helpful assistant",
      async_setup=get_mcp_server_tools
  )
  ```
  """

  async def __aenter__(self):
    if callable(self.async_setup):
      self.exit_stack = await self.async_setup(self)
    return self

  async def __aexit__(self, exc_type, exc_val, exc_tb):
    if self.exit_stack is not None:
      await self.exit_stack.aclose()

  @override
  async def _run_async_impl(
      self, ctx: InvocationContext
  ) -> AsyncGenerator[Event, None]:
    async for event in self._llm_flow.run_async(ctx):
      self.__maybe_save_output_to_state(event)
      yield event

  @override
  async def _run_live_impl(
      self, ctx: InvocationContext
  ) -> AsyncGenerator[Event, None]:
    async for event in self._llm_flow.run_live(ctx):
      self.__maybe_save_output_to_state(event)
      yield event
    if ctx.end_invocation:
      return

  @property
  def canonical_model(self) -> BaseLlm:
    """The resolved self.model field as BaseLlm.

    This method is only for use by Agent Development Kit.
    """
    if isinstance(self.model, BaseLlm):
      return self.model
    elif self.model:  # model is non-empty str
      return LLMRegistry.new_llm(self.model)
    else:  # find model from ancestors.
      ancestor_agent = self.parent_agent
      while ancestor_agent is not None:
        if isinstance(ancestor_agent, LlmAgent):
          return ancestor_agent.canonical_model
        ancestor_agent = ancestor_agent.parent_agent
      raise ValueError(f'No model found for {self.name}.')

  def canonical_instruction(self, ctx: ReadonlyContext) -> str:
    """The resolved self.instruction field to construct instruction for this agent.

    This method is only for use by Agent Development Kit.
    """
    if isinstance(self.instruction, str):
      return self.instruction
    else:
      return self.instruction(ctx)

  def canonical_global_instruction(self, ctx: ReadonlyContext) -> str:
    """The resolved self.instruction field to construct global instruction.

    This method is only for use by Agent Development Kit.
    """
    if isinstance(self.global_instruction, str):
      return self.global_instruction
    else:
      return self.global_instruction(ctx)

  @property
  def canonical_tools(self) -> list[BaseTool]:
    """The resolved self.tools field as a list of BaseTool.

    This method is only for use by Agent Development Kit.
    """
    return [_convert_tool_union_to_tool(tool) for tool in self.tools]

  @property
  def _llm_flow(self) -> BaseLlmFlow:
    if (
        self.disallow_transfer_to_parent
        and self.disallow_transfer_to_peers
        and not self.sub_agents
    ):
      return SingleFlow()
    else:
      return AutoFlow()

  def __maybe_save_output_to_state(self, event: Event):
    """Saves the model output to state if needed."""
    if (
        self.output_key
        and event.is_final_response()
        and event.content
        and event.content.parts
    ):
      result = ''.join(
          [part.text if part.text else '' for part in event.content.parts]
      )
      if self.output_schema:
        result = self.output_schema.model_validate_json(result).model_dump(
            exclude_none=True
        )
      event.actions.state_delta[self.output_key] = result

  @model_validator(mode='after')
  def __model_validator_after(self) -> LlmAgent:
    self.__check_output_schema()
    return self

  def __check_output_schema(self):
    if not self.output_schema:
      return

    if (
        not self.disallow_transfer_to_parent
        or not self.disallow_transfer_to_peers
    ):
      logger.warning(
          'Invalid config for agent %s: output_schema cannot co-exist with'
          ' agent transfer configurations. Setting'
          ' disallow_transfer_to_parent=True, disallow_transfer_to_peers=True',
          self.name,
      )
      self.disallow_transfer_to_parent = True
      self.disallow_transfer_to_peers = True

    if self.sub_agents:
      raise ValueError(
          f'Invalid config for agent {self.name}: if output_schema is set,'
          ' sub_agents must be empty to disable agent transfer.'
      )

    if self.tools:
      raise ValueError(
          f'Invalid config for agent {self.name}: if output_schema is set,'
          ' tools must be empty'
      )

  @field_validator('generate_content_config', mode='after')
  @classmethod
  def __validate_generate_content_config(
      cls, generate_content_config: Optional[types.GenerateContentConfig]
  ) -> types.GenerateContentConfig:
    if not generate_content_config:
      return types.GenerateContentConfig()
    if generate_content_config.thinking_config:
      raise ValueError('Thinking config should be set via LlmAgent.planner.')
    if generate_content_config.tools:
      raise ValueError('All tools must be set via LlmAgent.tools.')
    if generate_content_config.system_instruction:
      raise ValueError(
          'System instruction must be set via LlmAgent.instruction.'
      )
    if generate_content_config.response_schema:
      raise ValueError(
          'Response schema must be set via LlmAgent.output_schema.'
      )
    return generate_content_config


Agent: TypeAlias = LlmAgent
