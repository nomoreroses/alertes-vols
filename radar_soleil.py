#!/usr/bin/env python3
"""Radar ete 2027 : Rennes/Nantes -> ville soleil, A/R sans escale,
aller <= 50 EUR ET retour <= 50 EUR, sejour de 7 a 21 jours.
Deux sources : Google Flights (via SerpApi) + Ryanair (API publique, gratuite)."""
import os
import smtplib
import sqlite3
from datetime import date, datetime, timezone
from email.message import EmailMessage

import requests

SEUIL_TRAJET = 50                      # € max billet aller ET € max billet retour
FENETRE = "2027-07-01,2027-09-03"      # dates de depart possibles
FIN = "2027-09-10"                     # retour au plus tard
DUREE = "7,21"                         # duree du sejour en jours
MAX_VERIFS = 3                         # verifications SerpApi detaillees max par passage
DEPARTS = ["NTE", "RNS"]               # aeroports de depart
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
RYANAIR_URL = "https://services-api.ryanair.com/farfnd/v4/roundTripFares"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")
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
        "departure_id": ",".join(DEPARTS),
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


def ryanair_lien(origine, dest, date_out, date_in):
    return ("https://www.ryanair.com/fr/fr/trip/flights/select?adults=1"
            f"&isReturn=true&originIata={origine}&destinationIata={dest}"
            f"&dateOut={date_out}&dateIn={date_in}")


def ryanair(depart):
    """Interroge Ryanair pour un aeroport de depart ; renvoie une liste de bons plans normalises."""
    debut, fin_dep = FENETRE.split(",")
    dmin, dmax = DUREE.split(",")
    params = {
        "departureAirportIataCode": depart,
        "outboundDepartureDateFrom": debut,
        "outboundDepartureDateTo": fin_dep,
        "inboundDepartureDateFrom": debut,
        "inboundDepartureDateTo": FIN,
        "durationFrom": dmin,
        "durationTo": dmax,
        "priceValueTo": 2 * SEUIL_TRAJET,   # total A/R <= 2 * seuil (on filtre chaque trajet ensuite)
        "currency": "EUR",
        "market": "fr-fr",
    }
    try:
        r = requests.get(RYANAIR_URL, params=params,
                         headers={"User-Agent": UA, "Accept": "application/json"}, timeout=60)
        fares = r.json().get("fares", []) or []
    except (requests.RequestException, ValueError) as e:
        print(f"[ryanair] {depart}: {e}")
        return []

    trouves = []
    for f in fares:
        out, inb = f.get("outbound"), f.get("inbound")
        if not out or not inb:
            continue
        code = out["arrivalAirport"]["iataCode"]
        if code not in VILLES_SOLEIL:
            continue
        start = out["departureDate"][:10]
        end = inb["departureDate"][:10]
        if end > FIN:
            continue
        p_aller = out["price"]["value"]
        p_retour = inb["price"]["value"]
        if p_aller > SEUIL_TRAJET or p_retour > SEUIL_TRAJET:
            continue
        lien = ryanair_lien(depart, code, start, end)
        trouves.append({
            "code": code, "ville": VILLES_SOLEIL[code], "start": start, "end": end,
            "depart": depart,
            "aller": {"prix": p_aller, "lien": lien, "compagnie": "Ryanair",
                      "h_dep": out["departureDate"][11:16], "h_arr": out["arrivalDate"][11:16]},
            "retour": {"prix": p_retour, "lien": lien, "compagnie": "Ryanair",
                       "h_dep": inb["departureDate"][11:16], "h_arr": inb["arrivalDate"][11:16]},
            "total": round(p_aller + p_retour, 2),
        })
    return trouves


def google_flights():
    """Bons plans via Google Flights (SerpApi) : deals + verification detaillee."""
    candidats = []
    for d in deals():
        code = d.get("arrival_airport_code")
        if code not in VILLES_SOLEIL:
            continue
        if (d.get("end_date") or "") > FIN:
            continue
        candidats.append(d)

    trouves = []
    for d in candidats[:MAX_VERIFS]:
        code = d["arrival_airport_code"]
        start, end = d.get("start_date"), d.get("end_date")
        aller = trajet(",".join(DEPARTS), code, start)
        retour = trajet(code, ",".join(DEPARTS), end)
        if not aller or not retour:
            continue
        if aller["prix"] > SEUIL_TRAJET or retour["prix"] > SEUIL_TRAJET:
            continue
        trouves.append({
            "code": code, "ville": VILLES_SOLEIL[code], "start": start, "end": end,
            "depart": d.get("departure_airport_code", ""),
            "aller": aller, "retour": retour, "total": round(aller["prix"] + retour["prix"], 2),
        })
    return trouves


def envoyer_mail(trouves):
    lignes = []
    for t in trouves:
        nuits = (date.fromisoformat(t["end"]) - date.fromisoformat(t["start"])).days
        a, r = t["aller"], t["retour"]
        lignes.append(
            f"☀️ {t['ville']} ({t['code']}) — {t['total']} € A/R depuis {t['depart']} — {nuits} jours sur place\n"
            f"  Aller  {t['start']} {a['h_dep']}→{a['h_arr']} : {a['prix']} € — {a['compagnie']}\n  {a['lien']}\n"
            f"  Retour {t['end']} {r['h_dep']}→{r['h_arr']} : {r['prix']} € — {r['compagnie']}\n  {r['lien']}\n"
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

    # Collecte des deux sources.
    tous = google_flights()
    for depart in DEPARTS:
        tous += ryanair(depart)

    # Garder le moins cher par destination.
    meilleur = {}
    for t in tous:
        if t["code"] not in meilleur or t["total"] < meilleur[t["code"]]["total"]:
            meilleur[t["code"]] = t

    # Anti-spam : jamais alerte OU total plus bas qu'avant.
    trouves = [t for t in meilleur.values()
               if t["code"] not in deja or t["total"] < deja[t["code"]]]

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
