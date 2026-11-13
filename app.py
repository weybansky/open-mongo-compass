"""Open Mongo Compass: a small read-only MongoDB browser. Run with: python app.py"""

import csv
import io
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Dict, Iterator, Literal
from urllib.parse import quote

import uvicorn
from bson import json_util
from bson.errors import BSONError
from bson.json_util import RELAXED_JSON_OPTIONS
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from pymongo import MongoClient
from pymongo.cursor import Cursor
from pymongo.errors import ConfigurationError, OperationFailure, PyMongoError
from starlette.middleware.trustedhost import TrustedHostMiddleware

HOST = "127.0.0.1"  # Never expose this: there is no login, and it holds live DB connections.
PORT = int(os.environ.get("PORT", 8765))
MAX_SAFE_INT = 2**53 - 1  # Larger integers lose precision when the browser parses them.
QUERY_TIMEOUT_MS = 30_000

app = FastAPI(title="Open Mongo Compass", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])

clients: Dict[str, MongoClient] = {}
exports: Dict[str, "ExportBody"] = {}  # One-time download links, used once by the browser.


class ConnectBody(BaseModel):
    uri: str


class ConnBody(BaseModel):
    conn: str


class CollectionsBody(ConnBody):
    db: str


class CountBody(CollectionsBody):
    coll: str
    filter: str = "{}"


class QueryBody(CountBody):
    projection: str = "{}"
    sort: str = "{}"


class FindBody(QueryBody):
    skip: int = Field(0, ge=0)
    limit: int = Field(50, ge=1, le=1000)


class ExportBody(QueryBody):
    format: Literal["json", "csv"] = "json"


def get_client(conn: str) -> MongoClient:
    client = clients.get(conn)
    if client is None:
        raise HTTPException(404, "Not connected. Please connect again.")
    return client


def parse_doc(text: str, name: str) -> dict:
    doc = json_util.loads(text or "{}")
    if not isinstance(doc, dict):
        raise ValueError(f"{name} must be an object")
    return doc


def keep_big_ints(value):
    """Send integers JavaScript can't represent as strings, so the UI never shows a wrong value."""
    if isinstance(value, dict):
        return {k: keep_big_ints(v) for k, v in value.items()}
    if isinstance(value, list):
        return [keep_big_ints(v) for v in value]
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > MAX_SAFE_INT:
        return {"$numberLong": str(value)}
    return value


def list_databases(client: MongoClient) -> list:
    try:
        return sorted(client.list_database_names())
    except OperationFailure as listing_error:
        # Restricted users often can't list databases; use the one named in the URI, if any.
        try:
            return [client.get_default_database().name]
        except ConfigurationError:
            raise listing_error


@app.exception_handler(PyMongoError)
@app.exception_handler(BSONError)
@app.exception_handler(ValueError)
@app.exception_handler(TypeError)
def handle_query_error(_: Request, error: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=400)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).with_name("index.html"))


@app.post("/api/connect")
def connect(body: ConnectBody) -> dict:
    client = MongoClient(
        body.uri.strip(),
        serverSelectionTimeoutMS=15_000,
        datetime_conversion="DATETIME_AUTO",  # Don't fail on dates Python can't represent.
    )
    try:
        client.admin.command("ping")
        databases = list_databases(client)
    except Exception:
        client.close()
        raise
    conn = uuid.uuid4().hex
    clients[conn] = client
    return {"conn": conn, "databases": databases}


@app.post("/api/disconnect")
def disconnect(body: ConnBody) -> dict:
    client = clients.pop(body.conn, None)
    if client is not None:
        client.close()
    return {"ok": True}


@app.post("/api/collections")
def collections(body: CollectionsBody) -> dict:
    return {"collections": sorted(get_client(body.conn)[body.db].list_collection_names())}


def build_cursor(body: QueryBody) -> Cursor:
    cursor = get_client(body.conn)[body.db][body.coll].find(
        parse_doc(body.filter, "Filter"), parse_doc(body.projection, "Projection") or None
    )
    sort = parse_doc(body.sort, "Sort")
    return cursor.sort(list(sort.items())) if sort else cursor


@app.post("/api/find")
def find(body: FindBody) -> Response:
    cursor = (
        build_cursor(body)
        .skip(body.skip)
        .limit(body.limit + 1)  # One extra tells us whether there is a next page.
        .max_time_ms(QUERY_TIMEOUT_MS)
    )
    docs = list(cursor)
    payload = {"docs": keep_big_ints(docs[: body.limit]), "has_more": len(docs) > body.limit}
    return Response(json_util.dumps(payload, json_options=RELAXED_JSON_OPTIONS), media_type="application/json")


@app.post("/api/count")
def count(body: CountBody) -> dict:
    collection = get_client(body.conn)[body.db][body.coll]
    query = parse_doc(body.filter, "Filter")
    if not query:
        return {"count": collection.estimated_document_count(maxTimeMS=QUERY_TIMEOUT_MS), "estimated": True}
    return {"count": collection.count_documents(query, maxTimeMS=QUERY_TIMEOUT_MS), "estimated": False}


def in_chunks(pieces: Iterator[str], size: int = 256 * 1024) -> Iterator[str]:
    """Group small strings so the response isn't sent one document at a time."""
    buffer, length = [], 0
    for piece in pieces:
        buffer.append(piece)
        length += len(piece)
        if length >= size:
            yield "".join(buffer)
            buffer, length = [], 0
    if buffer:
        yield "".join(buffer)


def json_lines(cursor: Cursor) -> Iterator[str]:
    yield "["
    for i, doc in enumerate(cursor):
        yield ("," if i else "") + "\n" + json_util.dumps(keep_big_ints(doc), json_options=RELAXED_JSON_OPTIONS)
    yield "\n]\n"


def flatten(value, key: str, row: dict) -> None:
    """Turn nested fields into dotted columns, like address.city."""
    if isinstance(value, dict) and value and not any(k.startswith("$") for k in value):
        for k, v in value.items():
            flatten(v, f"{key}.{k}", row)
    elif isinstance(value, dict) and len(value) == 1 and not isinstance(next(iter(value.values())), (dict, list)):
        row[key] = next(iter(value.values()))  # {"$oid": "..."}, {"$date": "..."} and similar
    elif isinstance(value, (dict, list)):
        row[key] = json.dumps(value, ensure_ascii=False)
    elif isinstance(value, bool):
        row[key] = "true" if value else "false"
    else:
        row[key] = "null" if value is None else value


def csv_lines(cursor: Cursor) -> Iterator[str]:
    # The header needs every column, so rows go to a temporary file until the query finishes.
    with tempfile.TemporaryFile("w+", encoding="utf-8") as rows:
        columns: Dict[str, None] = {}
        for doc in cursor:
            row: dict = {}
            for key, value in json.loads(json_util.dumps(doc, json_options=RELAXED_JSON_OPTIONS)).items():
                flatten(value, key, row)
            columns.update(dict.fromkeys(row))
            rows.write(json.dumps(row) + "\n")
        rows.seek(0)
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(columns))
        writer.writeheader()
        yield "﻿" + out.getvalue()  # The byte order mark makes Excel read the file as UTF-8.
        for line in rows:
            out.seek(0)
            out.truncate()
            writer.writerow(json.loads(line))
            yield out.getvalue()


@app.post("/api/export")
def start_export(body: ExportBody) -> dict:
    build_cursor(body)  # Report bad filters here, before the browser starts a download.
    token = uuid.uuid4().hex
    exports[token] = body
    return {"token": token}


@app.get("/api/export/{token}")
def download_export(token: str) -> StreamingResponse:
    body = exports.pop(token, None)
    if body is None:
        raise HTTPException(404, "This export has expired. Start it again.")
    cursor = build_cursor(body)
    lines = csv_lines(cursor) if body.format == "csv" else json_lines(cursor)
    filename = quote(f"{body.db}.{body.coll}.{body.format}", safe="")
    return StreamingResponse(
        in_chunks(lines),
        media_type="text/csv; charset=utf-8" if body.format == "csv" else "application/json",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


if __name__ == "__main__":
    print(f"Open Mongo Compass: http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
