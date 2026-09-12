"""JLCPCB's assembly parts catalogue: search by manufacturer part number or by value and package.

The catalogue behind jlcpcb.com/parts answers a JSON POST that the web page itself uses; there is
no documented API, so this can break without notice. Every hit carries the LCSC code the fab's BOM
needs, the stock, whether the part is a *basic* part (no extended-part fee) and the unit price.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from .errors import LayerError
from .models import PartHit, PartsSearch

log = logging.getLogger(__name__)

SEARCH_URL = "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"
PARTS_FETCH_FAILED = "PARTS_FETCH_FAILED"


def search(keyword: str, limit: int = 8, *, in_stock_only: bool = True, timeout_s: float = 30.0) -> PartsSearch:
    body = json.dumps({
        "currentPage": 1, "pageSize": max(1, min(limit, 50)), "keyword": keyword, "componentLibraryType": "",
        "preferredComponentFlag": False, "stockFlag": bool(in_stock_only), "stockSort": None, "firstSortName": "",
        "secondSortName": "", "componentBrandList": [], "componentSpecificationList": [], "componentAttributeList": [],
        "searchSource": "search",
    }).encode()
    req = urllib.request.Request(SEARCH_URL, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Origin": "https://jlcpcb.com", "Referer": "https://jlcpcb.com/parts",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        raise LayerError(PARTS_FETCH_FAILED, f"JLCPCB answered HTTP {ex.code} for {keyword!r}", hint="The undocumented catalogue endpoint may have changed; search on jlcpcb.com/parts by hand.") from ex
    except (urllib.error.URLError, TimeoutError, OSError) as ex:
        raise LayerError(PARTS_FETCH_FAILED, f"could not reach JLCPCB: {ex}", hint="Check the network; the catalogue is only reachable online.") from ex
    page = (data.get("data") or {}).get("componentPageInfo") or {}
    hits: list[PartHit] = []
    for it in page.get("list") or []:
        prices = it.get("componentPrices") or []
        hits.append(PartHit(
            lcsc=str(it.get("componentCode") or ""),
            mpn=str(it.get("componentModelEn") or ""),
            manufacturer=str(it.get("componentBrandEn") or ""),
            package=str(it.get("componentSpecificationEn") or ""),
            description=str(it.get("describe") or "")[:200],
            stock=int(it.get("stockCount") or 0),
            basic=(it.get("componentLibraryType") == "base"),
            price_usd=float(prices[0].get("productPrice")) if prices and prices[0].get("productPrice") is not None else None,
            min_qty=int(prices[0].get("startNumber") or 0) if prices else None,
        ))
    return PartsSearch(keyword=keyword, total=int(page.get("total") or len(hits)), hits=hits, source="jlcpcb.com/parts catalogue, in-stock filter " + ("on" if in_stock_only else "off"))


def best(hits: list[PartHit]) -> PartHit | None:
    """Prefer basic parts, then the largest stock."""
    if not hits:
        return None
    return max(hits, key=lambda h: (h.basic, h.stock))
