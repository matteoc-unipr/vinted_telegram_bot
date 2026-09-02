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
from pathlib import Path
from urllib.parse import unquote_plus

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
                raw_items = await vw.poll_search(client, search, ITEMS_PER_PAGE)
                storage.mark_seen(search_id, [str(it.get("id")) for it in raw_items])
                storage.mark_baseline_done(search_id)
                logger.info(
                    "Baseline completata per ricerca #%s (%d annunci di partenza)",
                    search_id, len(raw_items),
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


async def poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job periodico: controlla tutte le ricerche attive e notifica i nuovi
    annunci. Usa un unico VintedClient per l'intero ciclo, così i cookie di
    sessione vengono riutilizzati invece di essere richiesti ad ogni ricerca."""
    searches = [s for s in storage.list_active_searches() if s.baseline_done]
    if not searches:
        return

    async with VintedClient(persist_cookies=True, cookies_dir=COOKIES_DIR) as client:
        for search in searches:
            try:
                raw_items = await vw.poll_search(client, search, ITEMS_PER_PAGE)
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
                    info = await vw.build_item_info(client, raw_item)
                    caption = vw.format_caption(info, search.name)
                    if info.photo_url:
                        await context.bot.send_photo(
                            chat_id=search.chat_id,
                            photo=info.photo_url,
                            caption=caption,
                            parse_mode=ParseMode.HTML,
                        )
                    else:
                        await context.bot.send_message(
                            chat_id=search.chat_id,
                            text=caption,
                            parse_mode=ParseMode.HTML,
                        )
                except Exception:
                    logger.exception(
                        "Errore inviando la notifica per l'articolo %s", raw_item.get("id")
                    )
                await asyncio.sleep(1.5)  # non intasare Telegram

            # Piccola pausa "cortese" tra una ricerca e l'altra verso Vinted.
            await asyncio.sleep(random.uniform(2, 4))


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

    logger.info("Bot avviato. Intervallo di polling: %ss", POLL_INTERVAL_SECONDS)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
