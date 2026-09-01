#!/usr/bin/env python3
"""
Flutlicht — Datenversorgung

Holt Kader und Spieltagsstatistiken von API-Football, rechnet Fantasy-Punkte
aus und schreibt data/spieler.json, das die Website liest.

Zwei Modi:
  python hole_daten.py kader      einmalig zum Saisonstart: baut die Spielerliste
  python hole_daten.py spieltag   laufend: holt Statistiken und aktualisiert Preise

Der API-Key kommt aus der Umgebungsvariable API_FOOTBALL_KEY und steht
niemals im Code oder im HTML.
"""

import json, os, sys, time, math, pathlib
from datetime import datetime, timedelta, timezone
import requests

# ----------------------------------------------------------------------
# Einstellungen
# ----------------------------------------------------------------------
BASIS = "https://v3.football.api-sports.io"
KEY = os.environ.get("API_FOOTBALL_KEY", "")
SAISON = 2026                      # 2026 = Saison 2026/27

LIGEN = {39: "PL", 140: "LL", 135: "SA", 78: "BL", 61: "L1"}

BUDGET = 500.0                     # Budget pro Manager
MANAGER = 4                        # so viele seid ihr
MARKT_SUMME = BUDGET * MANAGER * 4.5   # Gesamtvolumen des Marktes in Mio

DAEMPFUNG = 0.25                   # wie schnell der Preis dem Zielwert folgt
DECKEL = 0.10                      # maximale Veränderung pro Spieltag
FORM_GEWICHT = 0.6                 # Anteil der letzten 4 Spieltage am Zielwert
EXPONENT = 1.3                     # Spreizung: Topspieler überproportional teuer
VERLETZT_BODEN = 0.70              # Verletzte fallen höchstens auf 70 % zurück

PAUSE = 6.5                        # Sekunden zwischen Requests (Free: 10/Minute)

ORDNER = pathlib.Path("data")
ZIEL = ORDNER / "spieler.json"
CACHE = ORDNER / "_fortschritt.json"

POS_MAP = {"Goalkeeper": "TW", "Defender": "ABW", "Midfielder": "MF", "Attacker": "ST",
           "G": "TW", "D": "ABW", "M": "MF", "F": "ST"}

# Startpreis, wenn ein Spieler noch keine Historie hat
START_PREIS = {"TW": 14.0, "ABW": 16.0, "MF": 20.0, "ST": 22.0}


# ----------------------------------------------------------------------
# Punktesystem — identisch zu dem, was in der App unter "Regeln" steht
# ----------------------------------------------------------------------
def punkte_fuer(st, pos):
    """Rechnet die Fantasy-Punkte eines Spielers in einem Spiel aus."""
    p = 0
    minuten = st["games"].get("minutes") or 0
    if minuten == 0:
        return 0
    p += 2 if minuten > 60 else 1

    tore = st["goals"].get("total") or 0
    p += tore * {"TW": 10, "ABW": 6, "MF": 5, "ST": 4}[pos]
    p += (st["goals"].get("assists") or 0) * 3

    gegentore = st["goals"].get("conceded") or 0
    if pos in ("TW", "ABW") and minuten >= 60 and gegentore == 0:
        p += 4
    elif pos == "MF" and minuten >= 60 and gegentore == 0:
        p += 1
    if pos in ("TW", "ABW"):
        p -= gegentore // 3

    if pos == "TW":
        p += (st["goals"].get("saves") or 0) // 3
        p += (st["penalty"].get("saved") or 0) * 5

    p -= (st["penalty"].get("missed") or 0) * 2
    p -= (st["cards"].get("yellow") or 0) * 1
    p -= (st["cards"].get("red") or 0) * 3
    p -= (st.get("own_goals") or 0) * 2
    return p


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
class QuotaAus(Exception):
    pass


def hole(pfad, **params):
    if not KEY:
        sys.exit("API_FOOTBALL_KEY fehlt. Als Umgebungsvariable setzen.")
    r = requests.get(f"{BASIS}/{pfad}", headers={"x-apisports-key": KEY},
                     params=params, timeout=30)
    if r.status_code == 429:
        raise QuotaAus("Tageslimit erreicht")
    r.raise_for_status()
    d = r.json()
    if d.get("errors"):
        # Ein leeres errors-Feld ist eine Liste, ein gefülltes ein Dict
        if isinstance(d["errors"], dict) and d["errors"]:
            if any("limit" in str(v).lower() for v in d["errors"].values()):
                raise QuotaAus(str(d["errors"]))
            print(f"  Warnung von der API: {d['errors']}", file=sys.stderr)
    rest = r.headers.get("x-ratelimit-requests-remaining")
    if rest is not None and int(rest) <= 1:
        raise QuotaAus("Tageslimit fast aufgebraucht")
    time.sleep(PAUSE)
    return d.get("response", [])


# ----------------------------------------------------------------------
# Speichern und Laden
# ----------------------------------------------------------------------
def lade_stand():
    if ZIEL.exists():
        return json.loads(ZIEL.read_text(encoding="utf-8"))
    return {"stand": None, "spieltag": 0, "spieler": []}


def schreibe(stand):
    ORDNER.mkdir(exist_ok=True)
    stand["stand"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ZIEL.write_text(json.dumps(stand, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {ZIEL} geschrieben, {len(stand['spieler'])} Spieler")


# ----------------------------------------------------------------------
# Modus 1: Kader aufbauen
# ----------------------------------------------------------------------
def modus_kader():
    """
    Holt die aktuellen Kader aller Vereine der fünf Ligen.
    Braucht rund 105 Requests, passt also nicht in ein Free-Tageslimit.
    Das Skript merkt sich den Fortschritt und macht am nächsten Tag weiter —
    einfach nochmal starten.
    """
    ORDNER.mkdir(exist_ok=True)
    fortschritt = json.loads(CACHE.read_text()) if CACHE.exists() else {"teams": {}, "fertig": []}
    spieler = {s["id"]: s for s in lade_stand()["spieler"]}

    try:
        # Vereine je Liga, einmal holen und merken
        for liga_id, kuerzel in LIGEN.items():
            if str(liga_id) not in fortschritt["teams"]:
                print(f"Vereine {kuerzel} …")
                teams = hole("teams", league=liga_id, season=SAISON)
                fortschritt["teams"][str(liga_id)] = [
                    {"id": t["team"]["id"], "name": t["team"]["name"]} for t in teams]
                CACHE.write_text(json.dumps(fortschritt))

        # Kader je Verein
        for liga_id, kuerzel in LIGEN.items():
            for t in fortschritt["teams"][str(liga_id)]:
                if t["id"] in fortschritt["fertig"]:
                    continue
                print(f"Kader {t['name']} ({kuerzel}) …")
                antwort = hole("players/squads", team=t["id"])
                if antwort:
                    for sp in antwort[0].get("players", []):
                        pos = POS_MAP.get(sp.get("position") or "", None)
                        if not pos:
                            continue
                        sid = sp["id"]
                        alt = spieler.get(sid, {})
                        spieler[sid] = {
                            "id": sid,
                            "name": sp["name"],
                            "verein": t["name"],
                            "liga": kuerzel,
                            "pos": pos,
                            "preis": alt.get("preis", START_PREIS[pos]),
                            "punkte": alt.get("punkte", 0.0),
                            "gesamt": alt.get("gesamt", 0),
                            "historie": alt.get("historie", []),
                            "verletzt": False,
                        }
                fortschritt["fertig"].append(t["id"])
                CACHE.write_text(json.dumps(fortschritt))

        CACHE.unlink(missing_ok=True)
        print("Kaderaufbau abgeschlossen.")

    except QuotaAus as e:
        print(f"\n{e}. Fortschritt ist gesichert — morgen einfach nochmal starten.")

    stand = lade_stand()
    stand["spieler"] = list(spieler.values())
    normalisiere(stand["spieler"])
    schreibe(stand)


# ----------------------------------------------------------------------
# Modus 2: Spieltag auswerten
# ----------------------------------------------------------------------
def modus_spieltag(tage_zurueck=4):
    stand = lade_stand()
    if not stand["spieler"]:
        sys.exit("Noch keine Spielerliste. Erst 'python hole_daten.py kader' laufen lassen.")

    nach_id = {s["id"]: s for s in stand["spieler"]}
    erspielt = {}
    gesehen = set(stand.get("ausgewertet", []))

    heute = datetime.now(timezone.utc).date()
    zeitraum = [(heute - timedelta(days=i)).isoformat() for i in range(1, tage_zurueck + 1)]

    try:
        partien = []
        for liga_id in LIGEN:
            for tag in zeitraum:
                for f in hole("fixtures", league=liga_id, season=SAISON, date=tag):
                    if f["fixture"]["status"]["short"] == "FT" and f["fixture"]["id"] not in gesehen:
                        partien.append(f["fixture"]["id"])

        print(f"{len(partien)} neue Partien auszuwerten")
        for fid in partien:
            for team in hole("fixtures/players", fixture=fid):
                for eintrag in team.get("players", []):
                    sid = eintrag["player"]["id"]
                    if sid not in nach_id:
                        continue
                    st = eintrag["statistics"][0]
                    pos = POS_MAP.get(st["games"].get("position") or "", nach_id[sid]["pos"])
                    erspielt[sid] = erspielt.get(sid, 0) + punkte_fuer(st, pos)
            gesehen.add(fid)

    except QuotaAus as e:
        print(f"{e}. Es wird mit dem ausgewertet, was da ist.")

    if not erspielt:
        print("Keine neuen Partien.")
        return

    # Punkte eintragen
    for sid, p in erspielt.items():
        s = nach_id[sid]
        s["historie"] = (s["historie"] + [p])[-10:]
        s["gesamt"] = s.get("gesamt", 0) + p
        s["punkte"] = round(sum(s["historie"]) / len(s["historie"]), 1)

    preise_anpassen(stand["spieler"], set(erspielt))
    normalisiere(stand["spieler"])

    stand["spieltag"] = stand.get("spieltag", 0) + 1
    stand["ausgewertet"] = sorted(gesehen)[-800:]
    schreibe(stand)
    print(f"Spieltag {stand['spieltag']} verbucht, {len(erspielt)} Spieler mit Punkten")


# ----------------------------------------------------------------------
# Marktwerte
# ----------------------------------------------------------------------
def preise_anpassen(spieler, aktive):
    """
    Zielwert aus Form und Saisonschnitt, dann gedämpft und gedeckelt
    dorthin bewegen. Wer nicht gespielt hat, verliert langsam an Wert.
    """
    for s in spieler:
        h = s.get("historie", [])
        if not h:
            continue
        form = sum(h[-4:]) / len(h[-4:])
        schnitt = sum(h) / len(h)
        erwartung = FORM_GEWICHT * form + (1 - FORM_GEWICHT) * schnitt
        ziel = math.pow(max(erwartung, 0.5), EXPONENT) * 3.0

        if s["id"] not in aktive:
            ziel *= 0.85                      # ohne Einsatz sinkt der Wert
            if s.get("verletzt"):
                ziel = max(ziel, s["preis"] * VERLETZT_BODEN)

        neu = s["preis"] + DAEMPFUNG * (ziel - s["preis"])
        neu = max(s["preis"] * (1 - DECKEL), min(s["preis"] * (1 + DECKEL), neu))
        s["preis"] = round(max(neu, 2.0), 1)


def normalisiere(spieler):
    """
    Hält die Summe aller Marktwerte konstant. Ohne das inflationiert der
    Markt über die Saison und das Budget verliert seine Bedeutung.
    """
    summe = sum(s["preis"] for s in spieler)
    if summe <= 0:
        return
    faktor = MARKT_SUMME / summe
    for s in spieler:
        s["preis"] = round(max(s["preis"] * faktor, 2.0), 1)


# ----------------------------------------------------------------------
if __name__ == "__main__":
    modus = sys.argv[1] if len(sys.argv) > 1 else "spieltag"
    if modus == "kader":
        modus_kader()
    elif modus == "spieltag":
        modus_spieltag()
    else:
        sys.exit("Modus muss 'kader' oder 'spieltag' sein.")
