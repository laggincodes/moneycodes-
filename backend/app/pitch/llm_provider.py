"""
Optional LLM Provider for Pitch Generation using Gemini API (via google-genai SDK).
Falls back cleanly to the deterministic template engine when API key is unset or on error.
"""
import os
import json
import logging
from typing import Optional
from app.discovery.base import DiscoveredBusiness
from app.schemas import WebsiteAuditResult, LeadScore, SalesPitches
from app.pitch.template_engine import DeterministicPitchEngine

logger = logging.getLogger(__name__)


class SalesPitchGenerator:
    """
    Coordinates pitch generation: tries Gemini if GEMINI_API_KEY is configured;
    always falls back seamlessly to DeterministicPitchEngine.
    """

    def __init__(self, fallback_engine: Optional[DeterministicPitchEngine] = None):
        self.fallback = fallback_engine or DeterministicPitchEngine()
        self.api_key = os.getenv("GEMINI_API_KEY", "").strip()

    async def generate(
        self,
        business: DiscoveredBusiness,
        audit: WebsiteAuditResult,
        score: LeadScore,
    ) -> SalesPitches:
        # Default baseline pitch
        baseline = self.fallback.generate_pitch(business, audit, score)

        if not self.api_key:
            return baseline

        # Attempt Gemini LLM enhancement if key exists
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self.api_key)
            prompt = f"""
You are an expert freelance web developer consultant.
Create a personalized, honest sales outreach package for the following local business.
CRITICAL RULES:
- Use ONLY the provided verified facts.
- Do NOT invent revenue, customer statistics, owner names, or competitor claims.
- The tone must be respectful, helpful, and concise.

BUSINESS DETAILS:
Name: {business.name}
Category: {business.category}
Address: {business.address or 'Local area'}
Phone: {business.phone or 'None'}
Audit Summary: {baseline.audit_findings_summary}
Score: {score.total_score}/100 ({score.priority} Priority)
Scope: {baseline.suggested_project_scope}
Price Benchmark: {baseline.suggested_price_inr}

OUTPUT JSON FORMAT ONLY:
{{
  "email_subject": "...",
  "email_draft": "...",
  "whatsapp_draft": "..." or null,
  "follow_up_draft": "...",
  "why_worth_contacting": "..."
}}
"""
            response = await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.3,
                ),
            )

            if response.text:
                parsed = json.loads(response.text)
                return SalesPitches(
                    business_summary=baseline.business_summary,
                    verified_contacts=baseline.verified_contacts,
                    audit_findings_summary=baseline.audit_findings_summary,
                    recommended_features=baseline.recommended_features,
                    suggested_project_scope=baseline.suggested_project_scope,
                    suggested_price_inr=baseline.suggested_price_inr,
                    email_subject=parsed.get("email_subject", baseline.email_subject),
                    email_draft=parsed.get("email_draft", baseline.email_draft),
                    whatsapp_draft=parsed.get("whatsapp_draft", baseline.whatsapp_draft) if business.phone else None,
                    follow_up_draft=parsed.get("follow_up_draft", baseline.follow_up_draft),
                    why_worth_contacting=parsed.get("why_worth_contacting", baseline.why_worth_contacting),
                    generation_source="gemini_llm",
                )
        except Exception as exc:
            logger.warning(f"LLM pitch generation failed or timed out: {exc}. Using deterministic fallback.")

        return baseline
