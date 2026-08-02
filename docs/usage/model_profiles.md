# Model Profile Switching

## Purpose

Model profiles keep model-specific token budgets, temperatures, and thinking
rules out of individual commands. Selecting a profile does not start or stop an
LLM server.

## Single-Resident Model Workflow

On a machine that can keep only one large model resident:

1. Stop or switch the externally managed model server.
2. Confirm that `GET /v1/models` reports the intended model.
3. Select the matching Local SDLC Agent profile.
4. Run `doctor` before starting a long agent job.

```bash
python3 local_sdlc.py doctor --model-profile deepseek-v4-flash-agent
```

The request is rejected before generation when the named profile's effective
model is absent from `/v1/models`. No automatic fallback, model substitution,
container start, or second model launch occurs.

## Available DeepSeek Profiles

| Profile | Intended use | Thinking | Output budget |
|---|---|---|---:|
| `deepseek-v4-flash-agent` | First run and artifact-sensitive work | Off for every function | Up to 8192 |
| `deepseek-v4-flash-agent-deep` | Explicit analysis experiment | On for analysis; off for artifacts | Up to 8192 |

The stable profile is the default recommendation. The deep profile should be
used only after Doctor confirms that the serving stack returns reasoning in
`reasoning_content` and the final answer in `content`.

## Returning to Qwen

After the externally managed API reports the Qwen model again:

```bash
python3 local_sdlc.py doctor --model-profile qwen-agent
```

For a project config, leave `model` empty and change only `model_profile`:

```json
{
  "llm": {
    "base_url": "http://localhost:30000/v1",
    "model": "",
    "model_profile": "deepseek-v4-flash-agent"
  }
}
```

Avoid model-specific `api_profile` overrides in the shared config unless an
experiment requires them. Explicit overrides have higher priority than the
named preset and can otherwise carry Qwen-sized budgets into a DeepSeek run.

## Function-Level Model Overrides

`--api-profile FUNCTION:model=...` remains supported. It succeeds only when
that model is listed by the same API endpoint. With one resident model, an
override to the inactive model is rejected; with a future multi-model endpoint,
the same command can work without an architecture change.
