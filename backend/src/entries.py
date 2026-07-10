"""StandLog - entries CRUD Lambda.

Routes (HTTP API v2 payload):
  GET    /entries?start=YYYY-MM-DD&end=YYYY-MM-DD   list entries in a date range
  POST   /entries   {"text": "...", "tags": ["client-x"], "kind": "task|blocker|win|note"}
  DELETE /entries/{sk}

Single-table layout:
  pk = "USER#default"           (swap for a Cognito sub when you add auth)
  sk = "ENTRY#<iso8601-utc>#<short-id>"
"""

import json
import os
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

TABLE_NAME = os.environ["TABLE_NAME"]
PK = "USER#default"

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)

VALID_KINDS = {"task", "blocker", "win", "note"}


def _response(status: int, body) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_entries(params: dict) -> dict:
    start = params.get("start")  # YYYY-MM-DD
    end = params.get("end")

    key_cond = Key("pk").eq(PK)
    if start and end:
        key_cond &= Key("sk").between(f"ENTRY#{start}", f"ENTRY#{end}T23:59:59Z~")
    elif start:
        key_cond &= Key("sk").gte(f"ENTRY#{start}")
    else:
        key_cond &= Key("sk").begins_with("ENTRY#")

    items = []
    resp = table.query(KeyConditionExpression=key_cond, ScanIndexForward=False, Limit=200)
    items.extend(resp.get("Items", []))
    return _response(200, {"entries": items})


def create_entry(body: dict) -> dict:
    text = (body.get("text") or "").strip()
    if not text:
        return _response(400, {"error": "Field 'text' is required."})
    if len(text) > 2000:
        return _response(400, {"error": "Entry text is limited to 2000 characters."})

    kind = body.get("kind", "task")
    if kind not in VALID_KINDS:
        kind = "note"

    tags = body.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    tags = [str(t)[:50] for t in tags][:10]

    ts = _now_iso()
    item = {
        "pk": PK,
        "sk": f"ENTRY#{ts}#{uuid.uuid4().hex[:6]}",
        "text": text,
        "kind": kind,
        "tags": tags,
        "created_at": ts,
    }
    table.put_item(Item=item)
    return _response(201, {"entry": item})


def delete_entry(sk: str) -> dict:
    if not sk or not sk.startswith("ENTRY#"):
        return _response(400, {"error": "Invalid entry key."})
    table.delete_item(Key={"pk": PK, "sk": sk})
    return _response(200, {"deleted": sk})


def handler(event, _context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "")
    params = event.get("queryStringParameters") or {}

    try:
        if method == "GET" and path == "/entries":
            return list_entries(params)

        if method == "POST" and path == "/entries":
            body = json.loads(event.get("body") or "{}")
            return create_entry(body)

        if method == "DELETE" and path.startswith("/entries/"):
            sk = event.get("pathParameters", {}).get("sk", "")
            return delete_entry(sk)

        return _response(404, {"error": "Not found"})
    except json.JSONDecodeError:
        return _response(400, {"error": "Request body must be valid JSON."})
    except Exception as exc:  # noqa: BLE001 - surface a clean 500
        print(f"ERROR: {exc}")
        return _response(500, {"error": "Internal error"})
