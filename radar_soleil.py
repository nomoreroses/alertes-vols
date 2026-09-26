#!/usr/bin/env python3
"""Radar ete 2027 : Rennes/Nantes -> ville soleil, A/R sans escale,
aller <= 50 EUR ET retour <= 50 EUR, sejour de 7 a 21 jours."""
import os
import smtplib
import sqlite3
from datetime import datetime, timezone
from email.message import EmailMessage

import requests

SEUIL_TRAJET = 50                      # € max billet aller ET € max billet retour
FENETRE = "2027-07-01,2027-09-03"      # dates de depart possibles
FIN = "2027-09-10"                     # retour au plus tard
DUREE = "7,21"                         # duree du sejour en jours
MAX_VERIFS = 3                         # verifications detaillees max par passage
VILLES_SOLEIL = {
    "PMI": "Palma", "IBZ": "Ibiza", "AGP": "Malaga", "ALC": "Alicante",
    "BCN": "Barcelone", "LPA": "Gran Canaria – Las Palmas",
    "TFS": "Tenerife – Los Cristianos", "ACE": "Lanzarote", "FUE": "Fuerteventura",
    "CHQ": "La Canee", "RHO": "Rhodes", "SPU": "Split", "VAR": "Varna",
    "LCA": "Larnaca", "HRG": "Hurghada", "AGA": "Agadir", "ESU": "Essaouira",
    "TNG": "Tanger", "MIR": "Monastir", "SID": "Sal – Santa Maria",
    "BVC": "Boa Vista – Sal Rei", "AJA": "Ajaccio", "CLY": "Calvi",
    "MRS": "Marseille – Catalans", "TLN": "Toulon – Mourillon",
}

SERPAPI_KEY = os.environ["SERPAPI_KEY"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ALERT_TO = os.environ["ALERT_TO"]

URL = "https://serpapi.com/search.json"
DB = "radar.db"


def get(params):
    params = {**params, "currency": "EUR", "gl": "fr", "hl": "fr", "api_key": SERPAPI_KEY}
    data = requests.get(URL, params=params, timeout=60).json()
    if "error" in data:
        print(f"[serpapi] {data['error']}")
        return None
    return data


def deals():
    data = get({
        "engine": "google_flights_deals",
        "type": 1,
        "departure_id": "RNS,NTE",
        "outbound_date": FENETRE,
        "trip_length": DUREE,
        "stops": 1,
        "max_price": 2 * SEUIL_TRAJET,
    })
    return (data or {}).get("deals", []) or []


def trajet(departure_id, arrival_id, outbound_date):
    """Aller simple sans escale : {prix, lien, compagnie}, ou None."""
    data = get({
        "engine": "google_flights",
        "type": 2,
        "stops": 1,
        "departure_id": departure_id,
        "arrival_id": arrival_id,
        "outbound_date": outbound_date,
    })
    if data is None:
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
        return None
    return {"prix": prix, "lien": lien, "compagnie": compagnie}


def envoyer_mail(trouves):
    lignes = []
    for t in trouves:
        lignes.append(
            f"☀️ {t['ville']} ({t['code']}) — {t['total']} € A/R depuis {t['depart']}\n"
            f"  Aller  {t['start']} : {t['aller']['prix']} € — {t['aller']['compagnie']}\n  {t['aller']['lien']}\n"
            f"  Retour {t['end']} : {t['retour']['prix']} € — {t['retour']['compagnie']}\n  {t['retour']['lien']}\n"
        )
    msg = EmailMessage()
    msg["Subject"] = f"☀️ {len(trouves)} bons plans soleil"
    msg["From"] = GMAIL_USER
    msg["To"] = ALERT_TO
    msg.set_content("\n".join(lignes))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(msg)


def main():
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE IF NOT EXISTS alertes (code TEXT PRIMARY KEY, total REAL, ts TEXT)")
    deja = dict(db.execute("SELECT code, total FROM alertes").fetchall())

    # Etape 1 : reperer les A/R prometteurs dans les deals.
    candidats = []
    for d in deals():
        code = d.get("arrival_airport_code")
        if code not in VILLES_SOLEIL:
            continue
        if (d.get("end_date") or "") > FIN:
            continue
        prix_deal = d.get("price")
        if code in deja and prix_deal is not None and deja[code] <= prix_deal:
            continue
        candidats.append(d)

    # Etape 2 : verifier chaque billet reellement sous le seuil.
    trouves = []
    for d in candidats[:MAX_VERIFS]:
        code = d["arrival_airport_code"]
        start, end = d.get("start_date"), d.get("end_date")
        aller = trajet("RNS,NTE", code, start)
        retour = trajet(code, "RNS,NTE", end)
        if not aller or not retour:
            continue
        if aller["prix"] > SEUIL_TRAJET or retour["prix"] > SEUIL_TRAJET:
            continue
        total = aller["prix"] + retour["prix"]
        if code in deja and total >= deja[code]:
            continue
        trouves.append({
            "code": code, "ville": VILLES_SOLEIL[code], "start": start, "end": end,
            "depart": d.get("departure_airport_code", ""),
            "aller": aller, "retour": retour, "total": total,
        })

    if not trouves:
        print("Rien de nouveau.")
        db.close()
        return

    envoyer_mail(trouves)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for t in trouves:
        db.execute(
            "INSERT INTO alertes (code, total, ts) VALUES (?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET total=excluded.total, ts=excluded.ts",
            (t["code"], t["total"], ts))
    db.commit()
    db.close()
    print(f"{len(trouves)} bon(s) plan(s), mail envoye.")


if __name__ == "__main__":
    main()
