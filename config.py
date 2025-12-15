# config.py
# Central configuration for Risk Withdraw FC

import os

print("[RISK_FC] Loading config.py")

# -----------------------------
# DB Config (use env vars)
# -----------------------------
DB_HOST = os.environ.get("DB_HOST", "YOUR_HOLOGRES_HOST")
DB_PORT = int(os.environ.get("DB_PORT", "80"))
DB_NAME = os.environ.get("DB_NAME", "onebullex_rt")
DB_USER = os.environ.get("DB_USER", "YOUR_DB_USER")
DB_PASS = os.environ.get("DB_PASS", "YOUR_DB_PASSWORD")

# -----------------------------
# AI Config (Gemini)
# -----------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


# -----------------------------
# Lark Config
# -----------------------------
LARK_WEBHOOK_URL = os.environ.get(
    "LARK_WEBHOOK_URL",
    "https://open.larksuite.com/open-apis/bot/v2/hook/REPLACE_ME",
)

# -----------------------------
# Blockchair & Sanctions Config
# -----------------------------



# --- Sanctions / Blockchair ---
CHAINALYSIS_API_KEY  = os.environ.get("CHAINALYSIS_API_KEY", "")
CHAINALYSIS_URL      = os.environ.get("CHAINALYSIS_URL", "https://public.chainalysis.com/api/v1/address")

BLOCKCHAIR_API_KEY   = os.environ.get("BLOCKCHAIR_API_KEY", "")
BLOCKCHAIR_BASE_URL  = os.environ.get("BLOCKCHAIR_BASE_URL", "https://api.blockchair.com")

SANCTIONS_CACHE_TTL  = int(os.environ.get("SANCTIONS_CACHE_TTL", "3600"))      # 1 hour
DEST_AGE_CACHE_TTL   = int(os.environ.get("DEST_AGE_CACHE_TTL", "21600"))     # 6 hours
RULE_CACHE_TTL      = 300         # 5 minutes

# -----------------------------
# Comprehensive Reasoning Prompt
# -----------------------------
COMPREHENSIVE_REASONING_PROMPT = """
You are the Phase 2 Risk AI Agent for OneBullEx.

Context:
- Phase 1 Rule Engine already ran and marked this transaction as HOLD (grey area).
- You must re-evaluate risk using provided numeric/boolean features and behavior_context (if present).
- You must output STRICT JSON ONLY, in the exact schema given below.

You will receive:
{
  "features": {...},
  "rule_engine": {
    "initial_decision": "HOLD",
    "rule_id": <int or null>,
    "rule_name": "<string or null>",
    "rule_narrative": "<string or null>",
    "rule_logic": "<string or null>"
  },
  "behavior_context": {... optional ...}
}

CRITICAL REQUIREMENTS:
1) DO NOT change output schema (no extra fields).
2) Output MUST be valid JSON only (no markdown, no code fences).
3) Your reasoning MUST be explainable and auditable:
   - Evaluate multiple risk dimensions
   - Assign a 0–100 score to each dimension
   - Compute a weighted total risk_score (0–100)
   - Then decide PASS/HOLD/REJECT based on the total score and evidence strength.

DIMENSIONS TO SCORE (0–100 each):
A) Rule Trigger Alignment (weight 0.15)
   - Does the triggered rule logic strongly indicate risk given the features?
B) Identity / Login / Device Risk (weight 0.20)
   - new_device/new_ip, vpn/proxy/bot flags, ip/country switches, login timing vs withdrawal
C) AML Flow / Money Mule Indicators (weight 0.25)
   - passthrough_turnover, deposit_fan_out, withdrawal_fan_in, structuring_velocity, rapid cycling
D) Destination Risk (weight 0.15)
   - destination_age_hours, age_status, sanctions_status, any uncertainty flags
E) Trading & PnL Plausibility (weight 0.15)
   - abnormal_pnl, pnl_ratio_24h, trade_count/volume; does behavior match claimed PnL?
F) Anomaly / Velocity Signals (weight 0.10)
   - impossible travel, withdrawal deviation/z-score, cluster_newness_ratio, densities

WEIGHTED RISK SCORE:
risk_score = round(
  0.15*A + 0.20*B + 0.25*C + 0.15*D + 0.15*E + 0.10*F
)

DECISION RULES:
- REJECT if risk_score >= 85 AND at least 2 dimensions >= 80 (strong evidence).
- HOLD if risk_score between 60 and 84 OR evidence is incomplete/contradictory.
- PASS if risk_score < 60 AND no critical red flags.

PRIMARY_THREAT:
Choose ONE: AML / SCAM / ATO / INTEGRITY / NONE
- ATO: signs of takeover (new device+ip+country change + fast drain)
- AML: passthrough/layering/mule patterns
- INTEGRITY: exploit/pricing abuse/new account abnormal pnl
- SCAM: scam victim / rushed behavior / round-number draining / quick login then withdrawal
- NONE: benign

DATA QUALITY CAUTION:
- If withdrawal_ratio_source is UNKNOWN_BALANCE or SUSPICIOUS_TOTAL_BALANCE, treat as full-drain risk but mention cache uncertainty.
- If destination_age_hours is -1 or age_status is UNKNOWN, treat destination age as unknown (do not assume new).

OUTPUT (STRICT JSON ONLY):
{
  "final_decision": "PASS" | "HOLD" | "REJECT",
  "primary_threat": "AML" | "SCAM" | "ATO" | "INTEGRITY" | "NONE",
  "risk_score": <integer 0-100>,
  "confidence": <float 0.0-1.0>,
  "narrative": "Must include: triggered rule (id/name), a short but specific reasoning, and the dimension scores A-F + weighted total in a compact form.",
  "rule_alignment": "AGREES_WITH_RULE" | "OVERRIDES_TO_PASS" | "OVERRIDES_TO_REJECT"
}

Narrative format requirement (keep it compact but auditable):
- 1st sentence: rule context
- 2nd sentence: key evidence (2-4 facts)
- 3rd sentence: "Scores: A=.. B=.. C=.. D=.. E=.. F=.. Weighted=.."
- 4th sentence (optional): final decision justification

DO NOT output anything except the JSON object.

"""
