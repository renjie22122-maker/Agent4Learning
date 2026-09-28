"""工具参数校验：实验、平台与编码 Agent 共用的协议边界。"""
import re
import math
from typing import Any

class SchemaError(Exception):
    """参数不合法。**错误信息会回灌给模型**，所以要说清"哪个字段、错在哪、期望什么"。"""


def validate_schema(value: Any, schema: dict, path: str = "$") -> None:
    for keyword in ('$ref', 'oneOf', 'anyOf', 'allOf', 'not'):
        if keyword in schema:
            raise SchemaError(f'{path}: 当前校验器不支持 {keyword}，拒绝静默绕过校验')
    t = schema.get("type")
    if isinstance(t, list):
        errors = []
        for candidate in t:
            try:
                validate_schema(value, {**schema, 'type': candidate}, path)
                return
            except SchemaError as exc:
                errors.append(str(exc))
        raise SchemaError('; '.join(errors))
    if 'enum' in schema and value not in schema['enum']:
        raise SchemaError(f'{path}: 值不在 enum 中')
    if t == 'null' and value is not None:
        raise SchemaError(f'{path}: 期望 null')
    if t == "object":
        if not isinstance(value, dict):
            raise SchemaError(f"{path}: 期望 object，实际 {type(value).__name__}")
        for req in schema.get("required", []):
            if req not in value:
                raise SchemaError(f"{path}.{req}: 缺少必填字段（required）")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise SchemaError(
                    f"{path}: 出现未定义字段 {sorted(extra)}；允许的字段为 {sorted(props)}"
                )
        for k, v in value.items():
            if k in props:
                validate_schema(v, props[k], f"{path}.{k}")
    elif t == "string":
        if not isinstance(value, str):
            raise SchemaError(f"{path}: 期望 string，实际 {type(value).__name__}")
        if "enum" in schema and value not in schema["enum"]:
            raise SchemaError(f"{path}: 取值必须在 {schema['enum']} 内，实际 {value!r}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaError(f"{path}: 长度 {len(value)} 超过 maxLength={schema['maxLength']}")
        if 'minLength' in schema and len(value) < schema['minLength']:
            raise SchemaError(f'{path}: 字符串长度不足')
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise SchemaError(f"{path}: 不匹配 pattern={schema['pattern']}（实际 {value!r}）")
    elif t == "number" or t == "integer":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SchemaError(f"{path}: 期望 {t}，实际 {type(value).__name__}")
        if t == "integer" and not isinstance(value, int):
            raise SchemaError(f"{path}: 期望整数，实际 {value}")
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaError(f"{path}: {value} 小于 minimum={schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaError(f"{path}: {value} 大于 maximum={schema['maximum']}")
    elif t == "boolean":
        if not isinstance(value, bool):
            raise SchemaError(f"{path}: 期望 boolean，实际 {type(value).__name__}")
    elif t == "array":
        if not isinstance(value, list):
            raise SchemaError(f"{path}: 期望 array，实际 {type(value).__name__}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaError(f"{path}: 元素数 {len(value)} 超过 maxItems={schema['maxItems']}")
        if 'minItems' in schema and len(value) < schema['minItems']:
            raise SchemaError(f'{path}: 数组长度不足')
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(value):
                validate_schema(item, item_schema, f"{path}[{i}]")


