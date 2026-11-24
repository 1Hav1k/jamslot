"""Проверка ответов, запросов и событий по контрактам из ЛР3 (api/*.yaml)."""
import json
import pathlib
import re

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

API_DIR = pathlib.Path(__file__).resolve().parents[2] / "api"


class ContractViolation(AssertionError):
    pass


class _Spec:
    def __init__(self, path: pathlib.Path):
        self.name = path.stem
        self.doc = yaml.safe_load(path.read_text())
        self.uri = f"urn:{self.name}"
        self.registry = Registry().with_resource(self.uri, Resource(contents=self.doc, specification=DRAFT202012))
        self.ops = []
        for tpl, item in self.doc.get("paths", {}).items():
            rx = re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(tpl)) + "$")
            for method, op in item.items():
                if method != "parameters":
                    self.ops.append((rx, method.upper(), tpl, op))

    def resolve(self, node):
        while isinstance(node, dict) and "$ref" in node:
            cur = self.doc
            for part in node["$ref"][2:].split("/"):
                cur = cur[part]
            node = cur
        return node

    def validate(self, schema: dict, instance, where: str):
        v = Draft202012Validator(json.loads(json.dumps(schema).replace('"#/', f'"{self.uri}#/')),
                                 registry=self.registry, format_checker=FormatChecker())
        errors = sorted(v.iter_errors(instance), key=lambda e: list(e.absolute_path))
        if errors:
            e = errors[0]
            raise ContractViolation(f"{where}: {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}")


class Contracts:
    def __init__(self):
        self.specs = [_Spec(p) for p in sorted(API_DIR.glob("*-service.yaml"))]
        self.asyncapi = yaml.safe_load((API_DIR / "asyncapi.yaml").read_text())
        self.async_registry = Registry().with_resource("urn:asyncapi", Resource(contents=self.asyncapi,
                                                                                specification=DRAFT202012))
        self.messages = {m["name"]: m["payload"]["$ref"] for m in self.asyncapi["components"]["messages"].values()}
        self.checked = {"responses": 0, "requests": 0, "events": 0}
        self.seen_operations: set[str] = set()

    def find(self, method: str, path: str):
        for spec in self.specs:
            for rx, m, tpl, op in spec.ops:
                if m == method and rx.match(path):
                    return spec, tpl, op
        return None

    def check_request(self, method: str, path: str, body):
        found = self.find(method, path)
        if not found or body is None:
            return
        spec, tpl, op = found
        rb = op.get("requestBody")
        if not rb:
            return
        schema = spec.resolve(rb)["content"]["application/json"]["schema"]
        spec.validate(schema, body, f"{spec.name} {method} {tpl} request")
        self.checked["requests"] += 1

    def check_response(self, method: str, path: str, status: int, content_type: str, body):
        found = self.find(method, path)
        if not found:
            return
        spec, tpl, op = found
        self.seen_operations.add(f"{spec.name} {op['operationId']}")
        responses = op["responses"]
        if str(status) not in responses:
            if status in (409, 422) and "IDEMPOTENCY" in json.dumps(body or {}):
                return  # общие ответы механизма идемпотентности
            if status in (401, 429, 503):
                return  # ответы Gateway и общей инфраструктуры
            raise ContractViolation(f"{spec.name} {method} {tpl}: код {status} не описан в контракте "
                                    f"(есть {', '.join(responses)})")
        resp = spec.resolve(responses[str(status)])
        content = resp.get("content")
        if not content:
            return
        mt = next(iter(content))
        if content_type.split(";")[0].strip() != mt:
            raise ContractViolation(f"{spec.name} {method} {tpl} {status}: Content-Type {content_type}, ожидался {mt}")
        spec.validate(content[mt]["schema"], body, f"{spec.name} {method} {tpl} {status}")
        self.checked["responses"] += 1

    def check_event(self, event: dict):
        ref = self.messages.get(event.get("eventType"))
        if not ref:
            raise ContractViolation(f"событие {event.get('eventType')} не описано в asyncapi.yaml")
        v = Draft202012Validator({"$ref": "urn:asyncapi" + ref}, registry=self.async_registry,
                                 format_checker=FormatChecker())
        errors = list(v.iter_errors(event))
        if errors:
            e = errors[0]
            raise ContractViolation(f"событие {event['eventType']}: {'/'.join(map(str, e.absolute_path))}: {e.message}")
        self.checked["events"] += 1
