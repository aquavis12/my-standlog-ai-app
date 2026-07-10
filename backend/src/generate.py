"""StandLog - generate Lambda.

POST /generate
  {
    "mode": "standup" | "weekly" | "ticket_reply",
    "start": "YYYY-MM-DD",          # inclusive
    "end": "YYYY-MM-DD",            # inclusive
    "audience": "optional free text, e.g. 'client primary contact at ESAG'",
    "tag": "optional tag filter, e.g. 'client-x'"
  }

Pulls matching entries from DynamoDB, then asks a Bedrock model (Converse API)
to produce the requested artifact. Returns {"output": "...", "entry_count": n}.
"""

import json
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

TABLE_NAME = os.environ["TABLE_NAME"]
MODEL_ID = os.environ["MODEL_ID"]
PK = "USER#default"

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
bedrock = boto3.client("bedrock-runtime")

PROMPTS = {
    "standup": (
        "You are an assistant that writes daily standup updates for a cloud "
        "consultant. From the journal entries below, write a concise standup "
        "update with three sections: 'Yesterday/Done', 'Today/Next', and "
        "'Blockers'. Group related items, drop noise, keep it under 150 words. "
        "Plain text, hyphen bullets, no preamble."
    ),
    "weekly": (
        "You are an assistant that writes weekly work summaries for a cloud "
        "consultant. From the journal entries below, write a weekly summary "
        "with sections: 'Highlights', 'Delivered', 'In progress', 'Risks & "
        "blockers'. Merge duplicates, keep it under 250 words, professional "
        "tone suitable for a manager. Plain text, no preamble."
    ),
    "ticket_reply": (
        "You are an assistant that drafts client-facing ticket reply emails "
        "for an AWS consulting engineer. From the journal entries below, "
        "draft a professional, courteous ticket update email: brief context, "
        "work performed, findings, and next steps. Do not invent technical "
        "details that are not in the entries. Under 220 words. Start with a "
        "subject line ('Subject: ...'), then the body. No preamble."
    ),
}


def _response(status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _fetch_entries(start: str, end: str, tag: str | None) -> list[dict]:
    key_cond = Key("pk").eq(PK) & Key("sk").between(
        f"ENTRY#{start}", f"ENTRY#{end}T23:59:59Z~"
    )
    resp = table.query(KeyConditionExpression=key_cond, Limit=300)
    items = resp.get("Items", [])
    if tag:
        items = [i for i in items if tag in (i.get("tags") or [])]
    return items


def _format_entries(items: list[dict]) -> str:
    lines = []
    for it in items:
        tags = ",".join(it.get("tags") or [])
        tag_part = f" [{tags}]" if tags else ""
        lines.append(
            f"- ({it.get('created_at', '?')}) [{it.get('kind', 'note')}]{tag_part} "
            f"{it.get('text', '')}"
        )
    return "\n".join(lines)


def handler(event, _context):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Request body must be valid JSON."})

    mode = body.get("mode", "standup")
    if mode not in PROMPTS:
        return _response(400, {"error": f"mode must be one of {sorted(PROMPTS)}"})

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = body.get("start") or today
    end = body.get("end") or today
    tag = body.get("tag") or None
    audience = (body.get("audience") or "").strip()

    entries = _fetch_entries(start, end, tag)
    if not entries:
        return _response(200, {
            "output": "No journal entries found for that range - log a few notes first.",
            "entry_count": 0,
        })

    user_prompt = (
        f"Date range: {start} to {end}\n"
        + (f"Intended audience: {audience}\n" if audience else "")
        + "Journal entries:\n"
        + _format_entries(entries)
    )

    kwargs = dict(
        modelId=MODEL_ID,
        system=[{"text": PROMPTS[mode]}],
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        inferenceConfig={"maxTokens": 800, "temperature": 0.3},
    )
    guardrail_id = os.environ.get("GUARDRAIL_ID", "")
    if guardrail_id:
        kwargs["guardrailConfig"] = {
            "guardrailIdentifier": guardrail_id,
            "guardrailVersion": os.environ.get("GUARDRAIL_VERSION", "DRAFT"),
        }
    resp = bedrock.converse(**kwargs)

    output = "".join(
        block.get("text", "")
        for block in resp["output"]["message"]["content"]
    ).strip()

    return _response(200, {"output": output, "entry_count": len(entries),
                        "mode": mode, "stop_reason": resp.get("stopReason", "")})
