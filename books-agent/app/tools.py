"""The agent's tools. Each one sends ONE fixed GraphQL document to the backend with the user's token.

The model never gets the schema or a "call any query" tool — only these typed functions.
Reads run immediately. Writes (set_book, delete_book) are listed in AgentConfig.tool_approval,
so Continuum asks our approval handler first; the handler defers them until the user confirms.
"""

from __future__ import annotations

from typing import Any

from continuum.tools.mcp import FunctionTool

from app.backend_client import BackendError, gql
from app.context import turn

BOOK_FIELDS = "id title author year tags updatedAt updatedBy"


def _remember(books: list[dict[str, Any]]) -> None:
    """Record books the user has seen this turn, so the checkpoint can resolve "that book" later."""
    seen = turn().entities_seen
    for b in books:
        if b and b.get("id"):
            seen[b["id"]] = b.get("title", "")


def _failed(err: BackendError) -> dict[str, Any]:
    return {"ok": False, "error": str(err), "code": err.code}


# ---- reads ----------------------------------------------------------------------------------


async def get_book(args: dict[str, Any]) -> dict[str, Any]:
    try:
        data = await gql(
            f"query GetBook($id: ID!) {{ getBook(id: $id) {{ {BOOK_FIELDS} }} }}", {"id": args["id"]}, turn().token
        )
    except BackendError as err:
        return _failed(err)
    book = data["getBook"]
    if book:
        _remember([book])
    return {"ok": True, "book": book}


async def list_books(args: dict[str, Any]) -> dict[str, Any]:
    try:
        data = await gql(
            "query ListBooks($search: String, $limit: Int) "
            f"{{ listBooks(search: $search, limit: $limit) {{ {BOOK_FIELDS} }} }}",
            {"search": args.get("search"), "limit": args.get("limit", 10)},
            turn().token,
        )
    except BackendError as err:
        return _failed(err)
    found = data["listBooks"]
    _remember(found)
    return {"ok": True, "count": len(found), "books": found}


# ---- writes (deferred to the user's confirm) -------------------------------------------------


async def set_book(args: dict[str, Any]) -> dict[str, Any]:
    book_input = {k: args[k] for k in ("id", "title", "author", "year", "tags") if args.get(k) is not None}
    try:
        data = await gql(
            f"mutation SetBook($input: BookInput!) {{ setBook(input: $input) {{ {BOOK_FIELDS} }} }}",
            {"input": book_input},
            turn().token,
        )
    except BackendError as err:
        return _failed(err)
    _remember([data["setBook"]])
    return {"ok": True, "book": data["setBook"]}


async def delete_book(args: dict[str, Any]) -> dict[str, Any]:
    try:
        data = await gql(
            "mutation DeleteBook($id: ID!) { deleteBook(id: $id) { id deleted } }", {"id": args["id"]}, turn().token
        )
    except BackendError as err:
        return _failed(err)
    return {"ok": True, **data["deleteBook"]}


# Plain functions by name — the confirm endpoint calls these directly (no LLM involved).
IMPLEMENTATIONS = {"get_book": get_book, "list_books": list_books, "set_book": set_book, "delete_book": delete_book}
WRITE_TOOLS = {"set_book", "delete_book"}

TOOLS: list[FunctionTool] = [
    FunctionTool(
        name="get_book",
        description="Get one book by its id (a slug such as 'dune').",
        input_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        fn=get_book,
    ),
    FunctionTool(
        name="list_books",
        description="Search books by title, author or tag. Use this to find a book's id.",
        input_schema={
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Text to match; omit to list all"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
        fn=list_books,
    ),
    FunctionTool(
        name="set_book",
        description=(
            "Create or update a book. To update, pass the existing id and the full new values. "
            "Requires the user's confirmation before it runs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Existing id to update; omit to create"},
                "title": {"type": "string"},
                "author": {"type": "string"},
                "year": {"type": "integer"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "author"],
        },
        fn=set_book,
    ),
    FunctionTool(
        name="delete_book",
        description="Delete a book by id. Requires the user's confirmation before it runs.",
        input_schema={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        fn=delete_book,
    ),
]
