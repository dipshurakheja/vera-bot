"""Signal extraction: raw merchant + category context -> typed, comparable facts.

Every signal carries the literal numbers it came from so composers can quote
them verbatim (no invented statistics). Nothing here is trigger-specific.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..context.views import CategoryView, MerchantView, num
from ..utils.text import humanize

METRIC_LABELS = {
    "views": "profile views",
    "views_pct": "profile views",
    "calls": "calls",
    "calls_pct": "calls",
    "ctr": "click-through rate",
    "ctr_pct": "click-through rate",
    "directions": "direction requests",
    "directions_pct": "direction requests",
    "leads": "leads",
    "leads_pct": "leads",
    "review_count": "Google reviews",
    "reviews": "Google reviews",
}

# customer_aggregate keys that describe a meaningful sub-cohort, with human labels
COHORT_KEYS = {
    "high_risk_adult_count": "high-risk adult patients",
    "chronic_rx_count": "chronic-prescription customers",
    "total_active_members": "active members",
}

LAPSED_KEYS = {
    "lapsed_180d_plus": "haven't visited in 6+ months",
    "lapsed_90d_plus": "haven't been back in 90+ days",
    "lapsed_60d_plus": "haven't been back in 60+ days",
}


def metric_label(metric: str) -> str:
    return METRIC_LABELS.get(metric, humanize(metric.replace("_pct", "")))


@dataclass
class Signals:
    views: Optional[float] = None
    calls: Optional[float] = None
    directions: Optional[float] = None
    ctr: Optional[float] = None
    leads: Optional[float] = None
    peer_ctr: Optional[float] = None
    peer_views: Optional[float] = None
    peer_calls: Optional[float] = None
    peer_reviews: Optional[float] = None
    drop: Optional[tuple[str, float]] = None      # (metric_key, delta) most negative 7d delta
    rise: Optional[tuple[str, float]] = None      # (metric_key, delta) most positive 7d delta
    stale_posts_days: Optional[int] = None
    dormant_days: Optional[int] = None
    renewal_days: Optional[int] = None
    unverified: bool = False
    no_active_offers: bool = False
    lapsed_count: Optional[int] = None
    lapsed_label: str = ""
    retention: Optional[tuple[float, Optional[float], str]] = None   # (merchant, peer, window label)
    churn: Optional[tuple[float, Optional[float]]] = None            # monthly churn (merchant, peer)
    cohort: Optional[tuple[str, int, str]] = None                    # (key, count, label)
    total_people: Optional[int] = None
    repeat_pct: Optional[float] = None
    neg_theme: Optional[dict] = None
    pos_theme: Optional[dict] = None
    pending_intent: Optional[dict] = None
    engaged_recently: bool = False
    sub_status: str = ""
    days_remaining: Optional[int] = None
    days_since_expiry: Optional[int] = None
    notes: list[str] = field(default_factory=list)

    @property
    def ctr_below_peer(self) -> bool:
        return self.ctr is not None and self.peer_ctr is not None and self.ctr < self.peer_ctr * 0.95

    @property
    def ctr_above_peer(self) -> bool:
        return self.ctr is not None and self.peer_ctr is not None and self.ctr > self.peer_ctr * 1.05

    @property
    def calls_below_peer(self) -> bool:
        return self.calls is not None and self.peer_calls is not None and self.calls < self.peer_calls * 0.9

    @property
    def views_below_peer(self) -> bool:
        return self.views is not None and self.peer_views is not None and self.views < self.peer_views * 0.9

    @property
    def views_above_peer(self) -> bool:
        return self.views is not None and self.peer_views is not None and self.views > self.peer_views * 1.1

    @property
    def calls_above_peer(self) -> bool:
        return self.calls is not None and self.peer_calls is not None and self.calls > self.peer_calls * 1.1

    def weak_metrics(self) -> set[str]:
        weak = set()
        if self.ctr_below_peer:
            weak.add("ctr")
        if self.calls_below_peer or (self.drop and self.drop[0].startswith("calls")):
            weak.add("calls")
        if self.views_below_peer or (self.drop and self.drop[0].startswith("views")):
            weak.add("views")
        if self.retention and self.retention[1] is not None and self.retention[0] < self.retention[1]:
            weak.add("retention")
        if self.churn and self.churn[1] is not None and self.churn[0] > self.churn[1]:
            weak.add("retention")
        return weak


def extract(merchant: MerchantView, category: CategoryView) -> Signals:
    s = Signals()
    s.views = merchant.perf("views")
    s.calls = merchant.perf("calls")
    s.directions = merchant.perf("directions")
    s.ctr = merchant.perf("ctr")
    s.leads = merchant.perf("leads")
    s.peer_ctr = category.peer("avg_ctr")
    s.peer_views = category.peer("avg_views_30d")
    s.peer_calls = category.peer("avg_calls_30d")
    s.peer_reviews = category.peer("avg_review_count", "avg_reviews")

    deltas = merchant.delta_7d
    if deltas:
        k_min = min(sorted(deltas), key=lambda k: deltas[k])
        k_max = max(sorted(deltas), key=lambda k: deltas[k])
        if deltas[k_min] <= -0.05:
            s.drop = (k_min, deltas[k_min])
        if deltas[k_max] >= 0.05:
            s.rise = (k_max, deltas[k_max])

    stale = merchant.signal_value("stale_posts")
    s.stale_posts_days = int(stale) if stale else None
    dormant = merchant.signal_value("dormant_with_vera")
    s.dormant_days = int(dormant) if dormant else None
    renewal = merchant.signal_value("renewal_due_soon")
    s.renewal_days = int(renewal) if renewal else None

    s.unverified = merchant.verified is False or merchant.has_signal("unverified_gbp")
    s.no_active_offers = not merchant.active_offers

    for key, label in LAPSED_KEYS.items():
        v = merchant.agg(key)
        if v:
            s.lapsed_count, s.lapsed_label = int(v), label
            break

    for key, peer_key, label in (
        ("retention_6mo_pct", "retention_6mo_pct", "6-month"),
        ("retention_3mo_pct", "retention_3mo_pct", "3-month"),
    ):
        v = merchant.agg(key)
        if v is not None:
            s.retention = (v, category.peer(peer_key), label)
            break

    churn = merchant.agg("monthly_churn_pct")
    if churn is not None:
        s.churn = (churn, category.peer("monthly_churn_pct"))

    for key, label in COHORT_KEYS.items():
        v = merchant.agg(key)
        if v:
            s.cohort = (key, int(v), label)
            break

    total = merchant.agg("total_active_members", "total_unique_ytd")
    s.total_people = int(total) if total else None
    s.repeat_pct = merchant.agg("repeat_customer_pct")

    themes = []
    for t in merchant.review_themes:  # normalise counts so composers can print them safely
        occ = num(t.get("occurrences_30d"))
        themes.append(dict(t, occurrences_30d=int(occ) if occ is not None and occ > 0 else None))
    negs = sorted((t for t in themes if str(t.get("sentiment", "")).lower().startswith("neg")),
                  key=lambda t: (-(t["occurrences_30d"] or 0), str(t.get("theme"))))
    poss = sorted((t for t in themes if str(t.get("sentiment", "")).lower().startswith("pos") and t["occurrences_30d"]),
                  key=lambda t: (-(t["occurrences_30d"] or 0), str(t.get("theme"))))
    s.neg_theme = negs[0] if negs else None
    s.pos_theme = poss[0] if poss else None

    s.pending_intent = _pending_intent(merchant)
    s.engaged_recently = merchant.has_signal("engaged_in_last")

    sub = merchant.subscription
    s.sub_status = str(sub.get("status", "")).lower()
    dr = sub.get("days_remaining")
    s.days_remaining = int(dr) if isinstance(dr, (int, float)) and not isinstance(dr, bool) else None
    dse = sub.get("days_since_expiry")
    s.days_since_expiry = int(dse) if isinstance(dse, (int, float)) and not isinstance(dse, bool) else None
    return s


def _pending_intent(merchant: MerchantView) -> Optional[dict]:
    """Last merchant turn that expressed intent, with the Vera turn it answered."""
    hist = merchant.history
    for i in range(len(hist) - 1, -1, -1):
        turn = hist[i]
        if str(turn.get("from", "")).lower() != "merchant":
            continue
        engagement = str(turn.get("engagement", "")).lower()
        if not engagement.startswith("intent"):
            return None
        prev_vera = ""
        for j in range(i - 1, -1, -1):
            if str(hist[j].get("from", "")).lower() == "vera":
                prev_vera = str(hist[j].get("body", ""))
                break
        return {
            "merchant_text": str(turn.get("body", "")),
            "vera_text": prev_vera,
            "engagement": engagement,
            "ts": turn.get("ts"),
        }
    return None
