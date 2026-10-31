"""Verify the configured model provider before you demo.

    python3 scripts/check_provider.py              # whatever .env says
    python3 scripts/check_provider.py anthropic    # force one

Run this FIRST after adding an API key. It does a real round-trip, then a real
one-scenario agent run, so a key problem shows up now and not on stage.
"""
from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "backend"))
sys.path.insert(0, _HERE)

for line in ("../.env", ".env"):
    p = os.path.join(_HERE, line)
    if os.path.isfile(p):
        for ln in open(p):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, _, v = ln.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

from app.agent.brain import probe                 # noqa: E402
from app.config import settings                   # noqa: E402
from run_scenario import run_scenario             # noqa: E402


def main() -> int:
    forced = sys.argv[1] if len(sys.argv) > 1 else None
    prov = forced or settings.resolved_provider()
    print(f"\nprovider : {prov}")
    if prov == "anthropic":
        print(f"model    : {settings.model_deep}")
        print(f"key      : {'set (' + settings.anthropic_api_key[:8] + '…)' if settings.anthropic_api_key else 'MISSING'}")
    elif prov in ("kimi", "moonshot"):
        print(f"model    : {settings.kimi_model}")
        print(f"endpoint : {settings.kimi_base_url}")
        print(f"key      : {'set (' + settings.kimi_api_key[:8] + '…)' if settings.kimi_api_key else 'MISSING'}")
    elif prov == "openai":
        print(f"model    : {settings.openai_model}")
        print(f"endpoint : {settings.openai_base_url}")
        print(f"key      : {'set' if settings.openai_api_key else 'MISSING'}")
    elif prov == "ollama":
        print(f"model    : {settings.ollama_model} @ {settings.ollama_host}")

    print("\n1/2  round-trip …")
    r = probe(prov)
    print(f"     {'OK' if r['ok'] else 'FAILED'}  {r['detail']}  ({r['latency_ms']} ms)")

    # Kimi has two regional hosts and a key is only valid on the one that
    # issued it. Try the other automatically rather than making you guess.
    if not r["ok"] and prov in ("kimi", "moonshot"):
        alt = ("https://api.moonshot.cn/v1"
               if "moonshot.ai" in settings.kimi_base_url
               else "https://api.moonshot.ai/v1")
        print(f"\n     retrying on the other regional endpoint: {alt}")
        settings.kimi_base_url = alt
        r = probe(prov)
        print(f"     {'OK' if r['ok'] else 'FAILED'}  {r['detail']}  ({r['latency_ms']} ms)")
        if r["ok"]:
            print(f"\n     >>> Add this line to your .env:")
            print(f"     RESTORIO_KIMI_BASE_URL={alt}\n")

    if not r["ok"]:
        print("\n  The provider is not reachable. Check the key and endpoint, or")
        print("  demo with the deterministic reasoner, which needs neither:")
        print("      RESTORIO_PROVIDER=offline ./scripts/start.sh\n")
        return 1

    print("\n2/2  full agent run on the scaling scenario …")
    settings.autonomy = "full"
    out = asyncio.run(run_scenario("scaling", provider=prov))
    status = out.get("status")
    ok = status == "recovered"
    print(f"     status            : {status}")
    print(f"     true pressure     : {out.get('final_true_pressure', 0):.2f} bar (target 4.00)")
    print(f"     tool calls        : {out.get('tool_calls')}")
    print(f"     model calls       : {(out.get('usage') or {}).get('llm_calls')}")
    print(f"     tokens            : {(out.get('usage') or {}).get('input_tokens',0) + (out.get('usage') or {}).get('output_tokens',0)}")
    print(f"     diagnosis         : {(out.get('diagnosis') or '')[:160]}")
    print(f"\n     {'READY TO DEMO' if ok else 'NOT RECOVERED — check the incident timeline above'}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
