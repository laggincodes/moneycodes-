"""
Transparent Lead Scoring Engine.
Calculates a 0-100 score based on documented factors, evidence, and contactability.
Avoids presenting score as conversion probability; highlights fixable issues and outreach feasibility.
"""
from typing import List
from app.schemas import LeadScore, ScoreFactor, WebsiteAuditResult
from app.discovery.base import DiscoveredBusiness


class LeadScorer:
    """
    Computes a transparent 0-100 lead score.
    Breakdown explains the exact points awarded and the underlying evidence.
    """

    def calculate_score(
        self,
        business: DiscoveredBusiness,
        audit: WebsiteAuditResult,
    ) -> LeadScore:
        factors: List[ScoreFactor] = []
        total_score = 0

        # --- Factor 1: Digital Presence Opportunity (Max 35 points) ---
        if not audit.has_website_url:
            f1_pts = 35
            f1_reason = "No website found in public business listings."
            f1_evidence = "Zero listed website. Direct opportunity to build a modern starter or storefront website."
        elif not audit.is_reachable:
            f1_pts = 35
            f1_reason = "Existing website URL is broken, down, or returning errors."
            f1_evidence = f"Site failed to load (Status: {audit.status_code or 'Unreachable'}). Urgent fix needed."
        elif not audit.has_viewport_meta:
            f1_pts = 25
            f1_reason = "Existing website lacks mobile responsive viewport configuration."
            f1_evidence = "Desktop-only layout causes poor mobile experience for local smartphone searchers."
        elif not audit.ssl_valid:
            f1_pts = 15
            f1_reason = "Website lacks HTTPS / SSL encryption."
            f1_evidence = "Modern browsers show 'Not Secure' warnings to prospective customers."
        else:
            f1_pts = 5
            f1_reason = "Business has an active, accessible website."
            f1_evidence = "Standard web presence detected; potential redesign or feature upgrade candidate."

        total_score += f1_pts
        factors.append(ScoreFactor(
            name="Digital Presence Opportunity",
            points=f1_pts,
            max_points=35,
            reason=f1_reason,
            evidence=f1_evidence,
        ))

        # --- Factor 2: Direct Contact Channel Availability (Max 25 points) ---
        has_phone = bool(business.phone and len(business.phone) >= 5)
        has_email = audit.has_contact_email or audit.has_contact_phone

        if has_phone:
            f2_pts = 25
            f2_reason = "Verified public business telephone or WhatsApp contact available."
            f2_evidence = f"Public phone: {business.phone} (verified via OpenStreetMap listing)."
        elif has_email:
            f2_pts = 18
            f2_reason = "Online contact channel (tel/email) discovered during audit."
            f2_evidence = "Direct contact link identified on digital property."
        else:
            f2_pts = 0
            f2_reason = "No direct telephone or email identified in initial public listing."
            f2_evidence = "Cold outreach requires in-person visit or locating alternative contact channels."

        total_score += f2_pts
        factors.append(ScoreFactor(
            name="Actionable Contact Channel",
            points=f2_pts,
            max_points=25,
            reason=f2_reason,
            evidence=f2_evidence,
        ))

        # --- Factor 3: Missing Conversion & Customer Features (Max 20 points) ---
        f3_pts = 0
        missing_features = []
        if not audit.has_clear_cta:
            f3_pts += 10
            missing_features.append("no prominent call-to-action (Book/Order/Enquire)")
        if not audit.has_menu and any(c in business.category.lower() for c in ["caf", "restaurant", "food"]):
            f3_pts += 10
            missing_features.append("missing online food menu")
        elif not audit.has_social_links:
            f3_pts += 5
            missing_features.append("no social channel integration")

        f3_pts = min(20, f3_pts)
        if f3_pts > 0:
            f3_reason = f"Identified missing high-value conversion elements: {', '.join(missing_features)}."
            f3_evidence = f"Audit heuristics observed gaps in customer conversion path."
        else:
            f3_reason = "Standard conversion features appear present."
            f3_evidence = "Site contains calls-to-action and relevant feature anchors."

        total_score += f3_pts
        factors.append(ScoreFactor(
            name="Missing Customer Features",
            points=f3_pts,
            max_points=20,
            reason=f3_reason,
            evidence=f3_evidence,
        ))

        # --- Factor 4: Active Physical Business Evidence (Max 20 points) ---
        f4_pts = 0
        active_proofs = []
        if business.opening_hours:
            f4_pts += 10
            active_proofs.append(f"Operating hours logged: {business.opening_hours}")
        if business.address:
            f4_pts += 10
            active_proofs.append(f"Physical address: {business.address}")

        f4_pts = min(20, f4_pts)
        if f4_pts > 0:
            f4_reason = "Strong evidence of an active, operating physical business."
            f4_evidence = "; ".join(active_proofs)
        else:
            f4_reason = "Basic coordinates recorded, limited operational tags in public record."
            f4_evidence = "Missing data is treated neutrally (does not imply inactivity)."

        total_score += f4_pts
        factors.append(ScoreFactor(
            name="Active Business Verification",
            points=f4_pts,
            max_points=20,
            reason=f4_reason,
            evidence=f4_evidence,
        ))

        # Cap total score strictly between 0 and 100
        total_score = max(0, min(100, total_score))

        if total_score >= 70:
            priority = "High"
            explanation = "High priority: Verified operational business with clear, fixable digital gaps and a known contact method."
        elif total_score >= 45:
            priority = "Medium"
            explanation = "Medium priority: Viable prospect; moderate digital needs or requires secondary contact lookup."
        else:
            priority = "Low"
            explanation = "Lower priority: Business may already possess a modern site or lacks direct outreach details."

        return LeadScore(
            total_score=total_score,
            factors=factors,
            priority=priority,
            explanation=explanation,
        )
