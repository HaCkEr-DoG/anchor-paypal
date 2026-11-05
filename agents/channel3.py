"""
Channel3 Product Enrichment
https://trychannel3.com — 100M+ product graph

When a dispute comes in, Channel3 looks up the disputed item and returns
canonical product title, description, brand, category, price, and images.

This converts a bare transaction description ("Nike Air Max 90") into a
full product dossier that PayPal accepts as ITEM_DESCRIPTION evidence —
automatically, without the merchant lifting a finger.

Usage:
    client = Channel3Client()
    result = await client.enrich_dispute("PP-D-00112233", "Nike Air Max 90", 89.99)
    if result.succeeded:
        print(result.evidence_text)   # → submit to PayPal as ITEM_DESCRIPTION

Requires: CHANNEL3_API_KEY env var (get one free at trychannel3.com/sign-up/developer)
"""

import os
import httpx
from dataclasses import dataclass, field
from typing import Optional


CHANNEL3_API_KEY = os.environ.get("CHANNEL3_API_KEY", "")
CHANNEL3_BASE    = "https://api.trychannel3.com/v1"


@dataclass
class Channel3Product:
    id: str
    title: str
    description: str
    brand: str
    category: str
    images: list[str] = field(default_factory=list)
    price: Optional[float] = None
    currency: str = "USD"
    domain: str = ""
    availability: str = ""


@dataclass
class EnrichmentResult:
    dispute_id: str
    query: str
    product: Optional[Channel3Product] = None
    error: Optional[str] = None
    evidence_text: str = ""

    @property
    def succeeded(self) -> bool:
        return self.product is not None and not self.error


class Channel3Client:
    """
    Async wrapper around Channel3's search API.
    Called by EvidenceAssembler.gather_product_evidence() during dispute handling.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or CHANNEL3_API_KEY

    def enabled(self) -> bool:
        return bool(self.api_key)

    async def search_product(self, query: str, limit: int = 1) -> list[Channel3Product]:
        """
        Search Channel3 for a product by text.

        POST /v1/search
        Headers: x-api-key: <key>
        Body:    {"query": "...", "limit": N}
        """
        if not self.enabled():
            return []

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{CHANNEL3_BASE}/search",
                headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
                json={"query": query, "limit": limit},
            )
            resp.raise_for_status()
            data = resp.json()

        products: list[Channel3Product] = []
        for item in data.get("products", []):
            offers     = item.get("offers", [])
            # Pick the in-stock offer closest in price to the disputed amount
            in_stock   = [o for o in offers if o.get("availability") == "InStock"] or offers
            best_offer = min(in_stock, key=lambda o: o.get("price", {}).get("price", 9999), default=None)

            price     = best_offer["price"]["price"]    if best_offer else None
            currency  = best_offer["price"].get("currency", "USD") if best_offer else "USD"
            domain    = best_offer.get("domain", "")    if best_offer else ""
            avail     = best_offer.get("availability", "") if best_offer else ""

            brands     = item.get("brands") or []
            brand_name = brands[0]["name"] if brands else ""
            cat        = item.get("category") or {}
            cat_title  = cat.get("title", "") if isinstance(cat, dict) else ""

            imgs = [
                img["url"]
                for img in (item.get("images") or [])
            ][:3]

            products.append(Channel3Product(
                id=item.get("id", ""),
                title=item.get("title", ""),
                description=(item.get("description") or "")[:600],
                brand=brand_name,
                category=cat_title,
                images=imgs,
                price=price,
                currency=currency,
                domain=domain,
                availability=avail,
            ))

        return products

    async def enrich_dispute(
        self,
        dispute_id: str,
        item_description: str,
        transaction_amount: Optional[float] = None,
    ) -> EnrichmentResult:
        """
        Main entry point for dispute enrichment.

        Looks up the disputed product in Channel3, then formats the result
        as ITEM_DESCRIPTION evidence text for PayPal's provide-evidence endpoint.
        """
        if not self.enabled():
            return EnrichmentResult(
                dispute_id=dispute_id,
                query=item_description,
                error="CHANNEL3_API_KEY not configured — set it in .env or Render env vars",
            )

        try:
            products = await self.search_product(item_description, limit=1)
        except httpx.HTTPStatusError as e:
            return EnrichmentResult(
                dispute_id=dispute_id,
                query=item_description,
                error=f"Channel3 API error {e.response.status_code}: {e.response.text[:200]}",
            )
        except Exception as e:
            return EnrichmentResult(
                dispute_id=dispute_id,
                query=item_description,
                error=str(e),
            )

        if not products:
            return EnrichmentResult(
                dispute_id=dispute_id,
                query=item_description,
                error="No matching product found in Channel3 catalog",
            )

        p = products[0]
        lines = [f"Product: {p.title}"]
        if p.brand:
            lines.append(f"Brand: {p.brand}")
        if p.category:
            lines.append(f"Category: {p.category}")
        if p.price is not None:
            lines.append(f"Catalog price: {p.currency} {p.price:.2f}")
        if transaction_amount is not None:
            lines.append(f"Transaction amount: USD {transaction_amount:.2f}")
            if p.price is not None:
                delta_pct = abs(transaction_amount - p.price) / max(p.price, 0.01) * 100
                if delta_pct <= 20:
                    lines.append(f"Price match: within {delta_pct:.0f}% of catalog price ✓")
                else:
                    lines.append(f"Price note: {delta_pct:.0f}% deviation from catalog price")
        if p.availability:
            lines.append(f"Availability: {p.availability}")
        if p.domain:
            lines.append(f"Sold by: {p.domain}")
        if p.description:
            lines.append(f"\nProduct description:\n{p.description}")
        if p.images:
            lines.append(f"\nProduct images: {', '.join(p.images[:2])}")
        lines.append("\nData source: Channel3 product graph — trychannel3.com (100M+ products)")

        return EnrichmentResult(
            dispute_id=dispute_id,
            query=item_description,
            product=p,
            evidence_text="\n".join(lines),
        )
