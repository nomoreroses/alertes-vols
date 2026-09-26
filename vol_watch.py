#!/usr/bin/env python3
"""Alerte mail : Paris -> New York quand aller <= 300 EUR ET retour <= 300 EUR."""
import os
import smtplib
import sqlite3
from datetime import datetime, timezone
from email.message import EmailMessage

import requests

SEUIL_TRAJET = 300          # € max billet aller ET € max billet retour
ALLER = "2027-10-18"
RETOUR = "2027-11-01"

SERPAPI_KEY = os.environ["SERPAPI_KEY"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ALERT_TO = os.environ["ALERT_TO"]

URL = "https://serpapi.com/search.json"
DB = "prix.db"


def trajet(departure_id, arrival_id, outbound_date):
    """Aller simple : {prix, lien, compagnie}, ou None si prix indisponible."""
    params = {
        "engine": "google_flights",
        "type": 2,
        "departure_id": departure_id,
        "arrival_id": arrival_id,
        "outbound_date": outbound_date,
        "currency": "EUR",
        "gl": "fr",
        "hl": "fr",
        "api_key": SERPAPI_KEY,
    }
    data = requests.get(URL, params=params, timeout=60).json()
    if "error" in data:
        print(f"[serpapi] {departure_id}->{arrival_id} {outbound_date}: {data['error']}")
        return None
    lien = data.get("search_metadata", {}).get("google_flights_url")
    vols = data.get("best_flights", []) + data.get("other_flights", [])
    prix = (data.get("price_insights") or {}).get("lowest_price")
    compagnie = ""
    moins_cher = min((v for v in vols if v.get("price")), key=lambda v: v["price"], default=None)
    if moins_cher:
        if prix is None:
            prix = moins_cher["price"]
        compagnie = ", ".join(dict.fromkeys(
            s.get("airline", "") for s in moins_cher.get("flights", []) if s.get("airline")))
    if prix is None:
        print(f"[serpapi] {departure_id}->{arrival_id} {outbound_date}: prix indisponible")
        return None
    return {"prix": prix, "lien": lien, "compagnie": compagnie}


def envoyer_mail(aller, retour, total):
    msg = EmailMessage()
    msg["Subject"] = f"✈️ Paris-NY {total} € A/R ({aller['prix']} + {retour['prix']})"
    msg["From"] = GMAIL_USER
    msg["To"] = ALERT_TO
    msg.set_content(
        f"Paris -> New York aller/retour : {total} €\n\n"
        f"Aller  {ALLER} : {aller['prix']} € — {aller['compagnie']}\n{aller['lien']}\n\n"
        f"Retour {RETOUR} : {retour['prix']} € — {retour['compagnie']}\n{retour['lien']}\n"
    )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(msg)


def main():
    aller = trajet("CDG,ORY", "JFK,EWR,LGA", ALLER)
    retour = trajet("JFK,EWR,LGA", "CDG,ORY", RETOUR)
    if not aller or not retour:
        print("Prix incomplet, on s'arrete (normal avant ~dec. 2026).")
        return

    total = aller["prix"] + retour["prix"]
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE IF NOT EXISTS historique (ts TEXT, prix_aller REAL, prix_retour REAL, total REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS alertes (ts TEXT, total REAL)")
    db.execute("INSERT INTO historique VALUES (?,?,?,?)", (ts, aller["prix"], retour["prix"], total))
    db.commit()
    print(f"Aller {aller['prix']} € / Retour {retour['prix']} € / Total {total} €")

    if aller["prix"] > SEUIL_TRAJET or retour["prix"] > SEUIL_TRAJET:
        print("Au-dessus du seuil, pas d'alerte.")
        db.close()
        return

    plus_bas = db.execute("SELECT MIN(total) FROM alertes").fetchone()[0]
    if plus_bas is not None and total >= plus_bas:
        print(f"Deja alerte a {plus_bas} € ou moins, pas de nouveau mail.")
        db.close()
        return

    envoyer_mail(aller, retour, total)
    db.execute("INSERT INTO alertes VALUES (?,?)", (ts, total))
    db.commit()
    db.close()
    print("Mail envoye.")


if __name__ == "__main__":
    main()
