import base64
import json
import re
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

# Security Checkpoint Regex Patterns and Keywords
SSN_REGEX = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CC_REGEX = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
INJECTION_KEYWORDS = [
    "ignore",
    "override",
    "bypass",
    "system message",
    "system prompt",
    "developer instructions",
    "auto-approve",
    "auto approve",
    "force approve",
]


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
async def parse_input(
    ctx: Context, node_input: Any
) -> AsyncGenerator[Event | RequestInput, None]:
    """Parses incoming Pub/Sub style message (either plain JSON or base64-encoded 'data' key)."""
    # 1. Try to parse the current node_input
    parsed = {}
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
                pass
    elif isinstance(data_val, dict):
        parsed = data_val

    # 2. Check if this is a valid new expense report payload
    is_valid_new_expense = (
        isinstance(parsed, dict)
        and parsed.get("amount") is not None
        and parsed.get("submitter") is not None
    )

    if is_valid_new_expense:
        # A brand new expense report has been submitted, reset state for this cycle
        expense = ExpenseReport(
            amount=float(parsed.get("amount", 0)),
            submitter=str(parsed.get("submitter", "Unknown")),
            category=str(parsed.get("category", "General")),
            description=str(parsed.get("description", "")),
            date=str(parsed.get("date", "")),
        )
        route = "auto_approve" if expense.amount < THRESHOLD else "llm_review"
        yield Event(
            output=expense,
            route=route,
            state={
                "expense": expense.model_dump(),
                "security_alert": False,
                "redacted_categories": [],
                "risk_assessment": None,
                "decision": None,
            },
        )
        return

    # 3. If not a new payload, check if we already have the expense in state (resumption)
    if ctx.state and "expense" in ctx.state:
        expense = ExpenseReport(**ctx.state["expense"])
        route = "auto_approve" if expense.amount < THRESHOLD else "llm_review"
        yield Event(output=expense, route=route)
        return

    # 4. Check if we are resuming from the initial payload prompt
    if ctx.resume_inputs and "initial_payload" in ctx.resume_inputs:
        resume_val = ctx.resume_inputs["initial_payload"]
        try:
            parsed_resume = (
                json.loads(resume_val) if isinstance(resume_val, str) else resume_val
            )
            if (
                isinstance(parsed_resume, dict)
                and parsed_resume.get("amount") is not None
            ):
                expense = ExpenseReport(
                    amount=float(parsed_resume.get("amount", 0)),
                    submitter=str(parsed_resume.get("submitter", "Unknown")),
                    category=str(parsed_resume.get("category", "General")),
                    description=str(parsed_resume.get("description", "")),
                    date=str(parsed_resume.get("date", "")),
                )
                route = "auto_approve" if expense.amount < THRESHOLD else "llm_review"
                yield Event(
                    output=expense,
                    route=route,
                    state={"expense": expense.model_dump()},
                )
                return
        except Exception:
            pass

    # 5. Otherwise, request the initial payload
    yield RequestInput(
        interrupt_id="initial_payload",
        message="Welcome! Please provide the expense report JSON payload to begin.",
    )


@node
def auto_approve(node_input: ExpenseReport) -> Event:
    """Automatically approves expenses below the threshold ($100)."""
    return Event(output="approved", state={"decision": "approved"})


@node
def security_checkpoint(ctx: Context, node_input: ExpenseReport) -> Event:
    """Scrubs sensitive personal data and checks for prompt injection attempts."""
    description = node_input.description
    redacted_categories = []

    # 1. Scrub SSNs
    if SSN_REGEX.search(description):
        description = SSN_REGEX.sub("[REDACTED SSN]", description)
        redacted_categories.append("SSN")

    # 2. Scrub Credit Cards
    if CC_REGEX.search(description):
        description = CC_REGEX.sub("[REDACTED CREDIT CARD]", description)
        redacted_categories.append("Credit Card")

    # Update state with scrubbed description
    scrubbed_expense = node_input.model_copy(update={"description": description})
    state_updates = {"expense": scrubbed_expense.model_dump()}

    if redacted_categories:
        state_updates["redacted_categories"] = redacted_categories

    # 3. Check for prompt injection
    desc_lower = description.lower()
    is_injection = any(keyword in desc_lower for keyword in INJECTION_KEYWORDS)

    if is_injection:
        state_updates["security_alert"] = True
        assessment = {
            "risk_level": "CRITICAL",
            "risk_factors": ["Potential Prompt Injection Attack"],
            "explanation": "The expense description contains text that matches prompt injection signatures trying to override system rules.",
        }
        # Route straight to human review, bypassing the LLM
        return Event(output=assessment, route="injection_detected", state=state_updates)
    else:
        # Route to risk_reviewer (LLM)
        return Event(output=scrubbed_expense, route="clean", state=state_updates)


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
    is_security_alert = ctx.state.get("security_alert", False)
    redacted = ctx.state.get("redacted_categories", [])

    if not ctx.resume_inputs:
        prefix = "🚨 SECURITY ALERT & " if is_security_alert else "⚠️ "
        redact_info = f" [Redacted: {', '.join(redacted)}]" if redacted else ""

        alert_msg = (
            f"{prefix}ALERT: Expense of ${expense_dict.get('amount')} submitted by {expense_dict.get('submitter')} requires review.{redact_info}\n"
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
        (
            parse_input,
            {"auto_approve": auto_approve, "llm_review": security_checkpoint},
        ),
        # Security Checkpoint routes to either LLM review or directly to human
        (
            security_checkpoint,
            {"clean": risk_reviewer, "injection_detected": get_human_decision},
        ),
        # Approve branch merges to record_outcome
        (auto_approve, record_outcome),
        # Risk reviewer output goes to human decision
        (risk_reviewer, get_human_decision),
        # Human decision merges to record_outcome
        (get_human_decision, record_outcome),
    ],
)
