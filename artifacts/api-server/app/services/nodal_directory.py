"""Seeded merchant directory; only addresses marked verified are eligible for dispatch."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain import NodalDirectory

# Sources and verification are deliberately conservative. No speculative address
# is used for outbound email; unverified/unknown organizations remain addressless.
MERCHANTS = [
    ("amazon", "Amazon India", "E-commerce", "grievance-officer@amazon.in", True, "https://www.amazon.in/", None),
    ("flipkart", "Flipkart", "E-commerce", "grievance.officer@flipkart.com", True, "https://www.flipkart.com/", None),
    ("myntra", "Myntra", "E-commerce", None, False, None, None),
    ("meesho", "Meesho", "E-commerce", None, False, None, None),
    ("nykaa", "Nykaa", "E-commerce", "grievanceofficercs@nykaa.com", True, "https://www.nykaa.com/", None),
    ("zomato", "Zomato", "Food delivery", "grievance@zomato.com", True, "https://www.zomato.com/", None),
    ("swiggy", "Swiggy", "Food delivery", "grievances@swiggy.in", True, "https://www.swiggy.com/", None),
    ("zepto", "Zepto", "Quick commerce", None, False, None, None),
    ("blinkit", "Blinkit", "Quick commerce", None, False, None, None),
    ("bigbasket", "BigBasket", "Quick commerce", None, False, None, None),
    ("jiomart", "JioMart", "E-commerce", None, False, None, None),
    ("jio", "Jio", "Telecom", None, False, None, None),
    ("airtel", "Airtel", "Telecom", None, False, None, None),
    ("vi", "Vi", "Telecom", None, False, None, None),
    ("bsnl", "BSNL", "Telecom", None, False, None, None),
    ("irctc", "IRCTC", "Travel", None, False, None, None),
    ("makemytrip", "MakeMyTrip", "Travel", None, False, None, None),
    ("goibibo", "Goibibo", "Travel", None, False, None, None),
    ("indigo", "IndiGo", "Travel", "nodalofficer@goindigo.in", True, "https://www.goindigo.in/", None),
    ("ola", "Ola", "Mobility", "grievance@olacabs.com", True, "https://www.olacabs.com/", None),
    ("uber", "Uber India", "Mobility", "grievanceredressaloffice@uber.com", True, "https://www.uber.com/in/en/", None),
    ("rapido", "Rapido", "Mobility", None, False, None, None),
    ("paytm", "Paytm", "Payments", None, False, None, None),
    ("phonepe", "PhonePe", "Payments", "pno@phonepe.com", True, "https://www.phonepe.com/", None),
    ("googlepay", "Google Pay", "Payments", "nodal-gpay@google.com", True, "https://pay.google.com/intl/en_in/about/", None),
    ("razorpay", "Razorpay", "Payments", None, False, None, None),
    ("ajio", "AJIO", "E-commerce", None, False, None, None),
    ("tatacliq", "Tata CLiQ", "E-commerce", None, False, None, None),
]

ALIASES = {
    "amazon": ("amazon", "amazon india"),
    "flipkart": ("flipkart",),
    "myntra": ("myntra",),
    "meesho": ("meesho",),
    "nykaa": ("nykaa",),
    "zomato": ("zomato",),
    "swiggy": ("swiggy",),
    "zepto": ("zepto",),
    "blinkit": ("blinkit",),
    "bigbasket": ("bigbasket", "big basket"),
    "jiomart": ("jiomart", "jio mart"),
    "jio": ("jio fiber", "jio"),
    "airtel": ("airtel",),
    "vi": ("vodafone idea", "vi telecom"),
    "bsnl": ("bsnl",),
    "irctc": ("irctc",),
    "makemytrip": ("makemytrip", "make my trip"),
    "goibibo": ("goibibo",),
    "indigo": ("indigo", "6e"),
    "ola": ("ola",),
    "uber": ("uber",),
    "rapido": ("rapido",),
    "paytm": ("paytm",),
    "phonepe": ("phonepe", "phone pe"),
    "googlepay": ("google pay", "gpay"),
    "razorpay": ("razorpay",),
    "ajio": ("ajio",),
    "tatacliq": ("tata cliq", "tatacliq"),
}


def seed_nodal_directory(session: Session) -> None:
    existing = set(session.scalars(select(NodalDirectory.merchant_key)).all())
    for key, name, sector, email, verified, source, turnaround in MERCHANTS:
        if key in existing:
            continue
        session.add(
            NodalDirectory(
                merchant_key=key,
                merchant_name=name,
                sector=sector,
                email=email,
                email_verified=verified,
                source_url=source,
                turnaround_days=turnaround,
            )
        )
    session.flush()


def find_merchant(session: Session, merchant_name: str | None) -> NodalDirectory | None:
    if not merchant_name:
        return None
    normalized = " ".join(merchant_name.casefold().split())
    rows = session.scalars(select(NodalDirectory)).all()
    for row in rows:
        candidates = ALIASES.get(row.merchant_key, (row.merchant_name.casefold(),))
        if any(alias in normalized for alias in candidates) or normalized in row.merchant_name.casefold():
            return row
    return None


def directory_public_rows(session: Session) -> list[dict]:
    rows = session.scalars(select(NodalDirectory).order_by(NodalDirectory.sector, NodalDirectory.merchant_name)).all()
    return [
        {
            "merchant_name": row.merchant_name,
            "sector": row.sector,
            "email": row.email,
            "email_verified": row.email_verified,
            "source_url": row.source_url,
            "turnaround_days": row.turnaround_days,
        }
        for row in rows
    ]
