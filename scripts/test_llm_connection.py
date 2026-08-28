"""
Standalone connection test for the real Gemini API.

Run this BEFORE running the full pipeline with --llm. It makes exactly one
real API call, using the exact same GeminiClient and AdjudicationResponse
schema the actual pipeline uses — so if this script works, the pipeline's
LLM integration will too. If it fails, you'll get a clear error here rather
than a confusing failure buried inside a 962-record matching run.

Usage:
    python scripts/test_llm_connection.py

Requires GEMINI_API_KEY to be set as an environment variable — see
README.md for how to set it. Never hardcode the key here or anywhere else.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make `recon` importable when running this script directly, without
# requiring `pip install -e .` to have been run first.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recon.match.p5_llm import AdjudicationResponse, GeminiClient  # noqa: E402


def main() -> None:
    print("Testing Gemini API connection...")
    print()

    try:
        client = GeminiClient()
    except RuntimeError as e:
        print(f"FAILED before making any API call: {e}")
        sys.exit(1)

    # A deliberately trivial, unambiguous test prompt — the point here is
    # only to confirm the API call itself works and returns something that
    # validates against our real schema, not to test adjudication logic
    # (that's covered by tests/test_p5_llm.py against a fake client).
    test_prompt = """You are adjudicating ONE unresolved bank credit row against a small
set of candidate settlement batches.

UNRESOLVED BANK ROW
  bank_txn_id: BNK-TEST
  date: 2026-04-03
  net amount: 50000 paise
  narration: '1568176960vxp0rj RAZORPAY SETTLEMENT'
  ref_no: '1568176960vxp0rj'

CANDIDATE SETTLEMENTS
[0]   settlement_id: setl_TEST_A
  amount: 50000 paise
  settle_date: 2026-04-03
  fees: 1000 paise, tax: 180 paise
  utr (as recorded internally, NOT necessarily visible in the bank narration): 1568176960vxp0rj

INSTRUCTIONS
- If exactly one candidate is clearly the right match, return "match" with
  its settlement_id as candidate_id.
- If you are not genuinely confident, return "escalate".
- If you're confident NONE of the candidates match, return "no_match".
- candidate_id must be exactly one of: "setl_TEST_A" or null.

Return JSON matching the required schema only."""

    print("Sending one real request to Gemini...")
    try:
        raw_response = client.generate(test_prompt)
    except Exception as e:
        print(f"FAILED — the API call itself raised an error: {e}")
        print()
        print("Common causes:")
        print("  - GEMINI_API_KEY is set but invalid or revoked")
        print("  - No network access from this machine/network")
        print("  - The 'google-genai' package isn't installed "
              "(pip install -e \".[dev]\")")
        sys.exit(1)

    print("Raw response received:")
    print(raw_response)
    print()

    try:
        parsed = AdjudicationResponse.model_validate_json(raw_response)
    except Exception as e:
        print(f"WARNING — response received, but didn't validate against our schema: {e}")
        print("This would be treated as 'schema_invalid' by the real pipeline "
              "(an automatic escalation, not a crash) — but worth investigating "
              "if you see this consistently.")
        sys.exit(1)

    print("Parsed and validated successfully:")
    print(f"  decision:    {parsed.decision}")
    print(f"  candidate_id:{parsed.candidate_id}")
    print(f"  confidence:  {parsed.confidence}")
    print(f"  reasoning:   {parsed.reasoning}")
    print()

    if parsed.decision.value == "match" and parsed.candidate_id == "setl_TEST_A":
        print("SUCCESS — the exact expected answer for this trivial test case. "
              "Your Gemini connection is working correctly.")
    else:
        print("The connection works (you got a valid, schema-conforming response), "
              "but the model didn't give the expected answer for this trivial "
              "test case. Worth a second look before trusting it on real data.")


if __name__ == "__main__":
    main()
