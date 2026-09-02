# Bot Telegram per notifiche Vinted

Bot Telegram che monitora una o più ricerche Vinted (con tutti i filtri e
l'ordinamento che hai impostato tu, es. "Più recenti") e ti avvisa con
foto, prezzo, tasse e link non appena viene pubblicato un nuovo annuncio.

## Come funziona (in breve)

Vinted **non ha un'API pubblica ufficiale** per questo scopo. Questo bot usa
[`vinted-api-kit`](https://pypi.org/project/vinted-api-kit/), una libreria
open source che si appoggia alla stessa API "interna" usata dal sito web di
Vinted, gestendo automaticamente i cookie di sessione richiesti dal loro
sistema anti-bot (DataDome).

Due conseguenze pratiche da sapere prima di usarlo:

1. **Può capitare un errore 403/429 ogni tanto.** Vinted rallenta o blocca
   temporaneamente le richieste che sembrano "da bot", soprattutto se il
   bot gira su un IP da datacenter (molti VPS economici). Funziona meglio
   se lo fai girare da casa (es. un Raspberry Pi, un mini-PC, o anche il
   tuo PC/NAS sempre acceso). Il bot gestisce questi errori senza crashare
   e riprova al ciclo successivo.
2. **Il costo di spedizione esatto non è quasi mai disponibile.** Dipende
   dall'indirizzo dell'acquirente e dal metodo di spedizione scelto, quindi
   Vinted non lo espone in modo affidabile tramite questa API pubblica. Il
   bot mostra sempre **prezzo dell'articolo** e, quando disponibile,
   **tasse di Protezione Acquisti** e **totale**; per la spedizione, se non
   riesce a trovarla nella risposta, ti rimanda al link dell'annuncio.

Se preferisci una soluzione già pronta e più robusta (gestione proxy,
interfaccia web, RSS, ecc.) invece di gestire codice tuo, dai un'occhiata
anche a [Fuyucch1/Vinted-Notifications](https://github.com/Fuyucch1/Vinted-Notifications),
un progetto open source che fa esattamente questo ed è installabile con
Docker in pochi minuti.

## Requisiti

- Python 3.10 o superiore
- Un bot Telegram (token da [@BotFather](https://t.me/BotFather))

## Installazione locale

```bash
cd vinted_telegram_bot
python3 -m venv venv
source venv/bin/activate        # su Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Crea il tuo bot Telegram:

1. Apri una chat con [@BotFather](https://t.me/BotFather) su Telegram.
2. Manda `/newbot` e segui le istruzioni: ti darà un **token** tipo
   `123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`.
3. Cerca il tuo bot appena creato e manda `/start` (serve per poter
   ricevere messaggi da lui).

Configura il bot:

```bash
cp .env.example .env
```

Apri `.env` e imposta almeno `TELEGRAM_BOT_TOKEN`. Facoltativo ma
consigliato: imposta `ALLOWED_CHAT_IDS` con il tuo `chat_id` (lo trovi
scrivendo a [@userinfobot](https://t.me/userinfobot)), così solo tu potrai
usare il bot.

Avvia il bot:

```bash
python bot.py
```

Lascialo acceso (o usa Docker/systemd, vedi sotto) per ricevere le
notifiche in tempo reale.

## Uso

1. Su Vinted, imposta la ricerca con tutti i filtri che vuoi (categoria,
   taglia, prezzo, ecc.) e **ordina per "Più recenti"**.
2. Copia l'URL completo dalla barra degli indirizzi, tipo:
   ```
   https://www.vinted.it/catalog?search_text=nike&catalog[]=5&size_ids[]=208&status_ids[]=6&order=newest_first&price_to=40&currency=EUR
   ```
3. Nella chat con il bot, manda:
   ```
   /aggiungi https://www.vinted.it/catalog?search_text=nike&...
   ```
   Puoi anche mandare più URL insieme, separati da spazio o a capo, per
   aggiungerli tutti in una volta.
4. Il bot carica gli annunci già presenti come "base di partenza" (senza
   notificarli, per non riempirti la chat con la cronologia esistente),
   poi ti avvisa solo dei **nuovi** annunci pubblicati da quel momento.

Altri comandi:

| Comando | Effetto |
|---|---|
| `/lista` | Mostra le tue ricerche attive con il loro id |
| `/rimuovi <id>` | Elimina una ricerca |
| `/pausa <id>` | Sospende le notifiche senza eliminare la ricerca |
| `/riprendi <id>` | Riattiva una ricerca in pausa |
| `/aiuto` | Guida rapida |

## Esecuzione con Docker (consigliata per tenerlo sempre acceso)

```bash
cp .env.example .env   # e modificalo con il tuo token
docker compose up -d --build
```

I dati (database SQLite e cookie di sessione) vengono salvati nella
cartella `./data`, montata come volume, così sopravvivono ai riavvii del
container.

## Esecuzione come servizio (systemd, alternativa a Docker)

Esempio di unit file `/etc/systemd/system/vinted-bot.service`:

```ini
[Unit]
Description=Vinted Telegram Bot
After=network.target

[Service]
WorkingDirectory=/percorso/a/vinted_telegram_bot
ExecStart=/percorso/a/vinted_telegram_bot/venv/bin/python bot.py
Restart=always
EnvironmentFile=/percorso/a/vinted_telegram_bot/.env

[Install]
WantedBy=multi-user.target
```

Poi:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vinted-bot
```

## Struttura del progetto

```
vinted_telegram_bot/
├── bot.py              # comandi Telegram + ciclo di polling periodico
├── vinted_watcher.py    # interrogazione Vinted e formattazione messaggi
├── storage.py           # persistenza SQLite (ricerche + articoli già visti)
├── requirements.txt
├── .env.example
├── Dockerfile
├── docker-compose.yml
└── data/                # creata automaticamente: database + cookie
```

## Personalizzazioni comuni

- **Intervallo di controllo**: variabile `POLL_INTERVAL_SECONDS` in `.env`
  (default 90s). Più basso = notifiche più rapide ma più richieste a
  Vinted → più rischio di rallentamenti anti-bot.
- **Numero di annunci letti per ciclo**: `ITEMS_PER_PAGE` (default 20,
  massimo 96). Aumentalo solo se la tua ricerca produce molti annunci al
  minuto.
- **Restringere l'uso del bot**: `ALLOWED_CHAT_IDS` in `.env`.

## Limitazioni note

- Nessuna API pubblica ufficiale: possibili blocchi temporanei da parte del
  sistema anti-bot di Vinted, soprattutto da IP di datacenter/VPS.
- Il costo di spedizione preciso spesso non è recuperabile via API
  pubblica (vedi sopra).
- Il bot legge solo la prima pagina di risultati (di solito sufficiente,
  dato che è ordinata dal più recente); se una ricerca produce più di
  `ITEMS_PER_PAGE` nuovi annunci nell'intervallo tra due controlli, quelli
  in eccesso potrebbero non essere notificati singolarmente.
