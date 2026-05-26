# 7 — Module: LLM (`app/llm/`)

The **llm** module wraps OpenAI and contains all prompts. It does **three things**:

1. **Intent extraction** — NL prompt → `(template_id, params)`
2. **Insight generation** — query results → human readable summary
3. **Title generation** — first prompt of a session → short session title

The LLM never writes SQL. It picks from a fixed set of curated templates. This is intentional: it eliminates prompt injection and SQL injection from the LLM path entirely.

## File map

```
app/llm/
├── client.py              # AsyncOpenAI wrapper + retries
├── intent_extractor.py    # NL → IntentResult(template_id, params)
├── insight_generator.py   # rows + intent → text summary
├── title_generator.py     # first prompt → session title
└── prompts/
    ├── intent.txt         # System prompt for intent extraction
    ├── insight.txt        # System prompt for insight generation
    └── title.txt          # System prompt for title
```

## `client.py` — OpenAI wrapper

Thin wrapper around `openai.AsyncOpenAI` with:

- Configurable timeout (`OPENAI_TIMEOUT_SECONDS`)
- Retry on transient errors (5xx, rate limit) via `tenacity`
- Sensitive field redaction in logs
- JSON mode enforcement (`response_format={"type": "json_object"}`)

### Usage

```python
from app.llm.client import get_llm_client

client = get_llm_client()
resp = await client.chat.completions.create(
    model=settings.OPENAI_MODEL,
    messages=[...],
    response_format={"type": "json_object"},
)
```

## `intent_extractor.py` — The brain

This is the most critical LLM call.

### Input

- User's natural language prompt
- Optional: rolling chat context (last N messages)

### Output (strict JSON)

```python
class IntentResult(BaseModel):
    template_id: str          # must match a known template
    params: dict[str, Any]    # keys must match template's parameter schema
    confidence: float         # 0.0 - 1.0
    reasoning: str            # for debugging / audit
```

### How it works

```python
async def extract(prompt: str, context: list[dict] = None) -> IntentResult:
    system_prompt = render_intent_prompt(known_templates=template_loader.list_templates())
    messages = [
        {"role": "system", "content": system_prompt},
        *(context or []),
        {"role": "user", "content": prompt},
    ]
    
    response = await client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    raw = json.loads(response.choices[0].message.content)
    return IntentResult.model_validate(raw)
```

### What the prompt tells the LLM

`prompts/intent.txt` (excerpt):

```
You are an analytics router. Given a user question, pick exactly ONE template
from the registry below and extract parameters.

REGISTRY:
- top_customers_by_revenue(limit:int<=100, period_days:int)
- revenue_over_time(granularity: day|week|month, period_days:int)
- active_users_count(period_days:int)
- ping()

Respond with JSON:
{ "template_id": "...", "params": {...}, "confidence": 0.0-1.0, "reasoning": "..." }

If no template matches, return { "template_id": "ping", "confidence": 0.0,
"reasoning": "no match" }.
```

The template registry is **rendered into the system prompt at runtime** so adding a new template only requires:

1. Add JSON to `query_engine/templates/query_templates.json`
2. No code change in the LLM module

## `insight_generator.py`

Takes the query result rows (already executed) plus the user's original intent and produces a one-paragraph summary.

```python
async def generate(rows: list[dict], intent: IntentResult) -> str:
    if not rows:
        return "No results found for this query."
    
    summary_input = {
        "template_id": intent.template_id,
        "params": intent.params,
        "row_count": len(rows),
        "sample_rows": rows[:5],     # don't blow the token budget
        "aggregates": _compute_aggregates(rows),
    }
    
    response = await client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": INSIGHT_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(summary_input)},
        ],
        temperature=0.3,
    )
    return response.choices[0].message.content
```

### Why only 5 sample rows?

To control token cost. We send aggregate stats (count, sum, min, max, avg) computed locally — these are deterministic and free. The LLM uses them + a few example rows.

## `title_generator.py`

Generates a short title (≤40 chars) from the first prompt of a session.

```python
async def generate(prompt: str) -> str:
    response = await client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Write a 4-6 word title for this analytics question. No quotes."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=20,
    )
    return response.choices[0].message.content.strip()
```

## Prompts as files (not strings)

All prompts are in `prompts/*.txt`, **not** Python string literals.

### Why?

- Easy to diff in PRs
- Non-engineers can review/edit
- Versioning is just git history
- Loaded once at startup; cached

```python
@lru_cache(maxsize=1)
def _load_prompt(name: str) -> str:
    return (Path(__file__).parent / "prompts" / f"{name}.txt").read_text()
```

## Adding a new LLM call

1. Create `prompts/<name>.txt`
2. Create `<name>_generator.py` with one async function
3. Use `get_llm_client()` and `_load_prompt(name)`
4. Validate response with a Pydantic model
5. Add tests in `app/tests/unit/test_<name>.py` using `respx` to mock OpenAI

## Cost & latency notes

| Call | Tokens (typical) | Cost (gpt-4o) | Latency |
|------|------------------|----------------|---------|
| Intent extraction | 800 in / 100 out | ~$0.005 | 500-1500ms |
| Insight generation | 2000 in / 200 out | ~$0.015 | 800-2000ms |
| Title generation | 100 in / 20 out | ~$0.001 | 300-600ms |

For high-volume use cases:
- Use `gpt-4o-mini` (10x cheaper) for intent/title; keep `gpt-4o` only for insight
- Cache intents by prompt hash in Redis (5 min TTL)
- Skip insight generation if `len(rows) < 3`

## Testing LLM code

Use `respx` to mock OpenAI HTTP calls:

```python
import respx
from openai import AsyncOpenAI

@respx.mock
async def test_intent_extractor():
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=Response(200, json={
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "template_id": "top_customers_by_revenue",
                        "params": {"limit": 10, "period_days": 30},
                        "confidence": 0.95,
                        "reasoning": "test",
                    })
                }
            }]
        })
    )
    result = await intent_extractor.extract("top customers")
    assert result.template_id == "top_customers_by_revenue"
```

Next → [Module: Query Engine](./04-module-query-engine.md)
