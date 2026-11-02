"""Quizzy backend.

S3 (lecture storage, one folder per chat)
  -> Bedrock Knowledge Base (chunk + embed + vector search, RAG)
  -> Bedrock Converse API (Claude writes quizzes and explanations)
"""
import json
import os
import random
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()


def need(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing environment variable {name}. Copy .env.example to .env and fill it in.")
    return value


REGION = os.environ.get("AWS_REGION", "ap-southeast-1")
BUCKET = need("QUIZZY_BUCKET")
KB_ID = need("QUIZZY_KB_ID")
DS_ID = need("QUIZZY_DS_ID")
MODEL_ID = os.environ.get("QUIZZY_MODEL_ID", "global.anthropic.claude-sonnet-5-5")

MAX_CHATS = 3
MAX_FILE_BYTES = 25 * 1024 * 1024
# Formats Bedrock Knowledge Bases can parse. NOTE: .pptx is NOT supported: export slides to PDF first.
ALLOWED_EXT = {".pdf", ".docx", ".doc", ".txt", ".md", ".html", ".csv", ".xlsx", ".xls"}
CHAT_ID_RE = re.compile(r"^[a-f0-9]{12}$")

# S3 layout (one bucket):
#   chats/<chat_id>/<file>                  <- the Knowledge Base data source points at prefix "chats/"
#   chats/<chat_id>/<file>.metadata.json    <- sidecar: tells the KB which chat the file belongs to
#   meta/<chat_id>.json                     <- chat record (kept OUTSIDE chats/ so it is never ingested)
#   deploy/                                 <- optional: app zip for EC2

cfg = Config(region_name=REGION, retries={"max_attempts": 5, "mode": "standard"})
s3 = boto3.client("s3", config=cfg)                            # storage
agent = boto3.client("bedrock-agent", config=cfg)              # control plane: ingestion jobs
agent_rt = boto3.client("bedrock-agent-runtime", config=cfg)   # data plane: Retrieve
brt = boto3.client("bedrock-runtime", config=cfg)              # Converse (LLM)

app = FastAPI(title="Quizzy")


# ---------------------------------------------------------------- errors
@app.exception_handler(ClientError)
async def aws_client_error(_, exc: ClientError):
    err = exc.response.get("Error", {})
    return JSONResponse(status_code=502, content={"detail": f"AWS {err.get('Code')}: {err.get('Message')}"})


@app.exception_handler(BotoCoreError)
async def aws_core_error(_, exc: BotoCoreError):
    return JSONResponse(status_code=502, content={"detail": f"AWS config/credentials problem: {exc}"})


# ---------------------------------------------------------------- chat records (S3 JSON)
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def meta_key(chat_id: str) -> str:
    return f"meta/{chat_id}.json"


def chat_prefix(chat_id: str) -> str:
    return f"chats/{chat_id}/"


def load_chat(chat_id: str) -> dict:
    if not CHAT_ID_RE.match(chat_id):
        raise HTTPException(404, "Chat not found")
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=meta_key(chat_id))
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            raise HTTPException(404, "Chat not found")
        raise
    return json.loads(obj["Body"].read())


def save_chat(chat: dict) -> None:
    s3.put_object(
        Bucket=BUCKET,
        Key=meta_key(chat["id"]),
        Body=json.dumps(chat).encode(),
        ContentType="application/json",
    )


def list_chats() -> list[dict]:
    chats = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix="meta/"):
        for obj in page.get("Contents", []):
            body = s3.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read()
            chats.append(json.loads(body))
    return sorted(chats, key=lambda c: c["created_at"])


def list_files(chat_id: str) -> list[dict]:
    files = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=chat_prefix(chat_id)):
        for obj in page.get("Contents", []):
            name = obj["Key"][len(chat_prefix(chat_id)):]
            if name and not name.endswith(".metadata.json"):
                files.append({"name": name, "size": obj["Size"]})
    return files


# ---------------------------------------------------------------- Knowledge Base sync
def latest_job() -> dict | None:
    resp = agent.list_ingestion_jobs(
        knowledgeBaseId=KB_ID,
        dataSourceId=DS_ID,
        sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
        maxResults=1,
    )
    items = resp.get("ingestionJobSummaries", [])
    return items[0] if items else None


def compute_state(chat: dict) -> dict:
    """empty | indexing | pending | ready | failed  (read-only)."""
    uploaded = chat.get("last_upload_at")
    if not uploaded:
        return {"state": "empty", "failed_docs": 0}
    job = latest_job()
    if job and job["status"] in ("STARTING", "IN_PROGRESS", "STOPPING"):
        return {"state": "indexing", "failed_docs": 0}
    if job and job["startedAt"] >= datetime.fromisoformat(uploaded):
        failed = job.get("statistics", {}).get("numberOfDocumentsFailed", 0)
        ok = job["status"] == "COMPLETE"
        return {"state": "ready" if ok else "failed", "failed_docs": failed}
    return {"state": "pending", "failed_docs": 0}  # no job has seen the latest upload yet


def start_sync() -> None:
    """Start an ingestion job. Only ONE job can run per data source, so a conflict just means 'one is already running'."""
    try:
        agent.start_ingestion_job(knowledgeBaseId=KB_ID, dataSourceId=DS_ID)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConflictException":
            raise


# ---------------------------------------------------------------- retrieval + LLM helpers
def retrieve(chat_id: str, query: str, k: int = 8) -> list[dict]:
    """Vector search in the Knowledge Base, restricted to this chat's documents via the chat_id metadata filter."""
    resp = agent_rt.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text": query[:1000]},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": k,
                "filter": {"equals": {"key": "chat_id", "value": chat_id}},
            }
        },
    )
    out = []
    for r in resp.get("retrievalResults", []):
        uri = r.get("location", {}).get("s3Location", {}).get("uri", "")
        out.append({"text": r["content"]["text"], "source": uri.rsplit("/", 1)[-1], "score": r.get("score")})
    return out


def converse(system: str, user: str, max_tokens: int = 800, tool: dict | None = None) -> dict:
    kwargs = dict(
        modelId=MODEL_ID,
        system=[{"text": system}],
        messages=[{"role": "user", "content": [{"text": user}]}],
        inferenceConfig={"maxTokens": max_tokens},
    )
    if tool:  # force the model to answer through a JSON-schema tool => reliable structured output
        kwargs["toolConfig"] = {"tools": [tool], "toolChoice": {"tool": {"name": tool["toolSpec"]["name"]}}}
    return brt.converse(**kwargs)


QUIZ_TOOL = {
    "toolSpec": {
        "name": "submit_quiz",
        "description": "Submit the finished multiple-choice questions.",
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {"type": "string"},
                                "choices": {"type": "array", "items": {"type": "string"}, "minItems": 4, "maxItems": 4},
                                "answer_index": {"type": "integer", "minimum": 0, "maximum": 3},
                            },
                            "required": ["question", "choices", "answer_index"],
                        },
                    }
                },
                "required": ["questions"],
            }
        },
    }
}

GENERIC_QUERIES = [
    "key concepts and definitions",
    "important processes and steps",
    "examples and use cases",
    "comparisons and differences between things",
    "advantages, limitations and trade-offs",
    "rules, formulas and principles",
    "common mistakes and misconceptions",
    "summary of the main ideas",
]


# ---------------------------------------------------------------- API
@app.get("/api/health")
def health():
    return {"ok": True, "region": REGION, "model": MODEL_ID}


@app.get("/api/chats")
def get_chats():
    return {"chats": list_chats(), "max_chats": MAX_CHATS}


@app.post("/api/chats")
def create_chat():
    chats = list_chats()
    if len(chats) >= MAX_CHATS:
        raise HTTPException(409, f"Maximum {MAX_CHATS} chats. Delete one to make room.")
    taken = {c["name"] for c in chats}
    name = next(f"Chat {n}" for n in range(1, MAX_CHATS + 2) if f"Chat {n}" not in taken)
    chat = {"id": uuid.uuid4().hex[:12], "name": name, "created_at": now_iso(), "last_upload_at": None}
    save_chat(chat)
    return chat


@app.get("/api/chats/{chat_id}")
def get_chat(chat_id: str):
    chat = load_chat(chat_id)
    return {"chat": chat, "files": list_files(chat_id), "status": compute_state(chat)}


@app.delete("/api/chats/{chat_id}")
def delete_chat(chat_id: str):
    load_chat(chat_id)
    keys = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=chat_prefix(chat_id)):
        keys += [{"Key": o["Key"]} for o in page.get("Contents", [])]
    for i in range(0, len(keys), 1000):
        s3.delete_objects(Bucket=BUCKET, Delete={"Objects": keys[i:i + 1000]})
    s3.delete_object(Bucket=BUCKET, Key=meta_key(chat_id))
    start_sync()  # lets the KB drop the vectors of the deleted documents
    return {"deleted": chat_id}


@app.post("/api/chats/{chat_id}/files")
async def upload_files(chat_id: str, files: list[UploadFile] = File(...)):
    chat = load_chat(chat_id)
    saved = []
    for f in files:
        name = re.sub(r"[^A-Za-z0-9._-]", "", os.path.basename(f.filename or "").replace(" ", "_"))[:120]
        if os.path.splitext(name)[1].lower() not in ALLOWED_EXT:
            raise HTTPException(400, f"'{f.filename}' is not supported. Allowed: {', '.join(sorted(ALLOWED_EXT))} (export .pptx to PDF).")
        data = await f.read()
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, f"'{f.filename}' is larger than {MAX_FILE_BYTES // 1024 // 1024} MB.")
        key = chat_prefix(chat_id) + name
        # Sidecar FIRST, so an ingestion job can never see the document without its chat_id.
        sidecar = {"metadataAttributes": {"chat_id": {"value": {"type": "STRING", "stringValue": chat_id}, "includeForEmbedding": False}}}
        s3.put_object(Bucket=BUCKET, Key=key + ".metadata.json", Body=json.dumps(sidecar).encode(), ContentType="application/json")
        s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType=f.content_type or "application/octet-stream")
        saved.append(name)
    if saved:
        if re.match(r"^Chat \d+$", chat["name"]):
            chat["name"] = os.path.splitext(saved[0])[0].replace("_", " ")[:40]
        chat["last_upload_at"] = now_iso()
        save_chat(chat)
        start_sync()
    return {"saved": saved, "status": compute_state(chat)}


@app.post("/api/chats/{chat_id}/sync")
def sync_chat(chat_id: str):
    """Idempotent: make sure an ingestion job exists that covers the latest upload (also used to retry failures)."""
    chat = load_chat(chat_id)
    state = compute_state(chat)
    if state["state"] in ("pending", "failed"):
        if state["state"] == "failed":
            chat["last_upload_at"] = now_iso()
            save_chat(chat)
        start_sync()
        state = compute_state(chat)
    return state


class QuizReq(BaseModel):
    count: int = Field(5, ge=1, le=10)
    topic: str = Field("", max_length=200)
    difficulty: str = Field("medium", pattern="^(easy|medium|hard)$")
    seen: list[str] = Field(default_factory=list, max_length=60)


@app.post("/api/chats/{chat_id}/quiz")
def make_quiz(chat_id: str, req: QuizReq):
    load_chat(chat_id)
    chunks = retrieve(chat_id, req.topic.strip() or random.choice(GENERIC_QUERIES))
    if not chunks:
        raise HTTPException(409, "Nothing indexed for this chat yet. Wait until the status says Ready.")
    random.shuffle(chunks)
    excerpts = "\n\n---\n\n".join(f"[{c['source']}]\n{c['text']}" for c in chunks)
    already = "\n".join(f"- {q}" for q in req.seen) or "(none)"

    system = (
        "You are Quizzy, a quiz writer for students. Write multiple-choice questions strictly from the lecture excerpts. "
        "Rules: exactly 4 choices and exactly 1 correct; plausible distractors; no 'all/none of the above'; "
        "each question must be understandable on its own (never say 'according to the text/excerpt'); "
        f"difficulty: {req.difficulty}. Never repeat or closely rephrase a question from ALREADY ASKED."
    )
    user = f"LECTURE EXCERPTS:\n{excerpts}\n\nALREADY ASKED:\n{already}\n\nWrite {req.count} questions."
    resp = converse(system, user, max_tokens=3000, tool=QUIZ_TOOL)

    raw = []
    for block in resp["output"]["message"]["content"]:
        if "toolUse" in block:
            raw = block["toolUse"]["input"].get("questions", [])
    questions = []
    for q in raw:
        choices = q.get("choices", [])
        idx = q.get("answer_index")
        if len(choices) != 4 or not isinstance(idx, int) or not 0 <= idx < 4:
            continue
        correct = choices[idx]
        random.shuffle(choices)  # models like to put the answer in the same slot, so shuffle server-side
        questions.append({"id": uuid.uuid4().hex[:8], "question": q["question"], "choices": choices, "answer_index": choices.index(correct)})
    if not questions:
        raise HTTPException(502, "The model returned no usable questions. Try again.")
    return {"questions": questions}


class ExplainReq(BaseModel):
    question: str = Field(max_length=1000)
    choices: list[str] = Field(min_length=4, max_length=4)
    answer_index: int = Field(ge=0, le=3)
    picked_index: int | None = Field(None, ge=0, le=3)  # None = ran out of time


@app.post("/api/chats/{chat_id}/explain")
def explain(chat_id: str, req: ExplainReq):
    load_chat(chat_id)
    correct = req.choices[req.answer_index]
    chunks = retrieve(chat_id, f"{req.question} {correct}", k=4)
    excerpts = "\n\n---\n\n".join(c["text"] for c in chunks) or "(no excerpts found)"
    if req.picked_index is None:
        student = "The student ran out of time without answering."
    elif req.picked_index == req.answer_index:
        student = "The student answered correctly."
    else:
        student = f"The student chose: {req.choices[req.picked_index]!r} (wrong)."

    system = (
        "You are a friendly tutor. Using the lecture excerpts, explain in at most 110 words why the correct answer is right. "
        "If the student was wrong, say briefly why their choice is wrong. Plain text, no markdown headings, no bullet lists. "
        "If the excerpts do not cover it, say so and explain from general knowledge, marked clearly."
    )
    user = (
        f"QUESTION: {req.question}\nCHOICES: {json.dumps(req.choices)}\nCORRECT ANSWER: {correct}\n"
        f"{student}\n\nLECTURE EXCERPTS:\n{excerpts}"
    )
    resp = converse(system, user, max_tokens=500)
    text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"]).strip()
    return {"explanation": text, "sources": sorted({c["source"] for c in chunks if c["source"]})}


# ---------------------------------------------------------------- serve the built React app (production / EC2)
DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if DIST.exists():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="web")
