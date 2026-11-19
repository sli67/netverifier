"""Model calls that must come back as an instance of a pydantic schema."""

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, ValidationError

# Attempts at getting a usable answer from the model within one call.
MAX_TRIES = 3


class StructuredError(Exception):
    """The model did not return a usable instance of the schema."""


def make_ask(model: BaseChatModel):
    """ask(schema, system, user): one model call that must come back as an instance of schema."""

    def ask(schema: type[BaseModel], system: str, user: str) -> BaseModel:
        # The schema is offered as a tool the model is told to call. Forcing the
        # call through tool_choice is not used: DeepSeek's thinking mode rejects it.
        bound = model.bind_tools([schema])
        name = schema.__name__
        messages: list[Any] = [
            SystemMessage(f"{system}\n\nGive your answer by calling the {name} tool. Do not answer in plain text."),
            HumanMessage(user),
        ]
        problem: Any = "the model did not call the tool"
        for _ in range(MAX_TRIES):
            reply = bound.invoke(messages)
            call = next((c for c in reply.tool_calls if c["name"] == name), None)
            if call is None:
                messages += [reply, HumanMessage(f"Call the {name} tool with your answer.")]
                continue
            try:
                return schema(**call["args"])
            except ValidationError as error:
                problem = error
                correction = f"The arguments were not valid: {error}. Call {name} again with corrected arguments."
                messages += [reply, ToolMessage(correction, tool_call_id=call["id"])]
        raise StructuredError(f"The model did not return a usable {name}: {problem}"[:500])

    return ask
