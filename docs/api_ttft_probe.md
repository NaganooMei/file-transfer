# Streaming API latency probe

`scripts/api_ttft_probe.py` compares OpenAI-compatible chat endpoints and Anthropic
Messages endpoints using a synthetic Python question. It does not read local chat
history or configuration.

Install Python 3 and `requests` (`python -m pip install requests`). Set an API key
in an environment variable, then run:

```sh
python scripts/api_ttft_probe.py \
  --url https://example.com/v1/chat/completions \
  --model model-id --key-env API_KEY --repeat 3
```

The command reads the key from `API_KEY`; do not put real keys in command arguments
or committed files. Each request can consume provider quota. Output excludes keys,
prompt text, completion text, and full exception messages.

The default respects proxy environment variables and reuses a connection across
rounds. `--direct` disables environment-based proxies. It does not bypass a VPN,
TUN adapter, or transparent network proxy. Compare both routes at the same time of
day; a fresh connection and a reused connection can have different latency.

The probe sends its own user agent and a stable `x-opencode-session` header within
one run. This matters for endpoints requiring session identity. It does not
impersonate a production coding client.

`--thinking disabled` (default) sends `thinking.type=disabled`. Use
`--thinking default` to omit that provider-specific parameter, or
`--thinking enabled` to request thinking. Not every provider supports this field.
`--max-tokens` defaults to 64.

Use `--format anthropic` with a `/v1/messages` endpoint (including a local gateway).
The credential must belong to that endpoint: a gateway may require its own local
token, rather than an upstream provider key. Anthropic enabled thinking uses a
1024-token budget and requires `--max-tokens` greater than 1024.

To investigate coding-agent workloads, repeat with synthetic context and tools:

```sh
python scripts/api_ttft_probe.py \
  --url https://example.com/v1/chat/completions \
  --model model-id --key-env API_KEY --repeat 2 --thinking default \
  --context-functions 3000 --tools 24 --max-tokens 256
```

3000 generated Python functions produce roughly 75K input tokens on DeepSeek;
24 synthetic tool definitions add roughly 2.5K tokens. Exact counts vary by model.
No tools are executed. Long-context requests can cost substantially more than the
default short probe. Compare a run without `--tools` to one with it; examine actual
usage and cache hits, and keep concurrent traffic low when comparing latency.

Each output line is JSON:

| Field | Meaning |
| --- | --- |
| `headers_s` | Request start to receiving HTTP response headers |
| `first_line_s` | Request start to the first nonempty stream line |
| `first_sse_s` | Request start to the first SSE data line, possibly metadata |
| `first_token_s` | First nonempty content or reasoning delta |
| `first_reasoning_s` | First nonempty reasoning delta, if present |
| `first_content_s` | First nonempty answer content delta |
| `total_s` | Time to completion or failure |
| `usage` | Provider-reported token usage, if provided |
| `response_headers` | Allowlisted diagnostic headers, including `server-timing` |

Timing uses a monotonic clock and single-byte stream reads to avoid application
buffering. Reading full SSE lines still adds a small parsing/transport delay.
The script does not measure tool-call argument deltas as tokens. Errors and missing
token timestamps must be treated as failures, not zero latency. HTTP header timing
includes DNS, connection establishment, TLS, network travel, and server waiting;
it is not a server processing measurement. A provider's `server-timing` is
self-reported and its exact phase definitions depend on that provider.
