"""Generate explicit Starlette endpoints with msgspec codecs."""

from __future__ import annotations

import re

from ..ir import ApiSpec, Operation
from .contracts import render_contract_imports
from .inspection import (
    all_type_refs,
    has_cookie_arrays,
    imports_for_types,
    models_for_types,
    operation_uses_security,
    used_security_schemes,
)
from .security import render_authorization
from .writer import Writer, generated_header, render_imports, render_model_imports


def render_router(spec: ApiSpec, *, validate_responses: bool = True) -> str:
    w = Writer()
    w.line(generated_header(spec.source_hash))
    w.line("from __future__ import annotations")
    w.require("msgspec", "json")
    w.require("pathlib", "Path")
    w.require("starlette.requests", "Request")
    w.require("starlette.responses", "Response")
    w.require("starlette.routing", "Route")
    w.require("starlette.routing", "Router")
    for name in (
        "RequestError",
        "boolean",
        "check_json_content_type",
        "check_property_counts",
        "comma_values",
        "encode_header",
        "error_response",
        "json_body",
        "multipart_body",
        "parameter",
        "upload",
    ):
        w.require("._runtime", f"{name} as _runtime_{name}")
    refs = list(all_type_refs(spec))
    render_imports(w, imports_for_types(refs))
    render_model_imports(w, models_for_types(refs))
    render_contract_imports(w, spec)
    w.line("_encoder = json.Encoder()")
    if has_cookie_arrays(spec) and validate_responses:
        w.line("_cookie_decoder = json.Decoder(list[_contracts_Cookie] | None)")
    for op in spec.operations:
        for index, param in enumerate(op.parameters):
            annotation = param.type_ref.annotated
            w.line(f"_{op.python_name}_parameter_{index} = {annotation}")
            # Fail at import (and generation) for unsupported types, not on the first request.
            w.line(f"json.Decoder(_{op.python_name}_parameter_{index})")
        if op.request_body:
            body = op.request_body
            if body.property_counts is not None:
                w.line(f"_{op.python_name}_body_properties = {body.property_counts!r}")
            if body.is_multipart:
                for index, field in enumerate(body.multipart_fields):
                    if not field.is_file:
                        annotation = field.type_ref.annotated
                        w.line(f"_{op.python_name}_form_{index} = {annotation}")
                        w.line(f"json.Decoder(_{op.python_name}_form_{index})")
            else:
                annotation = body.type_ref.annotated
                w.line(f"_{op.python_name}_body_decoder = json.Decoder({annotation})")
        for response in op.responses:
            if response.event_fields:
                w.require("._streams", "EventField as _streams_EventField")
                w.line(f"_{op.python_name}_{response.status_code}_event_fields = (")
                for field in response.event_fields:
                    w.line(
                        f"_streams_EventField({field.python_name!r}, {field.required!r}, "
                        f"json.Decoder({field.type_ref.annotated}), "
                        f"json.Decoder({field.wire_type_ref.annotated}), "
                        f"{field.json_encoded!r}, {field.property_counts!r}),",
                        1,
                    )
                w.line(")")
            if response.property_counts is not None:
                w.line(
                    f"_{op.python_name}_{response.status_code}_properties = "
                    f"{response.property_counts!r}"
                )
            if response.type_ref:
                annotation = response.type_ref.annotated
                w.line(
                    f"_{op.python_name}_{response.status_code}_decoder = json.Decoder({annotation})"
                )
            for index, header in enumerate(response.headers):
                annotation = header.type_ref.annotated
                w.line(
                    f"_{op.python_name}_{response.status_code}_header_{index} = "
                    f"json.Decoder({annotation})"
                )
    schemes = used_security_schemes(spec)
    w.line()
    signature = "def create_router(handlers: Handlers, *, "
    if schemes:
        signature += "security: SecurityHandler, "
    w.line(signature + 'prefix: str = "", include_schema: bool = True) -> Router:')
    w.line("__oapi_handlers = handlers", 1)
    if schemes:
        w.line("__oapi_security = security", 1)
    by_name = {scheme.wire_name: scheme for scheme in schemes}
    for op in spec.operations:
        w.line()
        w.line(f"async def __oapi_endpoint_{op.python_name}(__oapi_http: Request) -> Response:", 1)
        if op.security:
            render_authorization(w, op, by_name)
        if op.request_body and op.request_body.is_multipart:
            streaming = any(response.streaming for response in op.responses)
            if streaming:
                w.require("contextlib", "AsyncExitStack")
                w.line("async with AsyncExitStack() as __oapi_resources:", 2)
                w.line("try:", 3)
                w.line(
                    "__oapi_form = await __oapi_resources.enter_async_context("
                    "_runtime_multipart_body(__oapi_http, "
                    f"required={op.request_body.required!r}))",
                    4,
                )
            else:
                w.line("try:", 2)
                w.line(
                    "async with _runtime_multipart_body(__oapi_http, "
                    f"required={op.request_body.required!r}) as __oapi_form:",
                    3,
                )
            with w.indented(), w.indented():
                render_endpoint(w, op, validate_responses)
            w.line("except _runtime_RequestError as __oapi_error:", 3 if streaming else 2)
            w.line("return _runtime_error_response(__oapi_error)", 4 if streaming else 3)
        else:
            render_endpoint(w, op, validate_responses)
    w.line("__oapi_routes = [", 1)
    for op in spec.operations:
        path_names = {
            param.wire_name: param.python_name
            for param in op.parameters
            if param.location == "path"
        }
        route_path = re.sub(
            r"{([^{}]+)}", lambda match, names=path_names: "{" + names[match[1]] + "}", op.path
        )
        w.line(
            f"Route(prefix + {route_path!r}, __oapi_endpoint_{op.python_name}, "
            f"methods=[{op.method.upper()!r}], name={op.operation_id!r}),",
            2,
        )
    w.line("]", 1)
    # Starlette implicitly adds HEAD to GET, which would shadow a declared HEAD operation.
    for index, op in enumerate(spec.operations):
        if op.method.upper() == "GET":
            w.line(f"__oapi_routes[{index}].methods = {{'GET'}}", 1)
    w.line("if include_schema:", 1)
    w.line("__oapi_schema = json.decode(Path(__file__).with_name('openapi.json').read_bytes())", 2)
    w.line(
        "__oapi_schema['paths'] = {prefix + path: item "
        "for path, item in __oapi_schema['paths'].items()}",
        2,
    )
    w.line("__oapi_schema_bytes = _encoder.encode(__oapi_schema)", 2)
    w.line("async def __oapi_openapi(request: Request) -> Response:", 2)
    w.line("return Response(__oapi_schema_bytes, media_type='application/json')", 3)
    w.line("__oapi_routes.append(Route(prefix + '/openapi.json', __oapi_openapi))", 2)
    w.line("return Router(routes=__oapi_routes)", 1)
    return w.render()


def render_endpoint(w: Writer, op: Operation, checked: bool) -> None:
    fields: list[tuple[str, str]] = []
    has_input = bool(op.parameters or op.request_body)
    if has_input:
        w.line("try:", 2)
    for index, param in enumerate(op.parameters):
        source = {
            "query": "query_params",
            "path": "path_params",
            "header": "headers",
            "cookie": "cookies",
        }[param.location]
        raw_type = param.wire_schema.get("type")
        array = raw_type == "array" or (isinstance(raw_type, list) and "array" in raw_type)
        getter = "getlist" if array and param.location in {"query", "header"} else "get"
        lookup_name = param.python_name if param.location == "path" else param.wire_name
        value = f"__oapi_http.{source}.{getter}({lookup_name!r})"
        if param.serialization == "comma-separated":
            value = f"_runtime_comma_values({value})"
        if raw_type == "boolean":
            value = f"_runtime_boolean({value})"
        elif (
            array
            and isinstance(items := param.wire_schema.get("items"), dict)
            and items.get("type") == "boolean"
        ):
            value = f"[_runtime_boolean(item) for item in ({value} or [])]"
        target = f"_{op.python_name}_parameter_{index}"
        variable = f"__oapi_parameter_{index}"
        w.line(
            f"{variable} = _runtime_parameter({value}, {target}, "
            f"{(param.location, param.wire_name)!r}, "
            f"required={param.required!r}, default={param.default!r})",
            3,
        )
        fields.append((param.python_name, variable))
    if op.request_body:
        body = op.request_body
        if body.is_multipart:
            render_form(w, op)
        else:
            w.line("__oapi_bytes = await __oapi_http.body()", 3)
            w.line("if __oapi_bytes:", 3)
            w.line("_runtime_check_json_content_type(__oapi_http.headers.get('content-type'))", 4)
            w.line(
                f"__oapi_body = _runtime_json_body(__oapi_bytes, "
                f"_{op.python_name}_body_decoder, required={body.required!r}"
                + (
                    f", property_counts=_{op.python_name}_body_properties"
                    if body.property_counts is not None
                    else ""
                )
                + ")",
                3,
            )
        fields.append((body.python_name, "__oapi_body"))
    if has_input:
        w.line("except _runtime_RequestError as __oapi_error:", 2)
        w.line("return _runtime_error_response(__oapi_error)", 3)
    if operation_uses_security(op):
        fields.append(("security_context", "__oapi_security_context"))
    arguments = ", ".join(f"{name}={value}" for name, value in fields)
    w.line(f"__oapi_request = _contracts_{op.class_name}.Request({arguments})", 2)
    errors = ", ".join(
        f"_contracts_{op.class_name}.{response.class_name}"
        for response in op.responses
        if response.status_code >= 400
    )
    if errors:
        w.line("try:", 2)
    w.line(
        f"__oapi_result = await __oapi_handlers.{op.group_field_name}."
        f"{op.python_name}(__oapi_request)",
        3 if errors else 2,
    )
    if errors:
        w.line(f"except ({errors}) as __oapi_error:", 2)
        w.line("__oapi_result = __oapi_error", 3)
        w.line("else:", 2)
        w.line("if isinstance(__oapi_result, Exception):", 3)
        w.line(f"raise TypeError({op.operation_id + ' error responses must be raised'!r})", 4)
    for response in op.responses:
        w.line(
            f"if isinstance(__oapi_result, _contracts_{op.class_name}.{response.class_name}):", 2
        )
        headers = ""
        if response.headers:
            w.require("msgspec", "to_builtins")
            w.require("starlette.datastructures", "MutableHeaders")
            w.line("__oapi_headers = MutableHeaders()", 3)
            for index, header in enumerate(response.headers):
                value = f"__oapi_result.{header.python_name}"
                if header.is_cookie_array:
                    if checked:
                        value = f"_cookie_decoder.decode(_encoder.encode({value}))"
                    w.line(f"__oapi_cookies = {value}", 3)
                    w.line(
                        "__oapi_cookie_headers = "
                        "[__oapi_cookie.to_header() for __oapi_cookie in __oapi_cookies] "
                        "if __oapi_cookies is not None else None",
                        3,
                    )
                    value = "__oapi_cookie_headers"
                if checked:
                    value = (
                        f"_{op.python_name}_{response.status_code}_header_{index}"
                        f".decode(_encoder.encode({value}))"
                    )
                w.line(f"__oapi_header = {value}", 3)
                w.line("if __oapi_header is not None:", 3)
                indent = 4
                if header.wire_name.lower() == "set-cookie":
                    w.line("if isinstance(__oapi_header, list):", 4)
                    w.line("for __oapi_cookie in __oapi_header:", 5)
                    w.line(
                        f"__oapi_headers.append({header.wire_name!r}, "
                        "_runtime_encode_header(to_builtins(__oapi_cookie)))",
                        6,
                    )
                    w.line("else:", 4)
                    indent = 5
                w.line(
                    f"__oapi_headers[{header.wire_name!r}] = "
                    "_runtime_encode_header(to_builtins(__oapi_header))",
                    indent,
                )
            headers = ", headers=__oapi_headers"
        if response.streaming:
            w.require("functools", "partial")
            w.require("._streams", "StreamResponse as _streams_StreamResponse")
            if response.event_fields:
                w.require("._streams", "encode_sse_item as _streams_encode_sse_item")
                encode = (
                    "partial(_streams_encode_sse_item, "
                    f"event_type=_contracts_{op.class_name}.{response.event_class_name}, "
                    f"fields=_{op.python_name}_{response.status_code}_event_fields, "
                    f"checked={checked!r}"
                )
            else:
                w.require("._streams", "encode_json_item as _streams_encode_json_item")
                encode = (
                    "partial(_streams_encode_json_item, "
                    f"decoder=_{op.python_name}_{response.status_code}_decoder, "
                    f"media_type={response.media_type!r}, checked={checked!r}"
                )
            if response.property_counts is not None:
                encode += f", property_counts=_{op.python_name}_{response.status_code}_properties"
            resources = (
                ", resources=__oapi_resources"
                if op.request_body is not None and op.request_body.is_multipart
                else ""
            )
            w.line(
                "return _streams_StreamResponse(__oapi_result.body, "
                f"encode={encode}), status_code={response.status_code}, "
                f"media_type={response.media_type!r}{headers}{resources})",
                3,
            )
        elif response.binary:
            content = "__oapi_result.body"
            if checked:
                w.require("msgspec", "convert")
                content = (
                    f"convert({content}, "
                    f"type=_{op.python_name}_{response.status_code}_decoder.type, "
                    "builtin_types=(bytes,))"
                )
            w.line(
                f"return Response({content}, status_code={response.status_code}, "
                f"media_type={response.media_type!r}{headers})",
                3,
            )
        elif response.type_ref:
            if checked:
                w.require("msgspec", "convert")
                w.require("msgspec", "to_builtins")
                w.line(
                    "__oapi_body = convert(to_builtins(__oapi_result.body), "
                    f"type=_{op.python_name}_{response.status_code}_decoder.type)",
                    3,
                )
                w.line("__oapi_content = _encoder.encode(__oapi_body)", 3)
                if response.property_counts is not None:
                    w.line(
                        "_runtime_check_property_counts(json.decode(__oapi_content), "
                        f"**_{op.python_name}_{response.status_code}_properties)",
                        3,
                    )
            else:
                w.line("__oapi_content = _encoder.encode(__oapi_result.body)", 3)
            w.line(
                f"return Response(__oapi_content, status_code={response.status_code}, "
                f"media_type={response.media_type!r}{headers})",
                3,
            )
        else:
            w.line(f"return Response(status_code={response.status_code}{headers})", 3)
    w.line(f"raise TypeError({op.operation_id + ' returned an unsupported response variant'!r})", 2)


def render_form(w: Writer, op: Operation) -> None:
    body = op.request_body
    assert body is not None
    if not body.required:
        w.line("if __oapi_form is None:", 3)
        w.line("__oapi_body = None", 4)
        w.line("else:", 3)
    indent = 3 if body.required else 4
    if body.property_counts is not None:
        w.line("try:", indent)
        w.line(
            "_runtime_check_property_counts(dict(__oapi_form), "
            f"**_{op.python_name}_body_properties)",
            indent + 1,
        )
        w.require("msgspec", "ValidationError")
        w.line("except ValidationError as __oapi_error:", indent)
        w.line(
            "raise _runtime_RequestError(str(__oapi_error), ('body',)) from __oapi_error",
            indent + 1,
        )
    for index, field in enumerate(body.multipart_fields):
        getter = "getlist" if field.is_array else "get"
        value = f"__oapi_form.{getter}({field.wire_name!r})"
        if field.is_file:
            constraints = "".join(
                f", {name}={value!r}" for name, value in field.type_ref.constraints
            )
            expr = (
                f"_runtime_upload({value}, {field.wire_name!r}, "
                f"required={field.required!r}, array={field.is_array!r}{constraints})"
            )
        else:
            if field.type_ref.annotation.removesuffix(" | None") == "bool":
                value = f"_runtime_boolean({value})"
            expr = (
                f"_runtime_parameter({value}, _{op.python_name}_form_{index}, "
                f"{('body', field.wire_name)!r}, required={field.required!r})"
            )
        w.line(f"__oapi_field_{index} = {expr}", indent)
    arguments = ", ".join(
        f"{field.python_name}=__oapi_field_{i}" for i, field in enumerate(body.multipart_fields)
    )
    w.line(f"__oapi_body = {body.type_ref.annotation}({arguments})", indent)
