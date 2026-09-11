"""
Bot Telegram per il monitoraggio di ricerche Vinted.

Comandi:
  /start                Messaggio di benvenuto
  /aiuto                Guida rapida
  /aggiungi <url> ...   Aggiunge una o più ricerche Vinted da monitorare
                         (URL multipli separati da spazio o a capo)
  /lista                Mostra le ricerche attive per questa chat
  /rimuovi <id>         Rimuove una ricerca
  /pausa <id>           Mette in pausa una ricerca (senza eliminarla)
  /riprendi <id>        Riattiva una ricerca in pausa

Il bot continua a controllare Vinted 24 ore su 24. Durante le "ore
silenziose" (configurabili, default 23:00-09:00) non invia notifiche
singole: raccoglie gli annunci trovati e li invia tutti insieme come
riepilogo appena finisce l'orario silenzioso.

Configurazione tramite variabili d'ambiente: vedi .env.example.

Avvio:
    python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote_plus
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
from vinted import VintedClient
from vinted.exceptions import VintedError, VintedRateLimitError

import vinted_watcher as vw
from storage import Storage

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO"),
)
logger = logging.getLogger("vinted_bot")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "bot.db"
COOKIES_DIR = DATA_DIR / "cookies"
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "90"))
ITEMS_PER_PAGE = int(os.environ.get("ITEMS_PER_PAGE", "20"))
ALLOWED_CHAT_IDS = {
    int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip()
}

# Ore silenziose: fuori da questo intervallo il bot invia le notifiche
# normalmente; dentro questo intervallo raccoglie gli annunci senza
# notificarli e li invia come riepilogo unico appena l'orario finisce.
# Usa il fuso orario indicato in TIMEZONE, non quello del server (utile
# perché molte VM cloud girano di default in UTC).
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Rome"))
QUIET_HOURS_START = int(os.environ.get("QUIET_HOURS_START", "23"))  # 23 = 23:00
QUIET_HOURS_END = int(os.environ.get("QUIET_HOURS_END", "9"))  # 9 = 09:00
DIGEST_MAX_ITEMS = int(os.environ.get("DIGEST_MAX_ITEMS", "20"))

# Timeout di sicurezza: la libreria che parla con Vinted non ha timeout
# propri, quindi se una richiesta resta "appesa" il bot si bloccherebbe
# per sempre. Questi limiti garantiscono che ogni ciclo si sblocchi da
# solo anche in caso di problemi di rete.
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "25"))
JOB_TIMEOUT_SECONDS = float(os.environ.get("JOB_TIMEOUT_SECONDS", "240"))

VINTED_URL_RE = re.compile(r"https?://(www\.)?vinted\.[a-z.]+/catalog\?", re.IGNORECASE)

storage = Storage(DB_PATH)


def _chat_allowed(chat_id: int) -> bool:
    return not ALLOWED_CHAT_IDS or chat_id in ALLOWED_CHAT_IDS


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Ciao! Sono un bot per monitorare ricerche Vinted.\n\n"
        "1. Vai su Vinted, imposta i filtri che vuoi e ordina i risultati "
        "per 'Più recenti'.\n"
        "2. Copia l'URL dalla barra degli indirizzi.\n"
        "3. Mandamelo con /aggiungi <url>.\n\n"
        "Usa /aiuto per la lista completa dei comandi."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>Comandi disponibili</b>\n"
        "/aggiungi <code>&lt;url&gt; [altro url...]</code> — aggiungi una o più ricerche\n"
        "/lista — mostra le ricerche attive in questa chat\n"
        "/rimuovi <code>&lt;id&gt;</code> — elimina una ricerca\n"
        "/pausa <code>&lt;id&gt;</code> — sospende le notifiche per una ricerca\n"
        "/riprendi <code>&lt;id&gt;</code> — riattiva una ricerca in pausa\n\n"
        "Quando aggiungi una ricerca, il bot memorizza gli annunci già presenti "
        "in quel momento senza notificarli (per non spammarti la cronologia), "
        "poi ti avvisa solo dei <i>nuovi</i> annunci pubblicati da lì in poi.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not _chat_allowed(chat_id):
        await update.message.reply_text("Non sei autorizzato a usare questo bot.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usa: /aggiungi <url_vinted> [altro_url ...]\n"
            "Esempio: /aggiungi https://www.vinted.it/catalog?search_text=nike&order=newest_first"
        )
        return

    text = " ".join(context.args)
    candidate_urls = [u for u in re.split(r"[\s\n]+", text.strip()) if u]
    added: list[tuple[int, str]] = []
    skipped: list[str] = []

    for raw_url in candidate_urls:
        if not VINTED_URL_RE.search(raw_url):
            skipped.append(raw_url)
            continue
        name = _guess_name(raw_url)
        search_id = storage.add_search(chat_id, name, raw_url)
        added.append((search_id, name))

    if added:
        lines = "\n".join(f"#{sid} — {name}" for sid, name in added)
        await update.message.reply_text(
            f"✅ Aggiunte {len(added)} ricerca/e:\n{lines}\n\n"
            "Sto caricando gli annunci attuali come base di partenza, "
            "poi ti avviso solo dei nuovi arrivi (di solito entro un minuto o due)."
        )
        # Baseline in background, per non far aspettare la risposta al comando.
        context.application.create_task(_baseline_new_searches(added))

    if skipped:
        await update.message.reply_text(
            "⚠️ Questi link non sembrano URL di ricerca Vinted validi "
            "(deve essere un link del tipo https://www.vinted.it/catalog?...):\n"
            + "\n".join(skipped)
        )


def _guess_name(url: str) -> str:
    m = re.search(r"search_text=([^&]+)", url)
    if m and m.group(1):
        return unquote_plus(m.group(1))
    return "Ricerca Vinted"


async def _baseline_new_searches(added: list[tuple[int, str]]) -> None:
    ids = {sid for sid, _ in added}
    searches = {s.id: s for s in storage.list_active_searches() if s.id in ids}
    async with VintedClient(persist_cookies=True, cookies_dir=COOKIES_DIR) as client:
        for search_id, search in searches.items():
            try:
                raw_items = await asyncio.wait_for(
                    vw.poll_search(client, search, ITEMS_PER_PAGE),
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                storage.mark_seen(search_id, [str(it.get("id")) for it in raw_items])
                storage.mark_baseline_done(search_id)
                logger.info(
                    "Baseline completata per ricerca #%s (%d annunci di partenza)",
                    search_id, len(raw_items),
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Timeout (%ss) nella baseline della ricerca #%s. "
                    "Se non parte a notificare, prova a rimuoverla e riaggiungerla con /aggiungi.",
                    REQUEST_TIMEOUT_SECONDS, search_id,
                )
            except VintedError as exc:
                logger.warning("Baseline fallita per ricerca #%s: %s", search_id, exc)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    searches = storage.list_searches(chat_id)
    if not searches:
        await update.message.reply_text("Non hai ancora nessuna ricerca attiva. Usa /aggiungi <url>.")
        return

    lines = []
    for s in searches:
        status = "▶️ attiva" if s.active else "⏸ in pausa"
        baseline = "" if s.baseline_done else " (base in caricamento...)"
        lines.append(f"#{s.id} [{status}]{baseline} {s.name}\n{s.url}")
    await update.message.reply_text("\n\n".join(lines))


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usa: /rimuovi <id> (vedi /lista per gli id)")
        return
    try:
        search_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("L'id deve essere un numero. Vedi /lista.")
        return

    ok = storage.remove_search(chat_id, search_id)
    await update.message.reply_text("🗑️ Rimossa." if ok else "Ricerca non trovata.")


async def _set_active_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, active: bool) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usa: /pausa <id> oppure /riprendi <id> (vedi /lista)")
        return
    try:
        search_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("L'id deve essere un numero. Vedi /lista.")
        return
    ok = storage.set_active(chat_id, search_id, active)
    if ok:
        await update.message.reply_text("⏸ Messa in pausa." if not active else "▶️ Riattivata.")
    else:
        await update.message.reply_text("Ricerca non trovata.")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_active_cmd(update, context, active=False)


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_active_cmd(update, context, active=True)


def is_quiet_hours(now: datetime | None = None) -> bool:
    """True se l'ora locale attuale (fuso TIMEZONE) rientra nell'intervallo
    silenzioso. Gestisce anche il caso in cui l'intervallo attraversi la
    mezzanotte (es. 23 -> 9)."""
    hour = (now or datetime.now(TIMEZONE)).hour
    start, end = QUIET_HOURS_START, QUIET_HOURS_END
    if start == end:
        return False  # intervallo nullo: mai silenzioso
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # attraversa la mezzanotte


async def _send_item_notification(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, photo_url: str, caption: str
) -> None:
    try:
        if photo_url:
            await context.bot.send_photo(
                chat_id=chat_id, photo=photo_url, caption=caption, parse_mode=ParseMode.HTML
            )
        else:
            await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Errore inviando una notifica alla chat %s", chat_id)


async def _flush_pending_digests(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Invia, se presenti, gli annunci raccolti durante le ore silenziose.
    Va chiamata a inizio ciclo quando NON siamo più in orario silenzioso,
    così il riepilogo parte al primo controllo utile dopo la fine della
    notte, senza bisogno di un job separato a orario fisso."""
    for chat_id in storage.list_chats_with_pending():
        rows = storage.get_pending_items(chat_id)
        if not rows:
            continue

        to_send = rows[:DIGEST_MAX_ITEMS]  # già ordinati per prezzo crescente
        extra = len(rows) - len(to_send)

        intro = f"☀️ Buongiorno! Durante la notte ho trovato {len(rows)} nuovi annunci"
        intro += f", te ne mostro i {len(to_send)} più convenienti:" if extra > 0 else ":"
        try:
            await context.bot.send_message(chat_id=chat_id, text=intro)
        except Exception:
            logger.exception("Errore inviando l'intro del riepilogo alla chat %s", chat_id)

        for row in to_send:
            await _send_item_notification(context, chat_id, row["photo_url"], row["caption"])
            await asyncio.sleep(1.5)

        storage.clear_pending_items(chat_id)
        logger.info("Riepilogo mattutino inviato alla chat %s (%d annunci)", chat_id, len(rows))


async def _run_poll_cycle(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Un singolo ciclo di controllo di tutte le ricerche attive.

    Fuori dalle ore silenziose, notifica subito i nuovi annunci (e prima
    di tutto svuota eventuali riepiloghi rimasti in sospeso dalla notte).
    Durante le ore silenziose, raccoglie gli annunci trovati senza
    notificarli: verranno inviati come riepilogo al termine della notte.

    Usa un unico VintedClient per l'intero ciclo, così i cookie di sessione
    vengono riutilizzati invece di essere richiesti ad ogni ricerca. Ogni
    chiamata di rete verso Vinted è protetta da un timeout: la libreria
    usata non ne ha uno proprio, quindi senza questa protezione una
    richiesta che resta "appesa" bloccherebbe il bot indefinitamente.
    """
    quiet_now = is_quiet_hours()

    if not quiet_now:
        await _flush_pending_digests(context)

    searches = [s for s in storage.list_active_searches() if s.baseline_done]
    if not searches:
        return

    async with VintedClient(persist_cookies=True, cookies_dir=COOKIES_DIR) as client:
        for search in searches:
            try:
                raw_items = await asyncio.wait_for(
                    vw.poll_search(client, search, ITEMS_PER_PAGE),
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Timeout (%ss) interrogando la ricerca #%s, salto questo ciclo.",
                    REQUEST_TIMEOUT_SECONDS, search.id,
                )
                continue
            except VintedRateLimitError:
                logger.warning("Vinted ha limitato le richieste, salto questo ciclo e rallento.")
                await asyncio.sleep(10)
                continue
            except VintedError as exc:
                logger.warning("Errore interrogando la ricerca #%s: %s", search.id, exc)
                continue

            all_ids = [str(it.get("id")) for it in raw_items]
            seen_ids = storage.get_seen_ids(search.id, all_ids)
            new_raw_items = vw.extract_new_item_dicts(raw_items, seen_ids)

            # Segna tutto come visto SUBITO, prima di inviare le notifiche,
            # per evitare doppi avvisi se qualcosa fallisce più avanti.
            storage.mark_seen(search.id, all_ids)

            for raw_item in new_raw_items:
                try:
                    info = await asyncio.wait_for(
                        vw.build_item_info(client, raw_item),
                        timeout=REQUEST_TIMEOUT_SECONDS,
                    )
                    caption = vw.format_caption(info, search.name)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Timeout (%ss) sui dettagli dell'articolo %s, salto.",
                        REQUEST_TIMEOUT_SECONDS, raw_item.get("id"),
                    )
                    continue
                except Exception:
                    logger.exception(
                        "Errore recuperando i dettagli dell'articolo %s", raw_item.get("id")
                    )
                    continue

                if quiet_now:
                    storage.add_pending_item(
                        chat_id=search.chat_id,
                        search_name=search.name,
                        item_id=info.item_id,
                        photo_url=info.photo_url,
                        caption=caption,
                        price=info.price,
                    )
                else:
                    await _send_item_notification(context, search.chat_id, info.photo_url, caption)

                # Pausa di cortesia dopo OGNI articolo elaborato, sia che sia
                # stato notificato subito sia che sia stato solo accodato per
                # il riepilogo: qui parte comunque una richiesta verso Vinted
                # (i dettagli dell'articolo), quindi va sempre distanziata.
                await asyncio.sleep(1.5)

            # Piccola pausa "cortese" tra una ricerca e l'altra verso Vinted.
            await asyncio.sleep(random.uniform(2, 4))


async def poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wrapper con "watchdog": garantisce che un ciclo di controllo non
    possa mai bloccare il bot per sempre. Se qualcosa si blocca oltre
    JOB_TIMEOUT_SECONDS (es. una richiesta di rete che non risponde né va
    in errore), il ciclo viene interrotto e si riprova al prossimo giro,
    invece di lasciare il bot silenzioso a tempo indeterminato."""
    try:
        await asyncio.wait_for(_run_poll_cycle(context), timeout=JOB_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.error(
            "Il ciclo di controllo ha superato %ss ed è stato interrotto. Riprovo al prossimo giro.",
            JOB_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.exception("Errore imprevisto nel ciclo di controllo periodico.")


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler(["start"], cmd_start))
    app.add_handler(CommandHandler(["aiuto", "help"], cmd_help))
    app.add_handler(CommandHandler(["aggiungi", "add"], cmd_add))
    app.add_handler(CommandHandler(["lista", "list"], cmd_list))
    app.add_handler(CommandHandler(["rimuovi", "remove"], cmd_remove))
    app.add_handler(CommandHandler(["pausa", "pause"], cmd_pause))
    app.add_handler(CommandHandler(["riprendi", "resume"], cmd_resume))

    app.job_queue.run_repeating(poll_job, interval=POLL_INTERVAL_SECONDS, first=10)

    logger.info(
        "Bot avviato. Intervallo di polling: %ss. Ore silenziose: %02d:00-%02d:00 (%s).",
        POLL_INTERVAL_SECONDS, QUIET_HOURS_START, QUIET_HOURS_END, TIMEZONE,
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
