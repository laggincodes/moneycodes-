"""
Deterministic Sales Pitch and Personalized Package Generator.
Generates tailored, factually grounded outreach drafts and project recommendations
based entirely on verified business evidence. Never invents facts or claims.
"""
from typing import Dict, List, Optional
from app.discovery.base import DiscoveredBusiness
from app.schemas import WebsiteAuditResult, LeadScore, SalesPitches


CATEGORY_FEATURES_MAP = {
    "café": [
        "Mobile-friendly digital food & beverage menu (fast-loading, easily updated)",
        "One-click WhatsApp table reservation button",
        "Google Business Profile & Apple Maps direct navigation link",
        "Curated photo gallery of ambiance and popular menu items",
    ],
    "restaurant": [
        "Interactive digital menu with dish categories and dietary tags",
        "Direct WhatsApp / Call table booking integration",
        "Customer reviews and culinary highlights section",
        "Operating hours & location map for foot-traffic guidance",
    ],
    "salon": [
        "Service treatment menu with transparent pricing options",
        "Quick appointment inquiry button directly into WhatsApp",
        "Visual portfolio of styling and beauty transformations",
        "Operating hours and stylist availability details",
    ],
    "gym": [
        "Weekly group class schedule and timing table",
        "Membership tiers and seasonal pass information",
        "Free 1-day trial session registration form",
        "Trainer profiles and equipment highlight gallery",
    ],
    "clinic": [
        "Doctor profiles, qualifications, and consultation hours",
        "Patient appointment booking request form",
        "Key specialties and treatment procedures overview",
        "Clear clinic location map, parking guidance, and emergency contact",
    ],
    "shop": [
        "Product catalog with seasonal collections and top-sellers",
        "Direct 'Inquire on WhatsApp' button on product cards",
        "Store opening hours, address, and in-store pickup details",
        "Customer testimonials and authentic product photography",
    ],
    "coaching": [
        "Course curricula, batches, and examination schedules",
        "Student enrollment and demo class inquiry form",
        "Faculty credentials and past student achievement highlights",
        "Downloadable course brochure and fee structure inquiry",
    ],
}

DEFAULT_FEATURES = [
    "Clean, mobile-first responsive design optimized for smartphones",
    "Prominent Call-to-Action buttons (Call Now, WhatsApp, Directions)",
    "Local SEO fundamentals for Google Search visibility",
    "Fast, secure hosting with HTTPS SSL encryption",
]


class DeterministicPitchEngine:
    """
    Generates tailored outreach packages deterministically without requiring an LLM API key.
    """

    def generate_pitch(
        self,
        business: DiscoveredBusiness,
        audit: WebsiteAuditResult,
        score: LeadScore,
    ) -> SalesPitches:
        cat_lower = business.category.lower()
        
        # Select category-specific recommended features
        recommended_features = []
        for key, features in CATEGORY_FEATURES_MAP.items():
            if key in cat_lower:
                recommended_features = features
                break
        if not recommended_features:
            recommended_features = DEFAULT_FEATURES

        # Determine project scope and suggested price range
        if not audit.has_website_url:
            scope_summary = "Starter Web Presence: 4-5 page responsive website, mobile-first design, Google Maps embed, WhatsApp chat button, and basic local SEO."
            suggested_price = "₹18,000 – ₹32,000 (Recommended freelance estimate — adjust based on client requirements)"
            audit_findings_summary = "No official website was found in public listings. Customers searching on Google can only find basic directory listings without a dedicated booking/inquiry portal."
            why_worth = f"{business.name} has a physical presence in {business.address or 'the local area'} but lacks a dedicated website. Building a simple, fast website gives them an immediate edge over competitors who only rely on third-party aggregators."
        elif not audit.is_reachable:
            scope_summary = "Website Recovery & Modernization: Fix domain/DNS issues, restore broken pages on clean modern architecture, and configure fast secure hosting."
            suggested_price = "₹14,000 – ₹26,000 (Recommended freelance estimate — adjust based on client requirements)"
            audit_findings_summary = f"Existing website ({audit.url}) is unreachable or returning errors (HTTP {audit.status_code or 'Down'})."
            why_worth = f"{business.name}'s current website is failing to load. This represents an active leak in their marketing and customer trust that needs prompt professional repair."
        elif not audit.has_viewport_meta:
            scope_summary = "Mobile Optimization Overhaul: Convert legacy layout to a fully responsive, mobile-first layout with click-to-call CTAs."
            suggested_price = "₹16,000 – ₹28,000 (Recommended freelance estimate — adjust based on client requirements)"
            audit_findings_summary = "Website is live but lacks modern mobile viewport configuration, meaning mobile visitors have to pinch and zoom."
            why_worth = f"More than 70% of local searches happen on mobile devices. {business.name} already has an online presence, so upgrading it to a smooth mobile experience offers immediate value."
        else:
            scope_summary = "Feature Upgrade & Conversion Tuning: Integrate WhatsApp quick-inquiry, improve page speed, and add a clear conversion CTA flow."
            suggested_price = "₹12,000 – ₹22,000 (Recommended freelance estimate — adjust based on client requirements)"
            audit_findings_summary = "Website is operational. Identified opportunities to enhance conversion actions and customer engagement."
            why_worth = f"{business.name} is an active local business where a conversion-focused redesign or feature addition can improve customer inquiries."

        # Verified contact info summary
        verified_contacts = {
            "phone": business.phone,
            "website": audit.url or business.website,
            "address": business.address,
            "source_listing": business.source_url,
        }

        # Business summary
        summary_lines = [
            f"{business.name} is a {business.category} located at {business.address or 'the specified local coordinates'}.",
        ]
        if business.opening_hours:
            summary_lines.append(f"Standard operating hours: {business.opening_hours}.")
        business_summary = " ".join(summary_lines)

        # First contact email draft
        email_subject = f"Website question for {business.name}"
        if not audit.has_website_url:
            email_body = (
                f"Hi team {business.name},\n\n"
                f"I came across {business.name} while looking at local {business.category.lower()} businesses in {business.address or 'your area'}.\n\n"
                f"I noticed that you don't currently have an official website linked where customers can view your {recommended_features[0].lower()} or contact you directly.\n\n"
                f"I'm an independent web developer and I help local businesses set up fast, clean, mobile-friendly websites that make it easy for nearby customers to find you, view your offerings, and reach out via WhatsApp or phone.\n\n"
                f"I've put together a few simple suggestions for what a starter site for {business.name} could look like:\n"
                f"• {recommended_features[0]}\n"
                f"• {recommended_features[1]}\n"
                f"• Google Maps navigation and direct click-to-call buttons\n\n"
                f"Would you be open to a quick 5-minute chat or WhatsApp exchange this week to see if this would be helpful for your business?\n\n"
                f"Best regards,\n"
                f"[Your Name]\n"
                f"[Your Phone / Portfolio Link]"
            )
        else:
            email_body = (
                f"Hi team {business.name},\n\n"
                f"I recently visited your website ({audit.url}) while reviewing local {business.category.lower()} businesses in {business.address or 'your area'}.\n\n"
                f"I wanted to share a quick observation that might be helpful: {audit_findings_summary}\n\n"
                f"As an independent web developer, I work with local businesses to ensure their web presence actively brings in new customers. With a few focused updates—such as {recommended_features[0].lower()} and clear mobile contact actions—you could significantly improve your visitor experience.\n\n"
                f"Would you be open to a quick 5-minute chat this week? I'd be happy to share a couple of specific suggestions with no obligation.\n\n"
                f"Best regards,\n"
                f"[Your Name]\n"
                f"[Your Phone / Portfolio Link]"
            )

        # WhatsApp draft
        whatsapp_draft = None
        if business.phone:
            if not audit.has_website_url:
                whatsapp_draft = (
                    f"Hello! I hope you're having a good day. "
                    f"I came across {business.name} while looking for {business.category.lower()} options in {business.address or 'the area'}. "
                    f"I noticed you don't currently have a dedicated website for customers to check your details or book easily. "
                    f"I'm a freelance web developer helping local businesses build fast, simple, mobile-friendly sites with WhatsApp booking. "
                    f"Would you be interested in seeing a quick concept for {business.name}?"
                )
            else:
                whatsapp_draft = (
                    f"Hello! I hope you're having a good week. "
                    f"I was checking out {business.name}'s website ({audit.url}) and noticed a quick fix that could help you get more inquiries from mobile visitors: {audit_findings_summary}. "
                    f"I'm a local freelance web developer—would you be open to a 2-minute chat about a simple solution?"
                )

        # Follow-up email draft
        follow_up_draft = (
            f"Hi team {business.name},\n\n"
            f"Just following up briefly on my previous note regarding {business.name}'s online presence. "
            f"I know how busy running a {business.category.lower()} can be.\n\n"
            f"If you'd like a quick look at the recommendations I outlined, please feel free to drop me a quick reply or message here whenever convenient.\n\n"
            f"Best regards,\n"
            f"[Your Name]"
        )

        return SalesPitches(
            business_summary=business_summary,
            verified_contacts=verified_contacts,
            audit_findings_summary=audit_findings_summary,
            recommended_features=recommended_features,
            suggested_project_scope=scope_summary,
            suggested_price_inr=suggested_price,
            email_subject=email_subject,
            email_draft=email_body,
            whatsapp_draft=whatsapp_draft,
            follow_up_draft=follow_up_draft,
            why_worth_contacting=why_worth,
            generation_source="deterministic_template",
        )
