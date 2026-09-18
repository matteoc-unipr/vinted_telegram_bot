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

PATCH ENDPOINT RICERCA (settembre 2026): verso metà settembre 2026 Vinted
ha spostato l'endpoint di ricerca catalogo da
    https://www.vinted.<tld>/api/v2/catalog/items
a
    https://api.vinted.<tld>/svc-catalogue/items
cambiando anche il formato di alcuni parametri di filtro e richiedendo
nuovi header (X-Anon-Id, X-Csrf-Token). La libreria "vinted-api-kit" (v1.0.0,
gennaio 2026) non è ancora stata aggiornata per seguirlo, quindi qui sotto
reimplementiamo SOLO la chiamata di ricerca, riusando l'autenticazione/
sessione già gestita dalla libreria. Il recupero dei dettagli del singolo
articolo (item_details) non risulta interessato da questo cambiamento e
resta quindi invariato.
ATTENZIONE: la mappatura di parametri/header qui sotto si basa su
segnalazioni pubbliche di problemi analoghi su librerie simili, non su un
test diretto contro Vinted (che da questo ambiente di sviluppo non è
raggiungibile): è ragionevole aspettarsi di doverla affinare in base ai
log reali.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from vinted import VintedClient
from vinted.exceptions import VintedAPIError, VintedError, VintedNetworkError

logger = logging.getLogger(__name__)

_CSRF_META_RE = re.compile(
    r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', re.IGNORECASE
)

# Parametri che nel nuovo endpoint sono stati spostati sotto attribute_ids[...].
# Confermato solo per catalog_ids/brand_ids dalle segnalazioni disponibili;
# gli altri filtri (size_ids, status_ids, price_*, ...) restano con lo
# stesso nome per ora.
_ATTRIBUTE_KEY_MAP = {
    "catalog_ids": "attribute_ids[catalog]",
    "brand_ids": "attribute_ids[brand]",
}


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
    dal più recente.

    NOTA: usa _patched_catalog_search invece del metodo search_items della
    libreria, perché quest'ultimo punta a un endpoint che Vinted ha
    dismesso a metà settembre 2026 (vedi note a inizio file)."""
    items = await _patched_catalog_search(client, search.url, per_page=per_page)
    return items or []


async def _patched_catalog_search(
    client: VintedClient, search_url: str, per_page: int = 20, page: int = 1
) -> list[dict[str, Any]]:
    """Cerca nel catalogo Vinted usando il nuovo endpoint svc-catalogue,
    riusando la sessione/i cookie già autenticati e gestiti da VintedClient
    (accediamo ad attributi "privati" della libreria di proposito: sono
    già stati testati per costruire la richiesta all'endpoint vecchio, e
    qui li riusiamo solo per ricostruire parametri e sessione)."""
    http_session = client._session
    catalog_api = client._catalog

    http_session.configure_from_url(search_url)

    old_params = catalog_api._build_params(search_url, per_page=per_page, page=page)
    old_params["time"] = int(time.time())

    new_params: dict[str, Any] = {}
    for key, value in old_params.items():
        new_params[_ATTRIBUTE_KEY_MAP.get(key, key)] = value

    locale = http_session.locale or "com"
    new_url = f"https://api.vinted.{locale}/svc-catalogue/items"

    extra_headers: dict[str, str] = {
        # Il nuovo endpoint potrebbe non dedurre più il paese dal dominio
        # come faceva quello vecchio (specie chiamandolo da un server negli
        # USA): dichiariamo esplicitamente la localizzazione italiana.
        "Accept-Language": f"{locale}-{locale.upper()},{locale};q=0.9",
    }
    anon_id = _extract_anon_id(http_session)
    if anon_id:
        extra_headers["X-Anon-Id"] = anon_id
    csrf_token = await _extract_csrf_token(http_session)
    if csrf_token:
        extra_headers["X-Csrf-Token"] = csrf_token

    # api.vinted.<tld> è un sottodominio diverso da www.vinted.<tld>, dove
    # sono stati ottenuti i cookie di sessione: se quei cookie hanno un
    # Domain ristretto a www, il jar automatico del client NON li invia
    # qui, rendendo la richiesta di fatto anonima (spiegherebbe il
    # catalogo/valuta sbagliati visti nei log). Li alleghiamo a mano.
    cookie_domains = _log_cookie_domains(http_session)
    cookie_header = _cookie_header_from_jar(http_session)
    if cookie_header:
        extra_headers["Cookie"] = cookie_header

    logger.info(
        "svc-catalogue: domini cookie disponibili=%s, cookie allegati manualmente=%s",
        cookie_domains, bool(cookie_header),
    )
    logger.debug(
        "svc-catalogue: richiesta url=%s params=%s header_extra=%s",
        new_url, new_params, list(extra_headers.keys()),
    )

    try:
        response = await http_session.session.get(
            new_url, params=new_params, headers=extra_headers, impersonate="chrome", verify=True,
        )
    except Exception as exc:
        raise VintedNetworkError("Errore di rete su svc-catalogue", exc) from exc

    if response.status_code >= 400:
        snippet = _safe_snippet(response)
        logger.warning(
            "svc-catalogue ha risposto %s per %s | header inviati: %s | corpo: %s",
            response.status_code, getattr(response, "url", new_url),
            list(extra_headers.keys()), snippet,
        )
        raise VintedAPIError(
            f"HTTP {response.status_code} da svc-catalogue",
            status_code=response.status_code, response=response,
        )

    try:
        data = response.json()
    except Exception as exc:
        logger.warning("svc-catalogue: 200 OK ma corpo non-JSON: %s", _safe_snippet(response))
        raise VintedAPIError(
            "Risposta non valida da svc-catalogue", status_code=response.status_code, response=response,
        ) from exc

    items = data.get("items") if isinstance(data, dict) else None
    if items is None:
        # La forma della risposta potrebbe essere cambiata rispetto al
        # vecchio endpoint: logghiamo le chiavi di primo livello per
        # capire come adattare il parsing in un prossimo aggiustamento.
        logger.warning(
            "svc-catalogue: 200 OK ma nessuna chiave 'items' nella risposta. Chiavi presenti: %s",
            list(data.keys()) if isinstance(data, dict) else type(data).__name__,
        )
        items = []
    else:
        logger.info("svc-catalogue: trovati %d articoli per %s", len(items), search_url)
        if items:
            # Diagnostica: mostriamo il primo articolo così com'è (con le
            # miniature della foto accorciate, che altrimenti da sole
            # riempiono il limite del log e nascondono gli altri campi),
            # per capire se lo schema è cambiato rispetto al vecchio
            # endpoint (es. url, prezzo, paese/valuta venditore).
            logger.info(
                "svc-catalogue: esempio primo articolo (per diagnosi): %s",
                _safe_json_snippet(_slim_item_for_log(items[0]), max_len=3000),
            )

    return items


def _safe_snippet(response) -> str:
    try:
        text = response.text or ""
    except Exception:
        return "(corpo non leggibile)"
    return " ".join(text.split())[:300]


def _safe_json_snippet(obj: Any, max_len: int = 800) -> str:
    try:
        text = json.dumps(obj, ensure_ascii=False)
    except Exception:
        text = str(obj)
    return text[:max_len]


def _slim_item_for_log(item: dict) -> dict:
    """Copia dell'articolo per il solo log diagnostico, con l'eventuale
    lista di miniature della foto accorciata: da sola può occupare gran
    parte del limite di lunghezza del log, nascondendo gli altri campi
    (prezzo, url, ecc.) che vengono dopo nel JSON."""
    slim = dict(item)
    photo = slim.get("photo")
    if isinstance(photo, dict) and isinstance(photo.get("thumbnails"), list):
        slim["photo"] = {**photo, "thumbnails": f"[{len(photo['thumbnails'])} miniature omesse]"}
    return slim


def _log_cookie_domains(http_session) -> list[str]:
    try:
        return sorted({getattr(c, "domain", "?") or "?" for c in http_session.session.cookies.jar})
    except Exception:
        logger.debug("Impossibile leggere i domini dei cookie", exc_info=True)
        return []


def _cookie_header_from_jar(http_session) -> str:
    """Costruisce a mano un header Cookie con tutti i cookie della sessione,
    per garantire che vengano inviati anche al sottodominio api.vinted.<tld>
    anche se il loro Domain originale è ristretto a www.vinted.<tld> (in tal
    caso il cookie jar automatico non li invierebbe a un host diverso)."""
    try:
        pairs = [f"{c.name}={c.value}" for c in http_session.session.cookies.jar if c.name and c.value]
        return "; ".join(pairs)
    except Exception:
        logger.debug("Impossibile costruire l'header Cookie manuale", exc_info=True)
        return ""


def _extract_anon_id(http_session) -> Optional[str]:
    try:
        for cookie in http_session.session.cookies.jar:
            name = (getattr(cookie, "name", "") or "").lower()
            if "anon" in name:
                return cookie.value
    except Exception:
        logger.debug("Impossibile leggere i cookie per cercare l'anon id", exc_info=True)
    return None


async def _extract_csrf_token(http_session) -> Optional[str]:
    """Il token CSRF non è nei cookie: proviamo a estrarlo dal tag meta
    standard di Rails nella home page di Vinted, e lo mettiamo in cache
    sull'oggetto sessione per non rifare questa richiesta extra ad ogni
    singola ricerca dello stesso ciclo."""
    cached = getattr(http_session, "_patched_csrf_token", None)
    if cached:
        return cached
    if not http_session.base_url:
        return None
    try:
        resp = await http_session.session.get(
            http_session.base_url, impersonate="chrome", verify=True
        )
    except Exception:
        logger.debug("Impossibile recuperare la home page per il csrf-token", exc_info=True)
        return None
    match = _CSRF_META_RE.search(resp.text or "")
    token = match.group(1) if match else None
    if token:
        http_session._patched_csrf_token = token
    return token


def extract_new_item_dicts(raw_items: list[dict], seen_ids: set[str]) -> list[dict]:
    """Filtra solo gli articoli non ancora notificati, restituendoli in
    ordine cronologico (dal più vecchio al più nuovo tra i nuovi trovati),
    così i messaggi Telegram arrivano nell'ordine giusto."""
    new_items = [it for it in raw_items if str(it.get("id")) not in seen_ids]
    return list(reversed(new_items))  # Vinted li restituisce dal più recente al più vecchio


def _absolute_item_url(client: VintedClient, raw_url: str, item_id: str, title: str) -> str:
    """Garantisce un link assoluto e funzionante all'annuncio, anche se il
    nuovo endpoint restituisce un percorso relativo o nessun link affatto."""
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return raw_url

    try:
        base = (client._session.base_url or "").rstrip("/")
    except Exception:
        base = ""

    if raw_url:
        path = raw_url if raw_url.startswith("/") else f"/{raw_url}"
        return f"{base}{path}"

    if item_id and item_id != "None" and base:
        # Gli URL articolo di Vinted funzionano anche senza lo slug testuale
        # dopo l'id (es. /items/123 invece di /items/123-nome-articolo).
        return f"{base}/items/{item_id}"

    return raw_url  # nessun modo di costruire un link valido, resta vuoto


async def build_item_info(client: VintedClient, raw_item: dict) -> ItemInfo:
    """Arricchisce un articolo trovato in catalogo con i dettagli (tasse,
    prezzo totale, eventuale stima di spedizione) leggendo la pagina
    dell'articolo. Richiede una chiamata di rete aggiuntiva, ma viene fatta
    solo per i nuovi articoli, non per l'intero catalogo ad ogni ciclo."""
    item_id = str(raw_item.get("id"))
    title = raw_item.get("title") or "(senza titolo)"
    url = _absolute_item_url(client, raw_item.get("url") or "", item_id, title)
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
    if isinstance(photo_data, dict):
        # Il nuovo endpoint (settembre 2026) chiama il campo "full_size_url"
        # invece del vecchio "url"; teniamo comunque "url" come ripiego nel
        # caso torni a cambiare ancora.
        photo_url = photo_data.get("full_size_url") or photo_data.get("url") or ""
    else:
        photo_url = str(photo_data or "")

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
