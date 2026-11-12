#!/usr/bin/env python3
"""Measure OpenAI-compatible streaming latency without printing credentials.

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
          max_tokens=64, headers=None):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a concise coding assistant."},
            {"role": "user", "content": "In Python, how do I get the length of a list named items? Reply with one line of code only."},
        ],
        "stream": True,
        "max_tokens": max_tokens,
    }
    if thinking != "default":
        payload["thinking"] = {"type": thinking}
    request_headers = {
        "Authorization": "Bearer " + key,
        "User-Agent": "ccswitch-latency-diagnostic/1.0",
        "x-opencode-session": session_id or str(uuid.uuid4()),
    }
    request_headers.update(headers or {})
    result = {"model": model, "thinking": thinking}
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
    args = parser.parse_args()
    key = os.environ.get(args.key_env)
    if not key:
        parser.error("credential environment variable is empty")
    session_id = str(uuid.uuid4())
    with requests.Session() as session:
        session.trust_env = not args.direct
        for index in range(args.repeat):
            result = probe(session, args.url, key, args.model,
                           thinking=args.thinking, session_id=session_id,
                           max_tokens=args.max_tokens)
            print(json.dumps({"round": index + 1, **result}), flush=True)


if __name__ == "__main__":
    main()
