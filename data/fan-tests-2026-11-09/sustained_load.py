"""Back-to-back 300-token generations for DURATION seconds; print server-side decode t/s.

The llama-server base URL comes from LLM_URL (e.g. http://<t4-address>:8080/v1): from the
environment, or else from the repo's untracked .env file (template: .env.example).
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path


def llm_url():
    if os.environ.get("LLM_URL"):
        return os.environ["LLM_URL"]
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "LLM_URL":
                return value.strip().strip("\"'")
    sys.exit("LLM_URL not set: export it, or add it to the repo's .env (see .env.example)")


URL = llm_url().rstrip("/") + "/chat/completions"
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 180

start = time.time()
i = 0
while time.time() - start < DURATION:
    i += 1
    body = json.dumps({
        "model": "qwen3.8-27b-iq3",
        "messages": [{"role": "user", "content": f"Request {i}: write a 250-word description of a typical day for a house painter."}],
        "max_tokens": 300,
        "temperature": 0.7,
    }).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        t = json.load(r)["timings"]
    print("%s  t+%5.0fs  req %2d  decode %3d tok @ %5.2f t/s" % (
        time.strftime("%H:%M:%S"), time.time() - start, i, t["predicted_n"], t["predicted_per_second"]), flush=True)
