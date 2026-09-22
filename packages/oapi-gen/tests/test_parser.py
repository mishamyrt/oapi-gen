from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from oapi_gen import GenerationError, generate_package
from oapi_gen.parser import OpenAPIParser, load_document, parse_openapi


@pytest.fixture
def document() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Parser boundaries", "version": "1"},
        "paths": {},
    }


def _operation(name: str, **fields: Any) -> dict[str, Any]:
    return {
        "operationId": name,
        "responses": {"204": {"description": "Empty"}},
        **fields,
    }


def test_operation_namespaces_avoid_contract_and_import_collisions(
    document: dict[str, Any],
) -> None:
    names = [
        "handlers",
        "handlersOperation",
        "defaultHandler",
        "protocol",
        "annotated",
        "keySecurity",
        "exception",
        "never",
        "cookie",
    ]
    document["components"] = {
        "securitySchemes": {"key": {"type": "apiKey", "in": "header", "name": "X-Key"}}
    }
    document["paths"] = {
        f"/{name}": {
            "get": _operation(
                name,
                parameters=[
                    {"in": "query", "name": "count", "schema": {"type": "integer", "minimum": 1}}
                ],
            )
        }
        for name in names
    }
    spec = OpenAPIParser(document, "hash").parse()
    assert {operation.operation_id: operation.class_name for operation in spec.operations} == {
        "handlers": "HandlersOperation_2",
        "handlersOperation": "HandlersOperation",
        "defaultHandler": "DefaultHandlerOperation",
        "protocol": "ProtocolOperation",
        "annotated": "AnnotatedOperation",
        "keySecurity": "KeySecurityOperation",
        "exception": "ExceptionOperation",
        "never": "NeverOperation",
        "cookie": "CookieOperation",
    }


def test_response_names_cover_standard_and_custom_statuses(document: dict[str, Any]) -> None:
    names = {
        200: "Ok",
        201: "Created",
        202: "Accepted",
        204: "NoContent",
        301: "MovedPermanently",
        400: "BadRequest",
        401: "Unauthorized",
        404: "NotFound",
        413: "PayloadTooLarge",
        414: "UriTooLong",
        416: "RangeNotSatisfiable",
        422: "UnprocessableEntity",
        429: "TooManyRequests",
        499: "Status499",
        500: "InternalServerError",
    }
    document["paths"] = {
        "/login": {
            "post": _operation(
                "login", responses={str(status): {"description": "Response"} for status in names}
            )
        }
    }
    operation = OpenAPIParser(document, "hash").parse().operations[0]
    assert {response.status_code: response.class_name for response in operation.responses} == names


@pytest.mark.parametrize(
    "media_type,schema",
    [
        ("application/pdf", {"type": "string"}),
        ("application/pdf", {"type": "string", "format": "byte"}),
        ("application/pdf", {"type": "string", "format": "binary", "nullable": True}),
        ("application/pdf", {"type": "object"}),
        ("multipart/mixed", {"type": "string", "format": "binary"}),
        ("application/*", {}),
        ("invalid", {}),
        ("application/pdf\r\nX-Injected: value", {}),
    ],
)
def test_binary_responses_reject_unsupported_schemas_and_media_types(
    document: dict[str, Any], media_type: str, schema: dict[str, Any]
) -> None:
    document["paths"] = {
        "/file": {
            "get": _operation(
                "download",
                responses={
                    "200": {"description": "File", "content": {media_type: {"schema": schema}}}
                },
            )
        }
    }
    with pytest.raises(GenerationError, match=r"GET /file.*response"):
        OpenAPIParser(document, "hash").parse()


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
def test_document_loading_preserves_source_hash(
    tmp_path: Path, document: dict[str, Any], suffix: str
) -> None:
    source = json.dumps(document) if suffix == ".json" else yaml.safe_dump(document)
    path = tmp_path / f"spec{suffix}"
    path.write_text(source, encoding="utf-8")

    loaded, source_hash = load_document(path)
    spec, parsed_document = parse_openapi(path)

    assert loaded == parsed_document == document
    assert source_hash == spec.source_hash == hashlib.sha256(path.read_bytes()).hexdigest()
    assert (spec.title, spec.api_version, spec.operations) == ("Parser boundaries", "1", ())


def test_operation_order_and_parameter_overrides(document: dict[str, Any]) -> None:
    document["paths"] = {
        "/z": {"get": _operation("last")},
        "/items/{id}": {
            "parameters": [
                {"name": "id", "in": "path", "schema": {"type": "integer"}},
                {"name": "X-Limit", "in": "header", "schema": {"type": "integer"}},
            ],
            "post": _operation("createItem"),
            "get": _operation(
                "getItem",
                parameters=[
                    {
                        "name": "x-limit",
                        "in": "header",
                        "schema": {"type": "integer", "default": 3},
                    }
                ],
            ),
        },
    }

    spec = OpenAPIParser(document, "hash").parse()

    assert [item.operation_id for item in spec.operations] == ["getItem", "createItem", "last"]
    parameters = spec.operations[0].parameters
    assert [(item.wire_name, item.python_name) for item in parameters] == [
        ("id", "id"),
        ("x-limit", "x_limit"),
    ]
    assert parameters[0].required
    assert parameters[1].has_default and parameters[1].default == 3
    assert spec.operations[1].parameters[1].wire_name == "X-Limit"
    assert spec.groups[0].operations == spec.operations


def test_security_inheritance_overrides_and_declaration_order(document: dict[str, Any]) -> None:
    document["components"] = {
        "securitySchemes": {
            name: {"type": "apiKey", "in": "header", "name": name} for name in ("z", "a")
        }
    }
    document["security"] = [{"z": ["read"], "a": []}, {}]
    document["paths"] = {
        "/inherited": {"get": _operation("inherited")},
        "/disabled": {"get": _operation("disabled", security=[])},
        "/overridden": {"get": _operation("overridden", security=[{"a": ["write"]}])},
    }

    spec = OpenAPIParser(document, "hash").parse()
    operations = {item.operation_id: item for item in spec.operations}

    assert [scheme.wire_name for scheme in spec.security_schemes] == ["a", "z"]
    inherited = operations["inherited"].security
    assert [(item.scheme_name, item.scopes) for item in inherited[0].schemes] == [
        ("z", ("read",)),
        ("a", ()),
    ]
    assert inherited[1].schemes == ()
    assert operations["disabled"].security == ()
    overridden = operations["overridden"].security
    assert len(overridden) == 1
    assert [(item.scheme_name, item.scopes) for item in overridden[0].schemes] == [
        ("a", ("write",))
    ]


def test_recursive_models_and_referenced_scalar_constraints(document: dict[str, Any]) -> None:
    document["components"] = {
        "schemas": {
            "Node~type": {
                "type": "object",
                "properties": {"child": {"$ref": "#/components/schemas/Node~0type"}},
            },
            "Count": {"type": "integer", "minimum": 2, "maximum": 9},
        }
    }
    document["paths"] = {
        "/items": {
            "get": _operation(
                "getItems",
                parameters=[
                    {
                        "name": "count",
                        "in": "query",
                        "required": True,
                        "schema": {"$ref": "#/components/schemas/Count", "minimum": 4},
                    }
                ],
                responses={
                    "200": {
                        "description": "Node",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Node~0type"}
                            }
                        },
                    }
                },
            )
        }
    }

    operation = OpenAPIParser(document, "hash").parse().operations[0]

    assert dict(operation.parameters[0].type_ref.constraints) == {"ge": 4, "le": 9}
    response_type = operation.responses[0].type_ref
    assert response_type is not None
    assert response_type.annotation == "_models_NodeType"
    assert response_type.model_names == frozenset({"NodeType"})


def test_component_errors_precede_operation_errors(document: dict[str, Any]) -> None:
    document["components"] = {
        "schemas": {
            "Unused": {
                "type": "object",
                "properties": {"child": {"$ref": "remote.yaml#/Child"}},
            }
        }
    }
    document["paths"] = {"/items": {"get": {}}}

    with pytest.raises(GenerationError) as caught:
        OpenAPIParser(document, "hash").parse()

    assert str(caught.value) == (
        "components.schemas.Unused.properties.child: external $ref values are not supported: "
        "'remote.yaml#/Child'"
    )


def test_cyclic_reference_aliases_keep_error_context(document: dict[str, Any]) -> None:
    document["components"] = {
        "requestBodies": {
            "A": {"$ref": "#/components/requestBodies/B"},
            "B": {"$ref": "#/components/requestBodies/A"},
        }
    }
    document["paths"] = {
        "/items": {"get": _operation("items", requestBody={"$ref": "#/components/requestBodies/A"})}
    }

    with pytest.raises(GenerationError) as caught:
        OpenAPIParser(document, "hash").parse()

    assert str(caught.value) == (
        "GET /items.requestBody: cyclic $ref alias '#/components/requestBodies/A'"
    )


def test_rejects_missing_operation_id(tmp_path: Path) -> None:
    specification = tmp_path / "invalid.yaml"
    specification.write_text(
        """
openapi: 3.1.0
info: {title: Invalid, version: 1.0.0}
paths:
  /items:
    get:
      responses:
        '204': {description: Empty}
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(GenerationError, match="operationId"):
        generate_package(specification, tmp_path / "generated")
