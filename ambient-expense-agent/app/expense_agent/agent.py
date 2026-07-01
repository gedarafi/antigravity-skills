import base64
import json
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.workflow import Workflow, node
from google.genai import types
from pydantic import BaseModel, Field

from .config import MODEL_NAME, THRESHOLD


# Pydantic Schemas for validation and auto-conversion
class ExpenseReport(BaseModel):
    amount: float = Field(description="The total monetary value of the expense")
    submitter: str = Field(
        description="The name/email of the person submitting the expense"
    )
    category: str = Field(description="The budget category for the expense")
    description: str = Field(description="Short description of the expense item")
    date: str = Field(description="The date of the transaction")


class RiskAssessment(BaseModel):
    risk_level: str = Field(description="The assessed risk level: LOW, MEDIUM, or HIGH")
    risk_factors: list[str] = Field(description="List of risk factors identified")
    explanation: str = Field(
        description="Reasoning or explanation behind this risk rating"
    )


# --- Node Implementations ---


@node
def parse_input(ctx: Context, node_input: Any) -> Event:
    """Parses incoming Pub/Sub style message (either plain JSON or base64-encoded 'data' key)."""
    # If we already have the expense in state, preserve and pass it through
    if ctx.state and "expense" in ctx.state:
        expense = ExpenseReport(**ctx.state["expense"])
        route = "auto_approve" if expense.amount < THRESHOLD else "llm_review"
        return Event(output=expense, route=route)

    raw_data = None
    if isinstance(node_input, types.Content):
        text = "".join(part.text for part in node_input.parts if part.text)
        try:
            raw_data = json.loads(text)
        except Exception:
            raw_data = text
    else:
        raw_data = node_input

    # Parse raw string format
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except Exception:
            pass

    # Extract Pub/Sub message structure if wrapped
    if isinstance(raw_data, dict):
        if (
            "message" in raw_data
            and isinstance(raw_data["message"], dict)
            and "data" in raw_data["message"]
        ):
            data_val = raw_data["message"]["data"]
        elif "data" in raw_data:
            data_val = raw_data["data"]
        else:
            data_val = raw_data
    else:
        data_val = raw_data

    # Decrypt base64 encoding if needed
    if isinstance(data_val, str):
        try:
            decoded = base64.b64decode(data_val).decode("utf-8")
            parsed = json.loads(decoded)
        except Exception:
            try:
                parsed = json.loads(data_val)
            except Exception:
                parsed = {}
    elif isinstance(data_val, dict):
        parsed = data_val
    else:
        parsed = {}

    # Cast to Pydantic Model
    expense = ExpenseReport(
        amount=float(parsed.get("amount", 0)),
        submitter=str(parsed.get("submitter", "Unknown")),
        category=str(parsed.get("category", "General")),
        description=str(parsed.get("description", "")),
        date=str(parsed.get("date", "")),
    )

    # Route conditionally
    route = "auto_approve" if expense.amount < THRESHOLD else "llm_review"

    return Event(output=expense, route=route, state={"expense": expense.model_dump()})


@node
def auto_approve(node_input: ExpenseReport) -> Event:
    """Automatically approves expenses below the threshold ($100)."""
    return Event(output="approved", state={"decision": "approved"})


# LLM Risk Review Agent (using LlmAgent)
risk_reviewer = LlmAgent(
    name="risk_reviewer",
    model=MODEL_NAME,
    instruction=(
        "You are an expense compliance risk agent. Review the following expense report for potential risk factors "
        "(e.g., policy compliance, unusual category, vague description). Rate the risk_level as LOW, MEDIUM, or HIGH, "
        "and list specific risk factors."
    ),
    output_schema=RiskAssessment,
    output_key="risk_assessment",
)


@node
async def get_human_decision(
    ctx: Context, node_input: dict
) -> AsyncGenerator[Event | RequestInput, None]:
    """Alerts the human reviewer with LLM risk assessment, and pauses the workflow for human approval."""
    expense_dict = ctx.state.get("expense", {})

    if not ctx.resume_inputs:
        # Alert layout/message
        alert_msg = (
            f"⚠️ ALERT: Expense of ${expense_dict.get('amount')} submitted by {expense_dict.get('submitter')} requires review.\n"
            f"Risk Level: {node_input.get('risk_level')}\n"
            f"Factors: {', '.join(node_input.get('risk_factors', []))}\n"
            f"Explanation: {node_input.get('explanation')}"
        )
        # Render the alert message on UI/terminal
        yield Event(
            content=types.Content(
                role="model", parts=[types.Part.from_text(text=alert_msg)]
            )
        )
        # Interrupt/pause the graph
        yield RequestInput(
            interrupt_id="human_approval",
            message="Do you approve or reject this expense? (Type 'approve' or 'reject')",
        )
        return

    # Once resumed
    user_decision = ctx.resume_inputs.get("human_approval", "").strip().lower()
    decision = "approved" if "approve" in user_decision else "rejected"

    yield Event(output=decision, state={"decision": decision})


@node
def record_outcome(ctx: Context, node_input: str) -> Event:
    """Records and logs the final outcome of the expense approval process."""
    expense_dict = ctx.state.get("expense", {})
    msg = f"Outcome recorded: Expense of ${expense_dict.get('amount')} by {expense_dict.get('submitter')} was {node_input}."
    return Event(
        content=types.Content(role="model", parts=[types.Part.from_text(text=msg)]),
        output=msg,
    )


# --- Workflow Graph Topology Definition ---

root_agent = Workflow(
    name="expense_approval_workflow",
    description="Graph workflow to evaluate and process expense approvals.",
    edges=[
        ("START", parse_input),
        # Branching based on route from parse_input
        (parse_input, {"auto_approve": auto_approve, "llm_review": risk_reviewer}),
        # Approve branch merges to record_outcome
        (auto_approve, record_outcome),
        # Review branch goes to human, then merges to record_outcome
        (risk_reviewer, get_human_decision),
        (get_human_decision, record_outcome),
    ],
)
