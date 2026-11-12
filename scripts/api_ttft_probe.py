#!/usr/bin/env python3
"""Measure OpenAI/Anthropic streaming latency without printing credentials.

Requires requests. Example:
  python api_ttft_probe.py --url https://example.com/v1/chat/completions \
    --model model-id --key-env API_KEY --repeat 3
Credentials are read from the environment; output contains timing and usage only.
"""

import argparse
import json
import os
import time
import uuid

import requests


def probe(session, url, key, model, *, thinking="disabled", session_id=None,
          max_tokens=64, headers=None, api_format="openai", context_functions=0,
          tool_count=0):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a concise coding assistant."},
            {"role": "user", "content": "In Python, how do I get the length of a list named items? Reply with one line of code only."},
        ],
        "stream": True,
        "max_tokens": max_tokens,
    }
    if context_functions:
        reference = "\n\nSynthetic reference Python functions for a latency test. Answer the user question only; do not analyze these functions.\n"
        reference += "".join(
            f"def sample_{i:05d}(x): return (x * {7907 + i*2} + {104729 + i*7}) % 1000003\n"
            for i in range(context_functions)
        )
        payload["messages"][0]["content"] += reference
    if tool_count:
        specs = [{
            "name": f"inspect_synthetic_resource_{i:02d}",
            "description": f"Inspect the synthetic coding reference resource number {i}. Use only when the user explicitly asks for resource inspection.",
            "input_schema": {
                "type": "object", "properties": {
                    "path": {"type": "string", "description": "Synthetic resource path"},
                    "line": {"type": "integer", "description": "Optional reference line number"},
                }, "required": ["path"],
            },
        } for i in range(tool_count)]
        payload["tools"] = specs if api_format == "anthropic" else [{
            "type": "function", "function": {
                "name": spec["name"], "description": spec["description"],
                "parameters": spec["input_schema"],
            },
        } for spec in specs]
    if context_functions and api_format == "openai":
        payload["stream_options"] = {"include_usage": True}
    if thinking != "default":
        payload["thinking"] = {"type": thinking}
        if api_format == "anthropic" and thinking == "enabled":
            payload["thinking"]["budget_tokens"] = 1024
    if api_format == "anthropic":
        payload["system"] = payload["messages"].pop(0)["content"]
    request_headers = {
        "Authorization": "Bearer " + key,
        "User-Agent": "ccswitch-latency-diagnostic/1.0",
        "x-opencode-session": session_id or str(uuid.uuid4()),
    }
    request_headers.update(headers or {})
    if api_format == "anthropic":
        request_headers["anthropic-version"] = "2023-06-01"
    result = {"model": model, "thinking": thinking, "api_format": api_format,
              "context_functions": context_functions, "tools_count": tool_count}
    start = time.perf_counter()
    def elapsed():
        return round(time.perf_counter() - start, 4)
    try:
        with session.post(url, json=payload, headers=request_headers,
                          stream=True, timeout=(10, 40)) as response:
            result.update(status=response.status_code, headers_s=elapsed())
            allowed = {"server", "cf-ray", "server-timing", "x-request-id",
                       "x-opencode-endpoint-id", "x-opencode-upstream-model-id"}
            result["response_headers"] = {
                k: v for k, v in response.headers.items() if k.lower() in allowed
            }
            if response.status_code != 200:
                result["error"] = "http_error"
                return result
            result["events"] = 0
            # A single-byte chunk avoids requests' default buffering obscuring TTFT.
            for line in response.iter_lines(chunk_size=1):
                if elapsed() > 50:
                    result["error"] = "wall_time_limit"
                    break
                if not line:
                    continue
                result.setdefault("first_line_s", elapsed())
                if not line.startswith(b"data:"):
                    continue
                raw = line[5:].strip()
                result.setdefault("first_sse_s", elapsed())
                if raw == b"[DONE]":
                    result["done"] = True
                    break
                try:
                    data = json.loads(raw)
                except ValueError:
                    continue
                result["events"] += 1
                if "error" in data:
                    result["error"] = "stream_error"
                    break
                if data.get("usage"):
                    result["usage"] = data["usage"]
                if data.get("model"):
                    result["response_model"] = data["model"]
                if api_format == "anthropic":
                    if data.get("type") == "message_start":
                        message = data.get("message", {})
                        if message.get("model"):
                            result["response_model"] = message["model"]
                        if message.get("usage"):
                            result["usage"] = message["usage"]
                    elif data.get("type") == "message_delta":
                        result.setdefault("usage", {}).update(data.get("usage", {}))
                        if data.get("delta", {}).get("stop_reason"):
                            result["finish_reason"] = data["delta"]["stop_reason"]
                    elif data.get("type") == "message_stop":
                        result["done"] = True
                        break
                    delta = data.get("delta", {})
                    block = data.get("content_block", {})
                    if delta.get("thinking") or block.get("thinking"):
                        result.setdefault("first_reasoning_s", elapsed())
                        result.setdefault("first_token_s", elapsed())
                    if delta.get("text") or block.get("text"):
                        result.setdefault("first_content_s", elapsed())
                        result.setdefault("first_token_s", elapsed())
                for choice in data.get("choices", []):
                    delta = choice.get("delta", {})
                    if delta.get("reasoning_content") or delta.get("reasoning"):
                        result.setdefault("first_reasoning_s", elapsed())
                        result.setdefault("first_token_s", elapsed())
                    if delta.get("content"):
                        result.setdefault("first_content_s", elapsed())
                        result.setdefault("first_token_s", elapsed())
                    if choice.get("finish_reason"):
                        result["finish_reason"] = choice["finish_reason"]
    except requests.RequestException as error:
        # Exception text may contain URLs or request information; emit only type.
        result["error"] = type(error).__name__
    finally:
        result["total_s"] = elapsed()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--key-env", default="API_KEY")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--direct", action="store_true")
    parser.add_argument("--thinking", choices=["default", "disabled", "enabled"], default="disabled")
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--format", choices=["openai", "anthropic"], default="openai")
    parser.add_argument("--context-functions", type=int, default=0,
                        help="Append generated synthetic code; 3000 functions are roughly 75K DeepSeek tokens")
    parser.add_argument("--tools", type=int, default=0,
                        help="Include synthetic tool definitions; no tools are executed")
    args = parser.parse_args()
    key = os.environ.get(args.key_env)
    if not key:
        parser.error("credential environment variable is empty")
    if args.format == "anthropic" and args.thinking == "enabled" and args.max_tokens <= 1024:
        parser.error("Anthropic enabled thinking requires --max-tokens greater than 1024")
    if args.repeat <= 0 or args.context_functions < 0 or args.tools < 0 or args.max_tokens <= 0:
        parser.error("repeat/max-tokens must be positive; context-functions/tools must be nonnegative")
    session_id = str(uuid.uuid4())
    with requests.Session() as session:
        session.trust_env = not args.direct
        for index in range(args.repeat):
            result = probe(session, args.url, key, args.model,
                           thinking=args.thinking, session_id=session_id,
                           max_tokens=args.max_tokens, api_format=args.format,
                           context_functions=args.context_functions, tool_count=args.tools)
            print(json.dumps({"round": index + 1, **result}), flush=True)


if __name__ == "__main__":
    main()
