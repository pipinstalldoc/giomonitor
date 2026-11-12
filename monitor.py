"""Monitor Mega Comics -> Discord.

- Notifica i nuovi prodotti e i prodotti tornati disponibili.
- Filtra per Pokemon, Dragon Ball e One Piece.
- Ricorda i prodotti tra un avvio manuale e l'altro tramite stato.json.

Uso su Windows:
    python monitor.py --once

Al primo avvio crea una fotografia del catalogo senza inviare notifiche.
Dagli avvii successivi segnala solo nuovi prodotti corrispondenti ai filtri e restock.
Impostare DISCORD_WEBHOOK come variabile d'ambiente prima dell'esecuzione.
"""
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ===================== IMPOSTAZIONI =====================
BASE = "https://megacomics.eu"

# Per impostazione predefinita stato.json viene salvato accanto a questo script,
# anche se il comando viene lanciato da un'altra cartella.
DEFAULT_STATE_FILE = Path(__file__).resolve().with_name("stato.json")
STATE_FILE = Path(os.environ.get("STATE_FILE", str(DEFAULT_STATE_FILE))).expanduser()
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK", "").strip()

PAUSA_PAGINE = 1.5
PAROLE_CHIAVE = ["pokemon", "dragon ball", "one piece"]
ESCLUDI = []  # es. ["manga", "volume"] per scartare i fumetti
NOTIFICA_NUOVI = True
NOTIFICA_RESTOCK = True

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ProductMonitor/1.0)"}
# ========================================================


def normalizza(testo):
    testo = unicodedata.normalize("NFKD", testo)
    testo = "".join(c for c in testo if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", testo.lower())


CHIAVI = [normalizza(k) for k in PAROLE_CHIAVE]
ESCLUSI = [normalizza(k) for k in ESCLUDI]


def interessa(prodotto):
    testo = normalizza(prodotto["nome"] + " " + " ".join(prodotto.get("categorie", [])))
    if any(parola in testo for parola in ESCLUSI):
        return False
    return any(parola in testo for parola in CHIAVI)


# ---------- LETTURA CATALOGO (API WooCommerce) ----------
def tutto_da_api():
    prodotti, pagina = [], 1
    while True:
        risposta = requests.get(
            f"{BASE}/wp-json/wc/store/v1/products",
            params={"per_page": 100, "page": pagina},
            headers=HEADERS,
            timeout=30,
        )
        risposta.raise_for_status()
        dati = risposta.json()
        if not isinstance(dati, list):
            raise RuntimeError("La risposta dell'API WooCommerce non è una lista di prodotti.")

        for p in dati:
            prezzi = p.get("prices") or {}
            minor = int(prezzi.get("currency_minor_unit", 2))
            valore_prezzo = prezzi.get("price") or "0"
            prezzo = int(valore_prezzo) / (10 ** minor)
            immagini = p.get("images") or []
            prodotti.append({
                "id": str(p["id"]),
                "nome": p.get("name", "Prodotto senza nome"),
                "url": p.get("permalink", BASE),
                "prezzo": f"€{prezzo:.2f}",
                "img": immagini[0].get("src") if immagini else None,
                "disponibile": bool(p.get("is_in_stock", True)),
                "categorie": [c.get("name", "") for c in p.get("categories", [])],
            })

        if len(dati) < 100:
            break
        pagina += 1
        time.sleep(PAUSA_PAGINE)
    return prodotti


# ---------- LETTURA CATALOGO (fallback HTML) ----------
def pagina_html(numero):
    url_pagina = f"{BASE}/shop/" if numero == 1 else f"{BASE}/shop/page/{numero}/"
    risposta = requests.get(url_pagina, headers=HEADERS, timeout=30)
    if risposta.status_code == 404:
        return None
    risposta.raise_for_status()
    soup = BeautifulSoup(risposta.text, "html.parser")
    prodotti, visti = [], set()

    for link in soup.select("h2 a[href*='/product/']"):
        url_prodotto = link.get("href", "")
        if not url_prodotto or url_prodotto in visti:
            continue
        visti.add(url_prodotto)
        box = link.find_parent("li") or link.parent
        testo = box.get_text(" ")
        match_prezzo = re.search(r"€\s?[\d.,]+", testo)
        immagine = box.find("img")
        src = (immagine.get("data-src") or immagine.get("src")) if immagine else None
        prodotti.append({
            "id": url_prodotto,
            "nome": link.get_text(strip=True),
            "url": url_prodotto,
            "prezzo": match_prezzo.group(0) if match_prezzo else "n/d",
            "img": src if src and src.startswith("http") else None,
            "disponibile": "esaurito" not in testo.lower(),
            "categorie": [],
        })
    return prodotti


def tutto_da_html():
    prodotti, numero = [], 1
    while True:
        pagina = pagina_html(numero)
        if not pagina:
            break
        prodotti.extend(pagina)
        numero += 1
        time.sleep(PAUSA_PAGINE)
    return prodotti


def prendi_prodotti():
    try:
        return tutto_da_api()
    except Exception as errore_api:
        print(f"API non disponibile, provo con il catalogo HTML: {errore_api}")
        prodotti_html = tutto_da_html()
        if not prodotti_html:
            raise RuntimeError("Non è stato possibile leggere il catalogo né tramite API né tramite HTML.")
        return prodotti_html


# ---------- DISCORD ----------
def notifica(prodotto, titolo, colore):
    if not WEBHOOK_URL:
        raise RuntimeError(
            "Webhook Discord non configurato. Imposta la variabile d'ambiente DISCORD_WEBHOOK."
        )

    embed = {
        "title": prodotto["nome"][:256],
        "url": prodotto["url"],
        "color": colore,
        "fields": [
            {"name": "Prezzo", "value": prodotto["prezzo"], "inline": True},
            {
                "name": "Stato",
                "value": "✅ Disponibile" if prodotto["disponibile"] else "❌ Esaurito",
                "inline": True,
            },
        ],
        "footer": {"text": "Mega Comics"},
    }
    if prodotto.get("img"):
        embed["thumbnail"] = {"url": prodotto["img"]}

    payload = {"content": titolo, "embeds": [embed]}
    risposta = requests.post(WEBHOOK_URL, json=payload, timeout=15)
    if risposta.status_code == 429:
        try:
            attesa = float(risposta.json().get("retry_after", 2))
        except (ValueError, TypeError, json.JSONDecodeError):
            attesa = 2
        time.sleep(max(attesa, 1))
        risposta = requests.post(WEBHOOK_URL, json=payload, timeout=15)
    risposta.raise_for_status()
    time.sleep(1)


# ---------- MEMORIA PERSISTENTE ----------
def stato_vuoto():
    return {"version": 1, "products": {}}


def carica_stato():
    if not STATE_FILE.exists():
        return stato_vuoto()

    with STATE_FILE.open("r", encoding="utf-8") as file:
        dati = json.load(file)

    # Formato nuovo: {"version": 1, "products": {id: {"disponibile": bool, ...}}}
    if isinstance(dati, dict) and isinstance(dati.get("products"), dict):
        return dati

    # Compatibilità con il vecchio stato.json, che conteneva {id: disponibile}.
    if isinstance(dati, dict):
        prodotti = {}
        for pid, disponibile in dati.items():
            if isinstance(disponibile, bool):
                prodotti[str(pid)] = {"disponibile": disponibile}
        print("Memoria precedente rilevata e convertita nel nuovo formato.")
        return {"version": 1, "products": prodotti}

    raise RuntimeError("Il file stato.json non ha un formato riconosciuto. Non è stato sovrascritto.")


def salva_stato(stato):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporaneo = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    with temporaneo.open("w", encoding="utf-8") as file:
        json.dump(stato, file, ensure_ascii=False, indent=2)
    temporaneo.replace(STATE_FILE)


# ---------- CICLO DI CONTROLLO ----------
def controlla(stato, primo_avvio):
    prodotti = prendi_prodotti()
    if not prodotti:
        raise RuntimeError("Il catalogo ha restituito zero prodotti: memoria non modificata.")

    memoria = stato["products"]
    nuovi_trovati = 0
    restock_trovati = 0

    # Al primo avvio viene creata una fotografia iniziale: non invia notifiche arretrate.
    if primo_avvio:
        for p in prodotti:
            memoria[p["id"]] = {
                "disponibile": p["disponibile"],
                "nome": p["nome"],
                "url": p["url"],
            }
        salva_stato(stato)
        print(f"Prima scansione completata: memorizzati {len(prodotti)} prodotti, nessuna notifica inviata.")
        print(f"File memoria: {STATE_FILE}")
        return

    for p in prodotti:
        pid = str(p["id"])
        prima = memoria.get(pid)
        disponibile_ora = bool(p["disponibile"])

        # Notifica un prodotto nuovo solo se corrisponde ai filtri.
        if prima is None and interessa(p) and NOTIFICA_NUOVI:
            notifica(p, "🆕 Nuovo prodotto!", 0x5865F2)
            nuovi_trovati += 1
        # Notifica il restock di un prodotto già noto.
        elif (
            prima is not None
            and prima.get("disponibile") is False
            and disponibile_ora
            and interessa(p)
            and NOTIFICA_RESTOCK
        ):
            notifica(p, "🔔 Tornato disponibile!", 0x57F287)
            restock_trovati += 1

        # Salva dopo ogni prodotto elaborato: se una notifica fallisce, quel prodotto
        # non viene aggiornato e potrà essere riprovato al successivo avvio.
        memoria[pid] = {
            "disponibile": disponibile_ora,
            "nome": p["nome"],
            "url": p["url"],
        }
        salva_stato(stato)

    print(f"Controllo completato: {len(prodotti)} prodotti analizzati.")
    print(f"Nuovi prodotti notificati: {nuovi_trovati}; restock notificati: {restock_trovati}.")
    print(f"Memoria aggiornata: {STATE_FILE}")


def main():
    # Su PC Windows puoi avviare il bot manualmente con: python monitor.py --once
    una_volta = "--once" in sys.argv or len(sys.argv) == 1

    try:
        stato = carica_stato()
        primo_avvio = not bool(stato.get("products"))
        controlla(stato, primo_avvio)
    except Exception as errore:
        print(f"ERRORE: {errore}")
        sys.exit(1)

    if not una_volta:
        print("Nota: questa versione è impostata per un controllo manuale per esecuzione.")


if __name__ == "__main__":
    main()
