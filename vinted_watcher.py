"""
Logica di interrogazione di Vinted e formattazione dei messaggi Telegram.

Usa la libreria "vinted-api-kit" (https://pypi.org/project/vinted-api-kit/),
che si occupa di:
  - rilevare automaticamente il dominio/locale dall'URL (vinted.it, vinted.fr, ...)
  - gestire i cookie di sessione richiesti dal sistema anti-bot di Vinted (DataDome)

NOTE IMPORTANTI:
  - Vinted non offre un'API pubblica ufficiale per questo scopo: quella usata
    qui è la stessa API "interna" usata dal sito, protetta da un sistema
    anti-bot. La libreria gestisce i cookie automaticamente, ma se il bot
    gira su un IP "da datacenter" (molti VPS economici) Vinted può comunque
    restituire errori 403/429. Funziona meglio da un IP residenziale
    (es. un mini-PC o Raspberry Pi a casa, o un normale PC sempre acceso).
  - Il costo di spedizione esatto dipende dall'indirizzo dell'acquirente e
    dal metodo di spedizione scelto, quindi NON è sempre disponibile tramite
    l'API pubblica. Quando non lo troviamo, lo segnaliamo nel messaggio
    invece di inventarlo.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from vinted import VintedClient
from vinted.exceptions import VintedError

logger = logging.getLogger(__name__)


@dataclass
class ItemInfo:
    item_id: str
    title: str
    url: str
    photo_url: str
    price: float
    currency: str
    total_price: Optional[float]
    service_fee: Optional[float]
    shipping_hint: Optional[str]
    brand: str
    size: str


def _find_shipping_hint(raw: dict) -> Optional[str]:
    """Cerca in modo euristico un campo relativo alla spedizione nel JSON
    grezzo dell'articolo. Non è garantito che Vinted lo includa in questa
    risposta: se non lo troviamo restituiamo None."""
    candidates: list[tuple[str, Any]] = []

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > 3 or len(candidates) >= 3:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                lk = k.lower()
                if "shipping" in lk and ("price" in lk or "amount" in lk or "cost" in lk):
                    candidates.append((k, v))
                elif isinstance(v, (dict, list)):
                    _walk(v, depth + 1)
        elif isinstance(node, list):
            for item in node[:5]:
                _walk(item, depth + 1)

    _walk(raw)
    if not candidates:
        return None

    _, value = candidates[0]
    if isinstance(value, dict):
        amount = value.get("amount")
        currency = value.get("currency_code", "")
        if amount is not None:
            try:
                return f"{float(amount):.2f} {currency}".strip()
            except (TypeError, ValueError):
                return None
    elif isinstance(value, (int, float, str)):
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return str(value)
    return None


async def poll_search(client: VintedClient, search, per_page: int = 20) -> list[dict]:
    """Interroga una ricerca e restituisce la lista grezza (dict) di articoli
    trovati in pagina 1, nell'ordine restituito da Vinted. Se l'URL della
    ricerca contiene già order=newest_first (consigliato), saranno ordinati
    dal più recente."""
    items = await client.search_items(url=search.url, per_page=per_page, raw_data=True)
    return items or []


def extract_new_item_dicts(raw_items: list[dict], seen_ids: set[str]) -> list[dict]:
    """Filtra solo gli articoli non ancora notificati, restituendoli in
    ordine cronologico (dal più vecchio al più nuovo tra i nuovi trovati),
    così i messaggi Telegram arrivano nell'ordine giusto."""
    new_items = [it for it in raw_items if str(it.get("id")) not in seen_ids]
    return list(reversed(new_items))  # Vinted li restituisce dal più recente al più vecchio


async def build_item_info(client: VintedClient, raw_item: dict) -> ItemInfo:
    """Arricchisce un articolo trovato in catalogo con i dettagli (tasse,
    prezzo totale, eventuale stima di spedizione) leggendo la pagina
    dell'articolo. Richiede una chiamata di rete aggiuntiva, ma viene fatta
    solo per i nuovi articoli, non per l'intero catalogo ad ogni ciclo."""
    item_id = str(raw_item.get("id"))
    title = raw_item.get("title") or "(senza titolo)"
    url = raw_item.get("url") or ""
    brand = raw_item.get("brand_title") or ""
    size = raw_item.get("size_title") or ""

    price_data = raw_item.get("price") or {}
    if isinstance(price_data, dict):
        currency = price_data.get("currency_code", "EUR")
        price = float(price_data.get("amount", 0) or 0)
    else:
        currency = raw_item.get("currency", "EUR")
        try:
            price = float(price_data)
        except (TypeError, ValueError):
            price = 0.0

    photo_data = raw_item.get("photo") or {}
    photo_url = photo_data.get("url", "") if isinstance(photo_data, dict) else str(photo_data or "")

    total_price: Optional[float] = None
    service_fee: Optional[float] = None
    shipping_hint: Optional[str] = None

    if url:
        detail: Optional[dict] = None
        try:
            detail = await client.item_details(url=url, raw_data=True)
        except VintedError as exc:
            logger.warning("Impossibile leggere i dettagli di %s: %s", url, exc)

        if detail:
            total_data = detail.get("total_item_price")
            if isinstance(total_data, dict):
                total_price = float(total_data.get("amount", 0) or 0)
            elif total_data is not None:
                try:
                    total_price = float(total_data)
                except (TypeError, ValueError):
                    total_price = None

            if total_price is not None and price:
                service_fee = round(total_price - price, 2)

            if service_fee is None:
                sf_data = detail.get("service_fee")
                if isinstance(sf_data, dict) and sf_data.get("amount") is not None:
                    try:
                        service_fee = float(sf_data["amount"])
                    except (TypeError, ValueError):
                        pass

            shipping_hint = _find_shipping_hint(detail)

            if not photo_url:
                photos = detail.get("photos") or []
                if photos and isinstance(photos[0], dict):
                    photo_url = photos[0].get("url", "")

    return ItemInfo(
        item_id=item_id,
        title=title,
        url=url,
        photo_url=photo_url,
        price=price,
        currency=currency,
        total_price=total_price,
        service_fee=service_fee,
        shipping_hint=shipping_hint,
        brand=brand,
        size=size,
    )


def format_caption(info: ItemInfo, search_name: str) -> str:
    lines = [f"🆕 <b>{_escape(info.title)}</b>"]

    meta_bits = [b for b in (info.brand, info.size) if b]
    if meta_bits:
        lines.append(" · ".join(_escape(b) for b in meta_bits))

    lines.append(f"💶 Prezzo: <b>{info.price:.2f} {info.currency}</b>")

    if info.service_fee is not None:
        lines.append(f"🧾 Tasse (Protezione Acquisti): {info.service_fee:.2f} {info.currency}")
    if info.total_price is not None:
        lines.append(f"➡️ Totale (senza spedizione): <b>{info.total_price:.2f} {info.currency}</b>")

    if info.shipping_hint:
        lines.append(f"📦 Spedizione (stima): {info.shipping_hint}")
    else:
        lines.append("📦 Spedizione: non disponibile in anteprima, vedi l'annuncio")

    if info.url:
        lines.append(f'\n🔗 <a href="{info.url}">Apri su Vinted</a>')

    lines.append(f"\n<i>Ricerca: {_escape(search_name)}</i>")
    return "\n".join(lines)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
