import os
import re
import time
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union
from uuid import UUID

from dotenv import load_dotenv
from langchain import LLMChain
from langchain.agents import AgentExecutor, AgentOutputParser, LLMSingleActionAgent
from langchain.input import get_color_mapping
from langchain.output_parsers import OutputFixingParser
from langchain.prompts import BaseChatPromptTemplate
from langchain.schema import AgentAction, AgentFinish, HumanMessage
from langchain.tools import BaseTool
from pydantic import BaseModel
from typing_extensions import override

from src.world.context import WorldContext

from ..event.base import Event, EventsManager
from ..memory.base import MemoryType
from ..tools.base import TOOLS, CustomTool
from ..tools.context import ToolContext
from ..tools.name import ToolName
from ..utils.colors import LogColor
from ..utils.formatting import print_to_console
from ..utils.model_name import ChatModelName
from ..utils.models import ChatModel
from ..utils.parameters import DEFAULT_SMART_MODEL
from ..utils.prompt import PromptString
from ..world.context import WorldContext
from .plans import PlanStatus, SinglePlan

load_dotenv()


# Set up a prompt template
class CustomPromptTemplate(BaseChatPromptTemplate):
    # The template to use
    template: str
    # The list of tools available
    tools: List[BaseTool]

    def format_messages(self, **kwargs) -> str:
        # Get the intermediate steps (AgentAction, Observation tuples)
        # Format them in a particular way
        intermediate_steps = kwargs.pop("intermediate_steps")
        thoughts = ""
        for action, observation in intermediate_steps:
            thoughts += action.log
            thoughts += f"\nObservation: {observation}\nThought: "
        # Set the agent_scratchpad variable to that value
        kwargs["agent_scratchpad"] = thoughts
        # Create a tools variable from the list of tools provided
        kwargs["tools"] = "\n".join(
            [f"{tool.name}: {tool.description}" for tool in self.tools]
        )
        # Create a list of tool names for the tools provided
        kwargs["tool_names"] = ", ".join([tool.name for tool in self.tools])
        formatted = self.template.format(**kwargs)
        return [HumanMessage(content=formatted)]


# set up the output parser
class CustomOutputParser(AgentOutputParser):
    def parse(self, llm_output: str) -> Union[AgentAction, AgentFinish]:
        # Check if agent should finish
        if "Final Response:" in llm_output:
            return AgentFinish(
                # Return values is generally always a dictionary with a single `output` key
                # It is not recommended to try anything else at the moment :)
                return_values={
                    "output": llm_output.split("Final Response:")[-1].strip()
                },
                log=llm_output,
            )
        # Parse out the action and action input
        regex = r"Action\s*\d*\s*:(.*?)\nAction\s*\d*\s*Input\s*\d*\s*:[\s]*(.*)"
        match = re.search(regex, llm_output, re.DOTALL)
        if not match:
            raise ValueError(f"Could not parse LLM output: `{llm_output}`")
        action = match.group(1).strip()
        action_input = match.group(2)
        # Return the action and action input
        return AgentAction(
            tool=action, tool_input=action_input.strip(" ").strip('"'), log=llm_output
        )


class PlanExecutorResponse(BaseModel):
    status: PlanStatus
    output: str
    tool: Optional[CustomTool]
    tool_input: Optional[str]


class PlanExecutor(BaseModel):
    """Executes plans for an agent."""

    agent_id: UUID
    context: WorldContext
    plan: Optional[SinglePlan] = None
    intermediate_steps: List[Tuple[AgentAction, str]] = []

    def __init__(self, agent_id: UUID, context: WorldContext) -> None:
        super().__init__(agent_id=agent_id, context=context)

    def get_executor(self, tools: list[CustomTool]) -> LLMSingleActionAgent:
        prompt = CustomPromptTemplate(
            template=PromptString.EXECUTE_PLAN.value,
            tools=tools,
            # This omits the `agent_scratchpad`, `tools`, and `tool_names` variables because those are generated dynamically
            # This includes the `intermediate_steps` variable because that is needed
            input_variables=["input", "intermediate_steps", "location_context"],
        )

        # set up a simple completion llm
        llm = ChatModel(model_name=DEFAULT_SMART_MODEL, temperature=0).defaultModel

        # LLM chain consisting of the LLM and a prompt
        llm_chain = LLMChain(llm=llm, prompt=prompt)

        output_parser = CustomOutputParser()

        executor = LLMSingleActionAgent(
            llm_chain=llm_chain,
            output_parser=output_parser,
            stop=["\nObservation:"],
        )
        return executor

    def set_plan(self, plan: SinglePlan) -> None:
        self.plan = plan
        self.intermediate_steps = []

    def start_or_continue_plan(
        self, plan: SinglePlan, tools: list[CustomTool]
    ) -> PlanExecutorResponse:
        if not self.plan or self.plan.description != plan.description:
            self.set_plan(plan)
        return self.execute(tools)

    def execute(self, tools: list[CustomTool]) -> str:
        if self.plan is None:
            raise ValueError("No plan set")

        executor = self.get_executor(tools=tools)

        response = executor.plan(
            input=self.plan.description,
            intermediate_steps=self.intermediate_steps,
            location_context=self.context.location_context_string(self.agent_id),
        )

        agent_name = self.context.get_agent_full_name(self.agent_id)

        for log in response.log.split("\n"):
            suffix = log.split(":")[0] if ":" in log else "Thought"
            print_to_console(f"{agent_name}: {suffix}: ", LogColor.THOUGHT, log)

        # If the agent is finished, return the output
        if isinstance(response, AgentFinish):
            self.plan = None
            self.intermediate_steps = []

            output = response.return_values.get("output")

            if output is None:
                raise ValueError(f"No output found in return values: {response}")

            if "Need Help" in output:
                return PlanExecutorResponse(status=PlanStatus.FAILED, output=output)
            else:
                return PlanExecutorResponse(status=PlanStatus.DONE, output=output)

        try:
            tool = TOOLS[ToolName(response.tool.lower())]
        except ValueError:
            raise ValueError(f"Tool: '{response.tool}' is not found in tool list")

        tool_context = ToolContext(
            agent_id=self.agent_id,
            context=self.context,
        )

        result = tool.run(response.tool_input, tool_context)

        print_to_console(f"{agent_name}: Action Response: ", LogColor.THOUGHT, result)

        self.intermediate_steps.append((response, result))

        return PlanExecutorResponse(
            status=PlanStatus.IN_PROGRESS,
            output=result,
            tool=tool,
            tool_input=response.tool_input,
        )
