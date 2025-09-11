#!/usr/bin/env python3
# (content identical to previous cell; re-writing due to state reset)
import os, re, time, unicodedata, hashlib
from collections import deque, defaultdict
from typing import Optional, Dict
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

API_KEYS = {k.strip() for k in os.getenv("GATEWAY_API_KEYS", "demo-key-123").split(",") if k.strip()}
RATE_LIMIT_BURST = 12
RATE_LIMIT_REFILL = 6
DAILY_QUOTA = int(os.getenv("GATEWAY_DAILY_QUOTA", "500"))
MAX_PROMPT_LEN = 5000
APPROVED_PREFIXES = ("Task:", "Question:", "User:", "Input:", "Query:")
TRIGGER = r"\+\+OVERRIDE_POLICY\+\+"
DISALLOWED_PATTERNS = [
    TRIGGER,
    r"ignore\s+previous\s+instructions",
    r"disregard\s+safety",
    r"leak\s+the\s*api\s*key",
    r"reveal\s+credentials?",
    r"exfiltrat(e|ion)\s+data",
    r"disable\s+(logging|monitoring)",
    r"write\s+malware",
]
SECRET_RX = re.compile(r'(api[_-]?key|secret|password)\s*[:=]\s*[A-Za-z0-9_\-]{8,}', re.I)
RECENT_HASHES = deque(maxlen=4096)
ip_buckets: Dict[str, Dict[str, float]] = {}
key_buckets: Dict[str, Dict[str, float]] = {}
key_daily_count: Dict[str, int] = defaultdict(int)
_last_quota_reset = time.time()

app = FastAPI(title="BSides Prompt Gateway v2", version="2.0")

class Query(BaseModel):
    prompt: str
    max_new_tokens: Optional[int] = 128

def normalize_text(txt: str) -> str:
    txt = unicodedata.normalize("NFKC", txt or "")
    return "".join(ch for ch in txt if ch.isprintable())

def enforce_template(txt: str) -> bool:
    return txt.strip().startswith(APPROVED_PREFIXES)

RULES = [re.compile(pat, re.IGNORECASE) for pat in DISALLOWED_PATTERNS]

def sanitize_input(user_text: str) -> Optional[str]:
    if not user_text or not user_text.strip():
        return "Empty prompt."
    if len(user_text) > MAX_PROMPT_LEN:
        return f"Prompt too long (>{MAX_PROMPT_LEN} chars)."
    for rx in RULES:
        if rx.search(user_text):
            return f"Blocked by rule: /{rx.pattern}/"
    return None

def near_duplicate(txt: str) -> bool:
    h = hashlib.blake2b(txt.strip().lower().encode(), digest_size=16).hexdigest()
    if h in RECENT_HASHES:
        return True
    RECENT_HASHES.append(h)
    return False

def token_bucket_allow(bucket: Dict[str, float], burst: int, refill_per_minute: float) -> bool:
    now = time.time()
    tokens = bucket.get("tokens", burst)
    ts = bucket.get("ts", now)
    elapsed = now - ts
    tokens = min(burst, tokens + (elapsed / 60.0) * refill_per_minute)
    if tokens >= 1.0:
        tokens -= 1.0
        bucket["tokens"] = tokens
        bucket["ts"] = now
        return True
    bucket["tokens"] = tokens
    bucket["ts"] = now
    return False

def require_api_key(req: Request) -> str:
    api_key = req.headers.get("X-API-Key", "").strip()
    if not api_key or api_key not in API_KEYS:
        raise HTTPException(status_code=401, detail="Missing or invalid API key.")
    return api_key

def reset_daily_quotas_if_needed():
    global _last_quota_reset
    now = time.time()
    if now - _last_quota_reset > 24 * 3600:
        key_daily_count.clear()
        _last_quota_reset = now

def enforce_quotas(api_key: str):
    reset_daily_quotas_if_needed()
    if key_daily_count[api_key] >= DAILY_QUOTA:
        raise HTTPException(status_code=429, detail="Daily quota exceeded for this API key.")
    key_daily_count[api_key] += 1

def redact_secrets(text: str) -> str:
    return SECRET_RX.sub("[REDACTED_SECRET]", text or "")

_tokenizer = None
_model = None

def _maybe_load_model():
    global _tokenizer, _model
    if _tokenizer is not None and _model is not None:
        return True
    try:
        from transformers import AutoTokenizer, AutoModelForCausalLM
        _tokenizer = AutoTokenizer.from_pretrained("distilgpt2")
        _model = AutoModelForCausalLM.from_pretrained("distilgpt2")
        return True
    except Exception:
        return False

def generate_text(prompt: str, max_new_tokens: int = 128) -> str:
    if _maybe_load_model():
        import torch
        inputs = _tokenizer(prompt, return_tensors="pt")
        with torch.no_grad():
            out = _model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=True,
                top_p=0.95,
                temperature=0.8,
                pad_token_id=_tokenizer.eos_token_id,
            )
        from transformers import set_seed
        return _tokenizer.decode(out[0], skip_special_tokens=True)
    return f"{prompt}\n\n[Mock LLM]: This response was moderated and generated safely."

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/query")
async def query(req: Request, q: Query):
    api_key = require_api_key(req)

    ip = req.client.host if req.client else "unknown"
    ip_bucket = ip_buckets.setdefault(ip, {})
    if not token_bucket_allow(ip_bucket, RATE_LIMIT_BURST, RATE_LIMIT_REFILL):
        raise HTTPException(status_code=429, detail="IP rate limit exceeded.")

    key_bucket = key_buckets.setdefault(api_key, {})
    if not token_bucket_allow(key_bucket, RATE_LIMIT_BURST, RATE_LIMIT_REFILL):
        raise HTTPException(status_code=429, detail="API key rate limit exceeded.")

    enforce_quotas(api_key)

    prompt = normalize_text(q.prompt)

    if not enforce_template(prompt):
        raise HTTPException(status_code=400, detail="Prompt must start with an approved prefix (Task:/Question:/User:/Input:/Query:).")

    if near_duplicate(prompt):
        raise HTTPException(status_code=429, detail="Duplicate/near-duplicate prompt throttled.")

    reason = sanitize_input(prompt)
    if reason is not None:
        raise HTTPException(status_code=400, detail=f"Blocked: {reason}")

    print(f"[ALLOW] ip={ip} key={api_key[:4]}*** prompt={prompt[:120].replace('\\n', ' ')}")
    text = generate_text(prompt.strip(), max_new_tokens=q.max_new_tokens or 128)
    text = redact_secrets(text)
    return {"result": text}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
