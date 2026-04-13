import requests
import json
import os
import sys
import re
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout.reconfigure(encoding='utf-8')



# =========================
# 📋 LISTES DE PAIRES
# =========================
WATCHLISTS = {
    "majors": [
        "BTC-USDT", "ETH-USDT", "BNB-USDT", "SOL-USDT", "XRP-USDT"
    ],
    "alts": [
        "AVAX-USDT", "DOT-USDT", "LINK-USDT", "MATIC-USDT", "ATOM-USDT",
        "UNI-USDT", "AAVE-USDT", "INJ-USDT", "SUI-USDT", "APT-USDT"
    ],
    "defi": [
        "CRV-USDT", "DYDX-USDT", "GMX-USDT", "SNX-USDT", "LDO-USDT"
    ],
    "custom": []  # ajoute tes paires ici
}

# Config scanner
TIMEFRAMES    = ["15m", "1H", "4H"]
CANDLES_LIMIT = 100          # bougies récupérées par TF
RSI_PERIOD    = 14
MIN_SCORE     = 6            # score minimum pour apparaître dans les résultats
MAX_WORKERS   = 5            # requêtes parallèles
SCAN_INTERVAL = 300          # secondes entre chaque scan complet (5 min)


# =========================
# 📡 DATA — OKX
# =========================
def get_candles(symbol, timeframe, limit=100):
    """Récupère les bougies OHLCV depuis OKX"""
    url = "https://www.okx.com/api/v5/market/candles"
    params = {
        "instId": symbol,
        "bar":    timeframe,
        "limit":  str(limit)
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json().get("data", [])
        # OKX retourne [ts, open, high, low, close, vol, ...]
        # ordre antichronologique → inverser
        candles = []
        for c in reversed(data):
            candles.append({
                "ts":     int(c[0]),
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5])
            })
        return candles
    except Exception as e:
        return []

def get_price(symbol):
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/market/ticker",
            params={"instId": symbol}, timeout=5
        )
        return float(r.json()["data"][0]["last"])
    except:
        return None


# =========================
# 📐 RSI — CALCUL NATIF
# =========================
def calculate_rsi(closes, period=14):
    """Calcule le RSI sans librairie externe"""
    if len(closes) < period + 1:
        return []

    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]

    # Première moyenne simple
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rsi_values = []

    for i in range(period, len(deltas)):
        if avg_loss == 0:
            rsi_values.append(100.0)
        else:
            rs  = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
            rsi_values.append(round(rsi, 2))

        # Moyenne exponentielle lissée (Wilder)
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    # Aligner avec les bougies (les period premières n'ont pas de RSI)
    padding = [None] * (period + 1)
    return padding + rsi_values


# =========================
# 🔍 DÉTECTION DIVERGENCES
# =========================
def find_swing_points(values, window=3):
    """Détecte les swing highs et lows sur une série"""
    highs, lows = [], []
    for i in range(window, len(values) - window):
        if values[i] is None:
            continue
        slice_v = [v for v in values[i-window:i+window+1] if v is not None]
        if not slice_v:
            continue
        if values[i] == max(slice_v):
            highs.append(i)
        if values[i] == min(slice_v):
            lows.append(i)
    return highs, lows

def detect_divergences(candles, rsi_values, min_distance=5):
    """
    Détecte les 4 types de divergences RSI.
    Retourne une liste de divergences détectées avec leur score.
    """
    if not candles or not rsi_values:
        return []

    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]

    swing_highs, swing_lows = find_swing_points(closes, window=3)

    divergences = []

    # --- BEARISH REGULAR : prix HH + RSI LH ---
    for i in range(len(swing_highs)):
        for j in range(i + 1, len(swing_highs)):
            a, b = swing_highs[i], swing_highs[j]
            if b - a < min_distance:
                continue
            if rsi_values[a] is None or rsi_values[b] is None:
                continue
            # Prix monte, RSI baisse
            if highs[b] > highs[a] and rsi_values[b] < rsi_values[a]:
                rsi_zone = "overbought" if rsi_values[a] > 70 or rsi_values[b] > 70 else "neutral"
                score = _score_divergence("bearish_regular", rsi_values[a], rsi_values[b], rsi_zone, b - a)
                divergences.append({
                    "type":       "bearish_regular",
                    "label":      "Bearish Regular",
                    "direction":  "short",
                    "idx_a":      a,
                    "idx_b":      b,
                    "price_a":    highs[a],
                    "price_b":    highs[b],
                    "rsi_a":      rsi_values[a],
                    "rsi_b":      rsi_values[b],
                    "rsi_zone":   rsi_zone,
                    "distance":   b - a,
                    "score":      score,
                    "fresh":      b >= len(candles) - 5  # divergence récente
                })

    # --- BULLISH REGULAR : prix LL + RSI HL ---
    for i in range(len(swing_lows)):
        for j in range(i + 1, len(swing_lows)):
            a, b = swing_lows[i], swing_lows[j]
            if b - a < min_distance:
                continue
            if rsi_values[a] is None or rsi_values[b] is None:
                continue
            if lows[b] < lows[a] and rsi_values[b] > rsi_values[a]:
                rsi_zone = "oversold" if rsi_values[a] < 30 or rsi_values[b] < 30 else "neutral"
                score = _score_divergence("bullish_regular", rsi_values[a], rsi_values[b], rsi_zone, b - a)
                divergences.append({
                    "type":       "bullish_regular",
                    "label":      "Bullish Regular",
                    "direction":  "long",
                    "idx_a":      a,
                    "idx_b":      b,
                    "price_a":    lows[a],
                    "price_b":    lows[b],
                    "rsi_a":      rsi_values[a],
                    "rsi_b":      rsi_values[b],
                    "rsi_zone":   rsi_zone,
                    "distance":   b - a,
                    "score":      score,
                    "fresh":      b >= len(candles) - 5
                })

    # --- BEARISH HIDDEN : prix LH + RSI HH ---
    for i in range(len(swing_highs)):
        for j in range(i + 1, len(swing_highs)):
            a, b = swing_highs[i], swing_highs[j]
            if b - a < min_distance:
                continue
            if rsi_values[a] is None or rsi_values[b] is None:
                continue
            if highs[b] < highs[a] and rsi_values[b] > rsi_values[a]:
                rsi_zone = "overbought" if rsi_values[b] > 70 else "neutral"
                score = _score_divergence("bearish_hidden", rsi_values[a], rsi_values[b], rsi_zone, b - a)
                divergences.append({
                    "type":       "bearish_hidden",
                    "label":      "Bearish Hidden",
                    "direction":  "short",
                    "idx_a":      a,
                    "idx_b":      b,
                    "price_a":    highs[a],
                    "price_b":    highs[b],
                    "rsi_a":      rsi_values[a],
                    "rsi_b":      rsi_values[b],
                    "rsi_zone":   rsi_zone,
                    "distance":   b - a,
                    "score":      score,
                    "fresh":      b >= len(candles) - 5
                })

    # --- BULLISH HIDDEN : prix HL + RSI LL ---
    for i in range(len(swing_lows)):
        for j in range(i + 1, len(swing_lows)):
            a, b = swing_lows[i], swing_lows[j]
            if b - a < min_distance:
                continue
            if rsi_values[a] is None or rsi_values[b] is None:
                continue
            if lows[b] > lows[a] and rsi_values[b] < rsi_values[a]:
                rsi_zone = "oversold" if rsi_values[b] < 30 else "neutral"
                score = _score_divergence("bullish_hidden", rsi_values[a], rsi_values[b], rsi_zone, b - a)
                divergences.append({
                    "type":       "bullish_hidden",
                    "label":      "Bullish Hidden",
                    "direction":  "long",
                    "idx_a":      a,
                    "idx_b":      b,
                    "price_a":    lows[a],
                    "price_b":    lows[b],
                    "rsi_a":      rsi_values[a],
                    "rsi_b":      rsi_values[b],
                    "rsi_zone":   rsi_zone,
                    "distance":   b - a,
                    "score":      score,
                    "fresh":      b >= len(candles) - 5
                })

    # Garder uniquement la divergence la plus récente et haute score par type
    best = {}
    for d in divergences:
        key = d["type"]
        if key not in best or d["score"] > best[key]["score"]:
            best[key] = d

    return sorted(best.values(), key=lambda x: x["score"], reverse=True)


def _score_divergence(div_type, rsi_a, rsi_b, rsi_zone, distance):
    """Score une divergence de 0 à 10"""
    score = 0

    # Type : regular > hidden
    if "regular" in div_type:
        score += 4
    else:
        score += 2

    # Zone RSI extrême
    if rsi_zone in ("overbought", "oversold"):
        score += 3
    else:
        score += 1

    # Distance entre les points (plus c'est long, plus c'est fiable)
    if distance >= 20:
        score += 2
    elif distance >= 10:
        score += 1

    # Amplitude de la divergence RSI
    amplitude = abs(rsi_a - rsi_b)
    if amplitude >= 10:
        score += 1

    return min(score, 10)


# =========================
# 📊 SCAN D'UNE PAIRE
# =========================
def scan_pair(symbol):
    """Scanne une paire sur tous les TF et retourne les divergences trouvées"""
    result = {
        "symbol":      symbol,
        "price":       get_price(symbol),
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "timeframes":  {},
        "best_signal": None,
        "total_score": 0
    }

    for tf in TIMEFRAMES:
        candles = get_candles(symbol, tf, CANDLES_LIMIT)
        if len(candles) < RSI_PERIOD + 10:
            continue

        closes     = [c["close"] for c in candles]
        rsi_values = calculate_rsi(closes, RSI_PERIOD)
        divs       = detect_divergences(candles, rsi_values)

        # Filtrer les divergences fraîches uniquement
        fresh_divs = [d for d in divs if d["fresh"]]

        result["timeframes"][tf] = {
            "rsi_current": rsi_values[-1] if rsi_values else None,
            "divergences": fresh_divs,
            "candles_count": len(candles)
        }

        # Accumuler le score total
        for d in fresh_divs:
            result["total_score"] += d["score"]

        # Meilleur signal = divergence regular sur TF élevé
        for d in fresh_divs:
            if result["best_signal"] is None:
                result["best_signal"] = {**d, "timeframe": tf}
            else:
                # Préférer TF plus élevé + regular + score plus élevé
                tf_priority = {"4H": 3, "1H": 2, "15m": 1}
                current_tf  = result["best_signal"]["timeframe"]
                if (tf_priority.get(tf, 0) > tf_priority.get(current_tf, 0)
                        and d["score"] >= result["best_signal"]["score"]):
                    result["best_signal"] = {**d, "timeframe": tf}

    return result


# =========================
# 🚀 SCANNER MULTI-PAIRES
# =========================
def run_scanner(watchlist_name="majors"):
    """Lance le scan sur toute une watchlist en parallèle"""
    pairs = WATCHLISTS.get(watchlist_name, [])
    if not pairs:
        print(f"❌ Watchlist '{watchlist_name}' vide")
        return []

    print(f"\n🔍 Scan {watchlist_name} — {len(pairs)} paires — {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    print(f"   TF : {', '.join(TIMEFRAMES)} | Score min : {MIN_SCORE}\n")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(scan_pair, p): p for p in pairs}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                res = future.result()
                results.append(res)
                # Affichage progressif
                if res["total_score"] >= MIN_SCORE:
                    print(f"  ✅ {symbol:<15} score={res['total_score']:>3}  "
                          f"prix={res['price']}")
                else:
                    print(f"  ·  {symbol:<15} score={res['total_score']:>3}")
            except Exception as e:
                print(f"  ❌ {symbol} erreur: {e}")

    # Trier par score décroissant
    results.sort(key=lambda x: x["total_score"], reverse=True)
    return results


# =========================
# 🖥️ AFFICHAGE RÉSULTATS
# =========================
DIV_ICON = {
    "bullish_regular": "🟢 Bull Regular",
    "bearish_regular": "🔴 Bear Regular",
    "bullish_hidden":  "🟩 Bull Hidden",
    "bearish_hidden":  "🟥 Bear Hidden",
}

def display_results(results, min_score=None):
    min_score = min_score or MIN_SCORE
    filtered  = [r for r in results if r["total_score"] >= min_score]

    if not filtered:
        print(f"\n  Aucun signal avec score ≥ {min_score}")
        return

    W = 60
    print(f"\n{'═' * W}")
    print(f"  DIVERGENCES DÉTECTÉES — {len(filtered)} paires / {len(results)} scannées")
    print(f"{'═' * W}")

    for r in filtered:
        print(f"\n  {r['symbol']:<14} {r['price']}$   score global : {r['total_score']}")
        print(f"  {'─' * (W-2)}")

        for tf, tf_data in r["timeframes"].items():
            rsi_cur = tf_data["rsi_current"]
            divs    = tf_data["divergences"]

            if not divs:
                continue

            rsi_tag = ""
            if rsi_cur:
                if rsi_cur > 70:
                    rsi_tag = f"RSI {rsi_cur:.1f} ⚠️ OB"
                elif rsi_cur < 30:
                    rsi_tag = f"RSI {rsi_cur:.1f} ⚠️ OS"
                else:
                    rsi_tag = f"RSI {rsi_cur:.1f}"

            print(f"\n  [{tf}]  {rsi_tag}")

            for d in divs:
                icon  = DIV_ICON.get(d["type"], d["type"])
                score_bar = ("█" * d["score"]).ljust(10, "░")
                fresh_tag = " ← FRAIS" if d["fresh"] else ""

                print(f"    {icon}{fresh_tag}")
                print(f"    Score    : {score_bar} {d['score']}/10")
                print(f"    Prix A→B : {d['price_a']:.2f} → {d['price_b']:.2f}")
                print(f"    RSI  A→B : {d['rsi_a']:.1f} → {d['rsi_b']:.1f}")
                print(f"    Distance : {d['distance']} bougies")
                print(f"    Zone RSI : {d['rsi_zone']}")

        # Meilleur signal global
        bs = r["best_signal"]
        if bs:
            direction = "LONG" if bs["direction"] == "long" else "SHORT"
            print(f"\n  SIGNAL PRIORITAIRE : {direction} sur {bs['timeframe']}")
            print(f"  → {DIV_ICON.get(bs['type'], bs['type'])}")
            print(f"  → Entrée zone : autour de {bs['price_b']:.2f}$")
            print(f"  → Confirmer avec : bougie 15m + RSI franchit 50")

    print(f"\n{'═' * W}\n")


# =========================
# 💾 EXPORT
# =========================
def save_results(results, watchlist_name):
    filename = f"scan_{watchlist_name}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"💾 Résultats sauvegardés → {filename}")
    return filename


# =========================
# 🔄 MODE CONTINU
# =========================
def run_continuous(watchlist_name="majors", interval=SCAN_INTERVAL):
    """Scan en boucle toutes les X secondes"""
    print(f"🔄 Mode continu — scan toutes les {interval//60} minutes")
    print(f"   Watchlist : {watchlist_name}")
    print(f"   Ctrl+C pour arrêter\n")

    cycle = 0
    while True:
        cycle += 1
        print(f"─── Cycle #{cycle} ───")

        results = run_scanner(watchlist_name)
        display_results(results)
        save_results(results, watchlist_name)

        print(f"⏳ Prochain scan dans {interval//60} minutes...\n")
        time.sleep(interval)


# =========================
# 🚀 MAIN
# =========================
def main():
    import argparse

    parser = argparse.ArgumentParser(description="RSI Divergence Scanner")
    parser.add_argument("--list",       default="majors",
                        choices=list(WATCHLISTS.keys()),
                        help="Watchlist à scanner")
    parser.add_argument("--score",      type=int, default=MIN_SCORE,
                        help="Score minimum pour afficher")
    parser.add_argument("--continuous", action="store_true",
                        help="Mode scan continu")
    parser.add_argument("--interval",   type=int, default=SCAN_INTERVAL,
                        help="Intervalle en secondes (mode continu)")
    parser.add_argument("--all",        action="store_true",
                        help="Scanner toutes les watchlists")
    args = parser.parse_args()

    if args.continuous:
        run_continuous(args.list, args.interval)
        return

    if args.all:
        all_results = []
        for wl_name in WATCHLISTS:
            if not WATCHLISTS[wl_name]:
                continue
            res = run_scanner(wl_name)
            all_results.extend(res)
        all_results.sort(key=lambda x: x["total_score"], reverse=True)
        display_results(all_results, args.score)
        save_results(all_results, "all")
        return

    results = run_scanner(args.list)
    display_results(results, args.score)
    save_results(results, args.list)


if __name__ == "__main__":
    main()