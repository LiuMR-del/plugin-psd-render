"""OpenAI-compatible AI relay client for template mapping suggestions.

The client never persists credentials. Callers provide an API key directly or
through an environment variable. Only structured, sanitized payloads should be
sent by the application.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

import ai_runtime

DEFAULT_MODELS = ["gpt-5.5", "gpt-5.6-terra", "glm-5.3"]
DEFAULT_TIMEOUT = 90.0
DEFAULT_TOTAL_TIMEOUT = 120.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# A packed exe unpacks code to a temp folder; keep .env next to the exe so
# the API settings saved in the UI survive restarts.
DOTENV_PATH = (Path(sys.executable).resolve().with_name(".env") if getattr(sys, "frozen", False)
               else Path(__file__).resolve().with_name(".env"))
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*([\s\S]*?)\s*```$", re.I)


def load_project_env(path: str | Path | None = None, *, override: bool = False) -> bool:
    """Load a local .env without overriding explicit OS variables by default."""
    return load_dotenv(Path(path) if path is not None else DOTENV_PATH, override=override)


class AIClientError(RuntimeError):
    """All user-facing AI request failures use this exception."""

    def __init__(self, message: str, *, kind: str = "unknown", http_status: int | None = None,
                 retryable: bool = False, fallbackable: bool = True) -> None:
        super().__init__(message)
        self.kind = str(kind or "unknown")
        self.http_status = http_status
        self.retryable = bool(retryable)
        self.fallbackable = bool(fallbackable)


@dataclass
class AIAttempt:
    model: str
    ok: bool
    error: str = ""
    kind: str = ""
    duration_ms: int = 0
    provider_id: str = ""
    provider_name: str = ""


@dataclass
class AIResult:
    payload: dict[str, Any]
    model: str
    attempts: list[AIAttempt] = field(default_factory=list)
    duration_ms: int = 0
    provider_id: str = ""
    provider_name: str = ""


def _safe_error(value: Any) -> str:
    text = str(value or "").replace("\n", " ").strip()
    # Never leak credential-like values or authorization headers into UI/logs.
    text = re.sub(r"(?i)authorization\s*:\s*bearer\s+[^\s,;]+", "Authorization: Bearer [redacted]", text)
    text = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [redacted]", text)
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", text)
    return text[:500]


def validate_base_url(value: str) -> str:
    """Validate and normalize an OpenAI-compatible relay base URL."""
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        raise AIClientError("请填写 AI 服务地址", kind="invalid_config", fallbackable=False)
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AIClientError("AI 服务地址必须是有效的 HTTP 或 HTTPS 地址", kind="invalid_config", fallbackable=False)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AIClientError("AI 服务地址不能包含账号、查询参数或片段", kind="invalid_config", fallbackable=False)
    return raw


def validate_api_key(value: str) -> str:
    key = str(value or "").strip()
    if not key:
        raise AIClientError("请填写 API Key")
    if len(key) > 4096 or any(char in key for char in "\r\n\x00"):
        raise AIClientError("API Key 格式无效")
    return key


def validate_models(models: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in models:
        model = str(raw or "").strip()
        if not model or model in seen:
            continue
        if len(model) > 200 or any(char in model for char in ",\r\n\x00"):
            raise AIClientError("模型名称包含不支持的字符")
        seen.add(model)
        result.append(model)
    if not result:
        raise AIClientError("请至少选择一个模型")
    return result


def _read_limited(response, limit: int = MAX_RESPONSE_BYTES) -> bytes:
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise AIClientError("AI 响应内容过大", kind="response_too_large", fallbackable=False)
    return raw


def _error_for_http(status: int, body: str, *, prefix: str = "AI") -> AIClientError:
    body_folded = str(body or "").casefold()
    quota_exhausted = "api_key_quota_exhausted" in body_folded or "quota exhausted" in body_folded
    retryable = status in {408, 425, 429, 500, 502, 503, 504} and not quota_exhausted
    kind = (
        "quota_exhausted" if quota_exhausted else
        "auth" if status in {401, 403} else
        "model_unavailable" if status == 404 else
        "rate_limit" if status == 429 else
        "transient_http" if retryable else
        "invalid_request"
    )
    return AIClientError(
        f"{prefix} HTTP {status}: {_safe_error(body)}",
        kind=kind, http_status=status, retryable=retryable,
        fallbackable=status not in {401, 403} and not quota_exhausted,
    )


def parse_json_content(content: Any) -> dict[str, Any]:
    if isinstance(content, list):
        content = "".join(
            str(item.get("text", "")) for item in content if isinstance(item, dict)
        )
    if isinstance(content, dict):
        return content
    raw = str(content or "").strip()
    fenced = _JSON_FENCE_RE.match(raw)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AIClientError("AI 返回的内容不是合法 JSON") from exc
    if not isinstance(parsed, dict):
        raise AIClientError("AI 返回的 JSON 顶层必须是对象")
    return parsed


def _endpoint(base_url: str) -> str:
    base = validate_base_url(base_url)
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def models_endpoint(base_url: str) -> str:
    base = validate_base_url(base_url)
    return base + "/models" if base.endswith("/v1") else base + "/v1/models"


def fetch_models(base_url: str, api_key: str, timeout: float = 15.0) -> list[str]:
    """Fetch a bounded OpenAI-compatible model ID list."""
    key = validate_api_key(api_key)
    request = urllib.request.Request(
        models_endpoint(base_url),
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            raw = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode("utf-8", errors="replace")
        raise _error_for_http(exc.code, body, prefix="拉取模型失败") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AIClientError(f"拉取模型失败：{_safe_error(exc)}") from exc
    if len(raw) > 1024 * 1024:
        raise AIClientError("模型列表响应过大")
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise AIClientError("模型列表响应不是合法 JSON") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise AIClientError("服务未返回 OpenAI 兼容的模型列表")
    models = validate_models([item.get("id", "") for item in data if isinstance(item, dict)])
    if len(models) > 500:
        models = models[:500]
    return models


_OLD_PROVIDER_KEY_RE = re.compile(r"^PSD_RENDER_AI_(?!API_KEY$)[A-Z0-9_]+_KEY$")


def save_environment_config(base_url: str, api_key: str, models: list[str],
                            env_path: str | Path | None = None) -> None:
    """Atomically persist AI settings to the project .env and reload it."""
    url = validate_base_url(base_url)
    key = validate_api_key(api_key)
    selected = validate_models(models)
    path = Path(env_path) if env_path is not None else DOTENV_PATH
    try:
        existing = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        existing = []
    values = {
        "PSD_RENDER_AI_BASE_URL": url,
        "PSD_RENDER_AI_API_KEY": key,
        "PSD_RENDER_AI_MODELS": ",".join(selected),
    }
    output: list[str] = []
    seen: set[str] = set()
    for line in existing:
        matched = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        name = matched.group(1) if matched else ""
        if _OLD_PROVIDER_KEY_RE.match(name):
            # 备用 API 已取消（2026-10-14），保存时一并清掉旧的备用 Key
            os.environ.pop(name, None)
            continue
        if name in values:
            output.append(f"{name}={values[name]}")
            seen.add(name)
        else:
            output.append(line)
    for name, value in values.items():
        if name not in seen:
            output.append(f"{name}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".env-", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output).rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    load_project_env(path, override=True)


_OPTIONAL_PARAMS = ("reasoning_effort", "response_format")


def _request_once(*, base_url: str, api_key: str, model: str,
                  messages: list[dict[str, str]], timeout: float) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": messages,
        # 保留高推理；reasoning 模式下不额外发送 temperature，避免
        # 中转服务把 temperature=0 当成冲突参数或触发更慢的兼容路径。
        "reasoning_effort": "high",
        "response_format": {"type": "json_object"},
    }

    def send(current: dict[str, Any]) -> str:
        request = urllib.request.Request(
            _endpoint(base_url),
            data=json.dumps(current, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=max(0.1, float(timeout))) as response:
                return _read_limited(response).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read(MAX_RESPONSE_BYTES + 1).decode("utf-8", errors="replace")
            if len(body) > MAX_RESPONSE_BYTES:
                body = body[:MAX_RESPONSE_BYTES]
            # 中转站里 Claude/Gemini/豆包等非 OpenAI 模型可能不支持其中某个参数:
            # 仅在服务明确点名拒绝时去掉该参数重发；不降低默认请求的推理程度。
            # 去掉 response_format 后仍按提示词要求返回 JSON（parse_json_content 兼容代码块）。
            rejected = next((name for name in _OPTIONAL_PARAMS
                             if name in current and name in body.casefold()), "")
            if exc.code in {400, 422} and rejected:
                fallback = dict(current)
                fallback.pop(rejected, None)
                return send(fallback)
            raise _error_for_http(exc.code, body) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AIClientError(
                f"AI 网络请求失败: {_safe_error(exc)}",
                kind="timeout" if isinstance(exc, TimeoutError) else "transient_network",
                retryable=True,
            ) from exc

    raw = send(payload)
    try:
        envelope = json.loads(raw)
    except ValueError as exc:
        raise AIClientError("AI 服务返回的响应不是合法 JSON", kind="invalid_response", retryable=True) from exc
    if not isinstance(envelope, dict):
        raise AIClientError("AI 服务响应顶层必须是对象", kind="invalid_response", retryable=True)
    if envelope.get("error"):
        raise AIClientError(
            f"AI 服务错误: {_safe_error(envelope['error'])}",
            kind="service_error", retryable=False,
        )
    choices = envelope.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise AIClientError("AI 响应缺少 choices", kind="invalid_response", retryable=True)
    message = choices[0].get("message") or {}
    return parse_json_content(message.get("content"))


def request_json(*, base_url: str, api_key: str, messages: list[dict[str, str]],
                 models: list[str] | None = None, timeout: float = DEFAULT_TIMEOUT,
                 total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
                 retries: int = 1, sleep_seconds: float = 0.4) -> AIResult:
    """Try validated models within one shared deadline, preserving high reasoning."""
    if not str(api_key or "").strip():
        raise AIClientError("未配置 AI API Key", kind="invalid_config", fallbackable=False)
    model_list = validate_models(list(models or DEFAULT_MODELS))
    timeout = max(0.1, min(float(timeout), 300.0))
    total_timeout = max(0.1, min(float(total_timeout), 600.0))
    retry_count = max(0, min(int(retries), 2))
    deadline = time.monotonic() + total_timeout
    attempts: list[AIAttempt] = []
    started = time.monotonic()
    for model in model_list:
        for attempt in range(retry_count + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AIClientError("AI 分析超过总时间限制，请稍后重试", kind="deadline", retryable=False)
            attempt_started = time.monotonic()
            try:
                payload = _request_once(
                    base_url=base_url, api_key=api_key, model=model,
                    messages=messages, timeout=min(timeout, remaining),
                )
                attempts.append(AIAttempt(model, True, duration_ms=int((time.monotonic() - attempt_started) * 1000)))
                return AIResult(payload, model, attempts, int((time.monotonic() - started) * 1000))
            except AIClientError as exc:
                attempts.append(AIAttempt(
                    model, False, _safe_error(exc), exc.kind,
                    int((time.monotonic() - attempt_started) * 1000),
                ))
                if not exc.fallbackable:
                    raise
                if not exc.retryable or attempt >= retry_count:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(max(0.0, float(sleep_seconds)), remaining))
        # Continue to the configured fallback model only when the error allows it.
    summary = "；".join(f"{item.model}: {item.error}" for item in attempts if not item.ok)
    last_kind = next((item.kind for item in reversed(attempts) if not item.ok), "all_models_failed")
    raise AIClientError("所有 AI 模型均不可用" + (f"（{summary}）" if summary else ""), kind=last_kind or "all_models_failed")


def request_json_providers(*, providers: list[dict], messages: list[dict[str, str]],
                           total_timeout: float = DEFAULT_TOTAL_TIMEOUT, retries: int = 1) -> AIResult:
    """Fail over across providers, then models within each provider.

    A quota/auth failure opens the current provider and immediately moves to
    the next configured provider; it never burns the remaining models on the
    same exhausted key.
    """
    started = time.monotonic()
    runtime = ai_runtime.load_runtime()
    errors: list[str] = []
    failure_kinds: list[str] = []
    attempts: list[AIAttempt] = []
    deadline = started + max(0.1, min(float(total_timeout), 600.0))
    for provider in providers or []:
        if not provider.get("enabled", True):
            continue
        provider_id = str(provider.get("id") or "")
        api_key = str(provider.get("api_key") or "").strip()
        base_url = str(provider.get("base_url") or "").strip()
        if not base_url or not api_key:
            continue
        entry = ai_runtime.provider_state(runtime, provider, api_key)
        if ai_runtime.is_blocked(entry):
            errors.append(f"{provider_id}: {entry.get('status')}")
            failure_kinds.append("quota_exhausted" if entry.get("status") == "quota_exhausted" else str(entry.get("status")))
            continue
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            result = request_json(
                base_url=base_url, api_key=api_key, messages=messages,
                models=list(provider.get("models") or DEFAULT_MODELS),
                timeout=min(DEFAULT_TIMEOUT, remaining), total_timeout=remaining,
                retries=retries,
            )
            ai_runtime.mark_success(runtime, provider, api_key)
            ai_runtime.save_runtime(runtime)
            for attempt in result.attempts:
                attempt.provider_id = provider_id
                attempt.provider_name = str(provider.get("name") or provider_id)
            result.provider_id = provider_id
            result.provider_name = str(provider.get("name") or provider_id)
            return result
        except AIClientError as exc:
            ai_runtime.mark_failure(runtime, provider, api_key, exc.kind, _safe_error(exc))
            ai_runtime.save_runtime(runtime)
            errors.append(f"{provider_id}: {_safe_error(exc)}")
            failure_kinds.append(exc.kind)
            if exc.kind in {"quota_exhausted", "auth", "invalid_config"}:
                continue
            if not exc.fallbackable:
                continue
    if failure_kinds and all(kind == "quota_exhausted" for kind in failure_kinds):
        # Every configured API is out of quota: the UI shows a dedicated popup.
        raise AIClientError("AI 额度已用完" + (f"（{'；'.join(errors)}）" if errors else ""), kind="quota_exhausted",
                            fallbackable=False)
    kind = failure_kinds[0] if len(set(failure_kinds)) == 1 else "all_providers_failed"
    raise AIClientError("AI 接口不可用" + (f"（{'；'.join(errors)}）" if errors else ""), kind=kind)


def _section_identity(raw: Any) -> Any:
    """Return the caller-supplied section identity without inventing one."""
    if not isinstance(raw, dict):
        return ""
    for key in ("section_identity", "section", "section_id", "section_path", "section_name"):
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return ""


def _raw_customization_fields(order_fields: Any) -> Any:
    """Normalize raw fields once while retaining section identity."""
    inherited_section = _section_identity(order_fields) if isinstance(order_fields, dict) else ""
    if isinstance(order_fields, dict):
        fields = order_fields.get("fields")
        source = fields if isinstance(fields, list) else [order_fields]
    elif isinstance(order_fields, list):
        source = order_fields
    else:
        return order_fields
    result: list[Any] = []
    for item in source:
        if not isinstance(item, dict):
            result.append(item)
            continue
        copied = {
            "section_identity": _section_identity(item) or inherited_section,
            "source_key": str(item.get("source_key") or item.get("label") or ""),
            "value": str(item.get("value") or ""),
        }
        if "values" in item:
            copied["values"] = [str(value) for value in item.get("values") or []]
        result.append(copied)
    return result


def mapping_messages(*, sku: str, size_key: str, filename: str,
                     order_fields: list[dict] | dict | None = None,
                     candidate_package: dict | None = None,
                     layer_tree: list[dict] | dict | None = None,
                     providers: list[dict] | None = None,
                     prior_profile: dict | None = None,
                     raw_customization_fields: Any = None) -> list[dict[str, str]]:
    """Build a bounded ambiguity-mapping prompt; never send a full PSD tree."""
    raw_fields = order_fields if raw_customization_fields is None else raw_customization_fields
    system = (
        "你是铝板画模板映射裁决助手。只返回 JSON 对象，不要 Markdown。"
        "你只能从 candidate_package 提供的完整路径中选择，不能创造、改写或猜测路径。"
        "如果候选不足以确定，必须写入 unresolved_fields，不要自行扩大范围。"
        "人物素材字段才允许生成 person_style_mappings；No、None、无、隐藏等值必须生成空候选并隐藏 parent_path。"
        "text_mappings 只能为 candidate_package.text_candidates.fields 中的字段生成，"
        "target_path/smart_object_path 只能取自 text_candidates.layers；"
        "年份字段只能对应 role=year 的图层，姓名字段只能对应 role=name 的图层，一个图层只能对应一个字段；"
        "可参考图层的 text 和 bbox（[左,上,右,下] 像素）判断左右位置，男方姓名通常在男性人物一侧。"
        "模板中找不到合适图层的字段写入 unresolved_fields，不要硬凑。"
    )
    if candidate_package is None and layer_tree is not None:
        candidate_package = {"legacy_layer_tree": layer_tree}
    request = {
        "task": "judge_ambiguous_template_candidates",
        "sku": str(sku),
        "size_key": str(size_key),
        "template_filename": str(filename),
        "customization_fields": _raw_customization_fields(raw_fields),
        "candidate_package": candidate_package or {},
        "prior_profile": prior_profile or {},
        "output_schema": {
            "normalized_fields": {"Custom Year": ["1991"], "Man's Hair Color": ["Brown"]},
            "field_types": {"Custom Year": "text", "Man's Hair Color": "person_style"},
            "person_style_mappings": [{
                "source_key": "Man's Hair Color",
                "sample_values": ["Brown"],
                "parent_path": ["Man素材", "组 1", "3-Man's Hair Color"],
                "smart_object_path": ["人物素材"],
                "value_mapping": {"Brown": [["Man素材", "组 1", "3-Man's Hair Color", "Brown"]]},
                "hide_parent_values": [], "confidence": 0.0, "required": False,
            }],
            "text_mappings": [{
                "source_key": "Custom Man's Name", "section": "Customize Text Information",
                "smart_object_path": [], "target_path": ["组 1", "Joe"], "confidence": 0.0,
            }],
            "ignored_fields": [], "unresolved_fields": [],
            "rule": "只能选择 candidate_package 中已有完整路径",
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
    ]


def analyze_template(*, base_url: str, api_key: str, sku: str, size_key: str,
                     filename: str, order_fields: list[dict] | dict | None = None,
                     candidate_package: dict | None = None,
                     layer_tree: list[dict] | dict | None = None,
                     providers: list[dict] | None = None,
                     prior_profile: dict | None = None,
                     raw_customization_fields: Any = None,
                     models: list[str] | None = None, timeout: float = DEFAULT_TIMEOUT,
                     total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
                     retries: int = 1) -> AIResult:
    if providers:
        return request_json_providers(
            providers=providers,
            messages=mapping_messages(
                sku=sku, size_key=size_key, order_fields=order_fields,
                filename=filename, candidate_package=candidate_package,
                prior_profile=prior_profile, raw_customization_fields=raw_customization_fields,
            ),
            total_timeout=total_timeout, retries=retries,
        )
    return request_json(
        base_url=base_url,
        api_key=api_key,
        messages=mapping_messages(
            sku=sku, size_key=size_key, filename=filename,
            order_fields=order_fields, candidate_package=candidate_package,
            prior_profile=prior_profile,
            raw_customization_fields=raw_customization_fields,
        ),
        models=models,
        timeout=timeout,
        total_timeout=total_timeout,
        retries=retries,
    )


def order_mapping_messages(*, sku: str, size_key: str, filename: str, request: dict) -> list[dict[str, str]]:
    """One isolated order: its full customization text + the template summary."""
    system = (
        "你是定制产品（铝板画、毛毯等）PSD 模板映射助手。只返回 JSON 对象，不要 Markdown。"
        "输入是一个订单的完整定制文案（customization_text）和这个 PSD 模板的图层摘要。"
        "只需要为 fields_to_map 里的字段给出规则；already_handled 里的字段程序已经处理好，不要重复。"
        "规则是“字段名 → 图层”，以后同模板的订单会复用规则、只替换数值，所以不要把具体数值写死进文字。"
        "姓名、年份、标题、首字母、日期等值要写进图片的字段输出 text_mappings，只能选 text_layers 里的图层；"
        "年份只能对应 role=year 的图层，姓名只能对应 role=name 的图层，一个图层只能对应一个字段；"
        "可参考 bbox（[左,上,右,下]）判断左右，男方姓名通常在男性人物一侧。"
        "设置文字颜色或字体的字段（kind 为 color/font，或你判断是改文字颜色的 Color 字段）输出 style_mappings，"
        "style 为 color 或 font；targets 是要改样式的文字图层（取自 text_layers），留空表示所有姓名图层；"
        "颜色值里没有 #RRGGBB 色值时（如 Light Pink），在 color_values 里按颜色名给出常用色值；"
        "颜色、字体字段不能写进 text_mappings。"
        "人物素材或选项类字段（发型、饮品、标语样式等）需要切换图层显示时输出 person_style_mappings，"
        "parent_path 是互斥的父级组，value_mapping 把本订单的值映射到要显示的完整路径，只能取自 material_candidates 或 layer_outline；"
        "No、None、无等值给空列表并写入 hide_parent_values。"
        "确认、尺寸、款式、特殊要求等不影响图片内容的字段写入 ignored_fields。"
        "模板里确实没有对应图层、无法自动完成的字段写入 unresolved_fields，不要硬凑，不能创造或改写路径。"
    )
    user = {
        "task": "map_one_order_to_template",
        "sku": str(sku), "size_key": str(size_key), "template_filename": str(filename),
        **request,
        "output_schema": {
            "text_mappings": [{"section": "Customize Text Information", "source_key": "Custom Man's Name",
                               "smart_object_path": [], "target_path": ["组 1", "Joe"], "confidence": 0.0}],
            "person_style_mappings": [{"section": "Customize", "source_key": "Choose Quote",
                                       "parent_path": ["定制项"], "smart_object_path": [],
                                       "value_mapping": {"GRILL & CHILL": [["定制项", "GRILL & CHILL"]]},
                                       "hide_parent_values": [], "confidence": 0.0}],
            "style_mappings": [{"section": "表面 1", "source_key": "Text Color", "style": "color",
                                "targets": [{"smart_object_path": [], "target_path": ["组 1", "Title"]}]}],
            "color_values": {"Light Pink": "#FFB6C1"},
            "ignored_fields": ["CONFIRM CUSTOM INFORMATION"],
            "unresolved_fields": [],
        },
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def analyze_order(*, providers: list[dict], sku: str, size_key: str, filename: str, request: dict,
                  total_timeout: float = DEFAULT_TOTAL_TIMEOUT, retries: int = 1) -> AIResult:
    return request_json_providers(
        providers=providers,
        messages=order_mapping_messages(sku=sku, size_key=size_key, filename=filename, request=request),
        total_timeout=total_timeout, retries=retries,
    )


def environment_config(reload_env: bool = False, env_path: str | Path | None = None) -> tuple[str, str, list[str]]:
    """Read the legacy single-provider environment configuration."""
    if reload_env:
        load_project_env(env_path, override=True)
    else:
        load_project_env(env_path)
    base_url = os.environ.get("PSD_RENDER_AI_BASE_URL", "").strip()
    api_key = os.environ.get("PSD_RENDER_AI_API_KEY", "").strip()
    raw_models = os.environ.get("PSD_RENDER_AI_MODELS", "")
    models = [item.strip() for item in raw_models.split(",") if item.strip()] or list(DEFAULT_MODELS)
    return base_url, api_key, models


def provider_configs(reload_env: bool = False, env_path: str | Path | None = None, settings_cfg: dict | None = None) -> list[dict]:
    """The one configured API (project .env), or [] when URL/Key are missing.

    2026-10-14 user decision: only one API is kept; the former backup API
    (``settings_cfg["ai_profiles"]``) is ignored.  ``settings_cfg`` stays for
    call compatibility.
    """
    base_url, api_key, models = environment_config(reload_env=reload_env, env_path=env_path)
    provider = ai_runtime.legacy_provider(base_url, api_key, models)
    return [provider] if provider["base_url"] and provider["api_key"] else []


def check_health(base_url: str, api_key: str, *, timeout: float = 15.0, probe_inference: bool = False, model: str = "") -> dict:
    """Check transport health, optionally with a tiny JSON inference probe."""
    started = time.monotonic()
    models = fetch_models(base_url, api_key, timeout=timeout)
    result = {"status": "healthy", "models": models, "latency_ms": int((time.monotonic() - started) * 1000)}
    if probe_inference:
        chosen = model or (models[0] if models else "")
        if not chosen:
            raise AIClientError("健康检查没有可用模型", kind="model_unavailable", fallbackable=False)
        request_json(
            base_url=base_url, api_key=api_key,
            messages=[{"role": "system", "content": "Return a JSON object only."}, {"role": "user", "content": "Return {\\\"ok\\\":true}."}],
            models=[chosen], timeout=min(timeout, 30), total_timeout=min(timeout, 30), retries=0,
        )
        result["inference_model"] = chosen
    return result
