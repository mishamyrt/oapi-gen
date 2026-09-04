"""Shared contracts and payloads for checked and trusted response modes."""

ITEM = {
    "type": "object",
    "required": ["id", "name", "quantity", "active"],
    "properties": {
        "id": {"type": "integer", "minimum": 0},
        "name": {"type": "string", "minLength": 1, "maxLength": 100},
        "quantity": {"type": "integer", "minimum": 1},
        "active": {"type": "boolean"},
    },
}
ITEM_REF = {"$ref": "#/components/schemas/Item"}
ENVELOPE_REF = {"$ref": "#/components/schemas/Envelope"}
SMALL = {"id": 1, "name": "test item", "quantity": 2, "active": True}
NESTED = {
    "items": [{**SMALL, "id": i, "name": f"item {i}"} for i in range(100)],
    "metadata": {"source": "benchmark", "version": "1"},
}


def operation(name, schema=None, *, body=None, parameters=None):
    response = {"description": "Result"}
    if schema is not None:
        response["content"] = {"application/json": {"schema": schema}}
    op = {
        "operationId": name,
        "tags": ["Bench"],
        "responses": {"204" if schema is None else "200": response},
    }
    if body is not None:
        op["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    if parameters:
        op["parameters"] = parameters
    return op


SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "Generated backend benchmark", "version": "1"},
    "paths": {
        "/empty": {"get": operation("empty")},
        "/parameters/{id}": {
            "get": operation(
                "parameters",
                parameters=[
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer", "minimum": 1},
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                    },
                    {"name": "active", "in": "query", "schema": {"type": "boolean"}},
                    {
                        "name": "tag",
                        "in": "query",
                        "schema": {"type": "array", "items": {"type": "string"}},
                    },
                    {
                        "name": "X-Token",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "session",
                        "in": "cookie",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                ],
            )
        },
        "/echo": {"post": operation("echo", ITEM_REF, body=ITEM_REF)},
        "/nested": {"post": operation("nested", ENVELOPE_REF, body=ENVELOPE_REF)},
        "/items": {"get": operation("listItems", {"type": "array", "items": ITEM_REF})},
    },
    "components": {
        "schemas": {
            "Item": ITEM,
            "Envelope": {
                "type": "object",
                "required": ["items", "metadata"],
                "properties": {
                    "items": {"type": "array", "minItems": 1, "maxItems": 1000, "items": ITEM_REF},
                    "metadata": {"type": "object", "additionalProperties": {"type": "string"}},
                },
            },
        }
    },
}

CASES = [
    ("empty", "GET", "/empty", None, 204),
    ("parameters", "GET", "/parameters/12?limit=20&active=true&tag=a&tag=b", None, 204),
    ("small_json", "POST", "/echo", SMALL, 200),
    ("nested_100", "POST", "/nested", NESTED, 200),
    ("create_100", "GET", "/items", None, 200),
    ("invalid_json", "POST", "/echo", {**SMALL, "quantity": 0}, 422),
]
