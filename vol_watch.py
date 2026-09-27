#!/usr/bin/env python3
"""Alerte mail : Paris -> New York sur une fenetre de dates.
Depart entre DEPART_MIN et RETOUR_MAX - DUREE_MIN, retour au plus tard RETOUR_MAX,
sejour >= DUREE_MIN jours ; alerte si aller <= SEUIL ET retour <= SEUIL."""
import os
import smtplib
import sqlite3
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

import requests

SEUIL_TRAJET = 300           # € max billet aller ET € max billet retour
DEPART_MIN = "2027-10-15"    # premier jour de depart possible
RETOUR_MAX = "2027-11-05"    # dernier jour de retour possible
DUREE_MIN = 8                # sejour minimum en jours
PAS_JOURS = 6                # echantillonnage des dates (1 test tous les N jours)

SERPAPI_KEY = os.environ["SERPAPI_KEY"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
ALERT_TO = os.environ["ALERT_TO"]

URL = "https://serpapi.com/search.json"
PARIS = "CDG,ORY"
NY = "JFK,EWR,LGA"
DB = "prix.db"


def jours(debut, fin, pas):
    """Liste de dates ISO de debut a fin (inclus) par pas de `pas` jours."""
    d, f, out = date.fromisoformat(debut), date.fromisoformat(fin), []
    while d <= f:
        out.append(d.isoformat())
        d += timedelta(days=pas)
    return out


def trajet(departure_id, arrival_id, outbound_date):
    """Aller simple : {prix, lien, compagnie, h_dep, h_arr}, ou None si prix indisponible."""
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
    compagnie = h_dep = h_arr = ""
    moins_cher = min((v for v in vols if v.get("price")), key=lambda v: v["price"], default=None)
    if moins_cher:
        if prix is None:
            prix = moins_cher["price"]
        segs = moins_cher.get("flights", [])
        compagnie = ", ".join(dict.fromkeys(
            s.get("airline", "") for s in segs if s.get("airline")))
        if segs:
            h_dep = (segs[0].get("departure_airport", {}).get("time", "") or "")[11:16]
            h_arr = (segs[-1].get("arrival_airport", {}).get("time", "") or "")[11:16]
    if prix is None:
        return None
    return {"prix": prix, "lien": lien, "compagnie": compagnie, "h_dep": h_dep, "h_arr": h_arr}


def envoyer_mail(b):
    nuits = (date.fromisoformat(b["retour_date"]) - date.fromisoformat(b["aller_date"])).days
    a, r = b["aller"], b["retour"]
    msg = EmailMessage()
    msg["Subject"] = f"✈️ Paris-NY {b['total']} € A/R ({a['prix']} + {r['prix']})"
    msg["From"] = GMAIL_USER
    msg["To"] = ALERT_TO
    msg.set_content(
        f"Paris -> New York aller/retour : {b['total']} € — {nuits} jours sur place\n\n"
        f"Aller  {b['aller_date']} {a['h_dep']}→{a['h_arr']} : {a['prix']} € — {a['compagnie']}\n{a['lien']}\n\n"
        f"Retour {b['retour_date']} {r['h_dep']}→{r['h_arr']} : {r['prix']} € — {r['compagnie']}\n{r['lien']}\n"
    )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        s.send_message(msg)


def main():
    dernier_depart = (date.fromisoformat(RETOUR_MAX) - timedelta(days=DUREE_MIN)).isoformat()
    premier_retour = (date.fromisoformat(DEPART_MIN) + timedelta(days=DUREE_MIN)).isoformat()

    # Prix aller/retour les moins chers sur chaque date echantillonnee (sous le seuil).
    allers = {}
    for d in jours(DEPART_MIN, dernier_depart, PAS_JOURS):
        v = trajet(PARIS, NY, d)
        if v and v["prix"] <= SEUIL_TRAJET:
            allers[d] = v
    retours = {}
    for d in jours(premier_retour, RETOUR_MAX, PAS_JOURS):
        v = trajet(NY, PARIS, d)
        if v and v["prix"] <= SEUIL_TRAJET:
            retours[d] = v

    # Meilleure combinaison respectant le sejour minimum.
    best = None
    for da, va in allers.items():
        for dr, vr in retours.items():
            if (date.fromisoformat(dr) - date.fromisoformat(da)).days < DUREE_MIN:
                continue
            total = round(va["prix"] + vr["prix"], 2)
            if best is None or total < best["total"]:
                best = {"aller_date": da, "retour_date": dr, "aller": va, "retour": vr, "total": total}

    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE IF NOT EXISTS historique (ts TEXT, aller_date TEXT, retour_date TEXT, total REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS alertes (ts TEXT, total REAL)")
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if best is None:
        print("Aucune combinaison sous le seuil (normal si les prix ne sont pas encore dispos).")
        db.commit()
        db.close()
        return

    db.execute("INSERT INTO historique VALUES (?,?,?,?)",
               (ts, best["aller_date"], best["retour_date"], best["total"]))
    db.commit()
    print(f"Meilleur A/R : {best['aller_date']} -> {best['retour_date']} = {best['total']} €")

    plus_bas = db.execute("SELECT MIN(total) FROM alertes").fetchone()[0]
    if plus_bas is not None and best["total"] >= plus_bas:
        print(f"Deja alerte a {plus_bas} € ou moins, pas de nouveau mail.")
        db.close()
        return

    envoyer_mail(best)
    db.execute("INSERT INTO alertes VALUES (?,?)", (ts, best["total"]))
    db.commit()
    db.close()
    print("Mail envoye.")


if __name__ == "__main__":
    main()
