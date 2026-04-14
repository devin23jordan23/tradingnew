"""
conditions.py - All trading alert conditions.
Each function returns (triggered: bool, details: dict)
"""

def check_above_premarket_high(current_price, pm_high, tolerance=0.002):
    if pm_high is None or pm_high == 0:
        return False, {}
    diff_pct  = (current_price - pm_high) / pm_high
    triggered = diff_pct >= -tolerance
    return triggered, {
        "condition": "Above PM High",
        "pm_high":   round(pm_high, 2),
        "current":   round(current_price, 2),
        "diff_pct":  round(diff_pct * 100, 2)
    }


def check_ema_touch(current_price, ema_value, tolerance=0.003):
    if ema_value is None or ema_value == 0:
        return False, {}
    diff_pct  = abs(current_price - ema_value) / ema_value
    triggered = diff_pct <= tolerance
    return triggered, {
        "condition": "9 EMA Touch",
        "ema":       round(ema_value, 2),
        "current":   round(current_price, 2),
        "diff_pct":  round(diff_pct * 100, 2)
    }


def check_vwap(current_price, vwap, direction="above"):
    if vwap is None or vwap == 0:
        return False, {}
    above     = current_price > vwap
    triggered = above if direction == "above" else not above
    label     = "Above VWAP" if above else "Below VWAP"
    return triggered, {
        "condition": label,
        "vwap":      round(vwap, 2),
        "current":   round(current_price, 2),
        "diff_pct":  round(((current_price - vwap) / vwap) * 100, 2)
    }


def check_elevated_volume(current_vol, avg_vol, threshold=1.5):
    if avg_vol is None or avg_vol == 0:
        return False, {}
    ratio     = current_vol / avg_vol
    triggered = ratio >= threshold
    return triggered, {
        "condition":   "Elevated Volume",
        "ratio":       round(ratio, 2),
        "current_vol": int(current_vol),
        "avg_vol":     int(avg_vol)
    }


def check_prior_day_high_break(current_price, prior_day_high, current_vol, avg_vol, vol_threshold=1.5):
    if prior_day_high is None or prior_day_high == 0:
        return False, {}
    vol_ratio = current_vol / avg_vol if avg_vol else 0
    triggered = current_price > prior_day_high and vol_ratio >= vol_threshold
    return triggered, {
        "condition":      "Prior Day High Break",
        "prior_day_high": round(prior_day_high, 2),
        "current":        round(current_price, 2),
        "vol_ratio":      round(vol_ratio, 2)
    }


def check_half_atr_retrace(current_price, open_price, atr, session_high, retrace_pct=0.50):
    if atr is None or atr == 0 or session_high is None:
        return False, {}
    half_atr_target = open_price + (atr * 0.5)
    target_reached  = session_high >= half_atr_target
    if not target_reached:
        return False, {}
    move_size       = session_high - open_price
    pullback        = session_high - current_price
    pullback_ratio  = pullback / move_size if move_size > 0 else 0
    triggered       = abs(pullback_ratio - retrace_pct) <= 0.05
    return triggered, {
        "condition":        f"{int(retrace_pct*100)}% Retrace from Session High",
        "open":             round(open_price, 2),
        "session_high":     round(session_high, 2),
        "half_atr_target":  round(half_atr_target, 2),
        "current":          round(current_price, 2),
        "pullback_pct":     round(pullback_ratio * 100, 2)
    }


def check_order_block(current_price, ob_low, ob_high, tolerance=0.005):
    if ob_low is None or ob_high is None:
        return False, {}
    in_zone = (ob_low * (1 - tolerance)) <= current_price <= (ob_high * (1 + tolerance))
    return in_zone, {
        "condition": "Order Block Return",
        "ob_zone":   f"${round(ob_low,2)} – ${round(ob_high,2)}",
        "current":   round(current_price, 2)
    }


def check_option_retrace(current_option_price, session_option_high, levels=(0.50, 0.625)):
    if session_option_high is None or session_option_high == 0:
        return False, {}
    for level in levels:
        target = session_option_high * (1 - level)
        if abs(current_option_price - target) / session_option_high <= 0.03:
            return True, {
                "condition":    f"Option {int(level*100)}% Retrace",
                "session_high": round(session_option_high, 2),
                "target":       round(target, 2),
                "current":      round(current_option_price, 2),
                "retrace_level": f"{int(level*100)}%"
            }
    return False, {}


def check_bull_flag(candles, min_legs=2):
    if len(candles) < min_legs + 2:
        return False, {}
    lows        = [c["low"] for c in candles[-5:]]
    higher_lows = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i-1])
    ranges      = [c["high"] - c["low"] for c in candles[-5:]]
    compressing = ranges[-1] < ranges[0]
    triggered   = higher_lows >= min_legs and compressing
    return triggered, {
        "condition":         "Bull Flag (Higher Lows)",
        "higher_lows_count": higher_lows,
        "compressing":       compressing,
        "recent_lows":       [round(l, 2) for l in lows]
    }


def check_earnings_vwap_pullback(current_price, prior_day_vwap, tolerance=0.008):
    if prior_day_vwap is None or prior_day_vwap == 0:
        return False, {}
    diff      = abs(current_price - prior_day_vwap) / prior_day_vwap
    triggered = diff <= tolerance
    return triggered, {
        "condition":    "Earnings Gap → Prior VWAP Pullback",
        "prior_vwap":   round(prior_day_vwap, 2),
        "current":      round(current_price, 2),
        "distance_pct": round(diff * 100, 2)
    }


def score_conditions(triggered_list):
    weights = {
        "order block":   2,
        "option":        2,
        "earnings":      2,
        "above pm high": 1,
        "9 ema":         1,
        "vwap":          1,
        "volume":        1,
        "prior day":     1,
        "retrace":       1,
        "bull flag":     1,
    }
    score = 0
    for detail in triggered_list:
        cond = detail.get("condition", "").lower()
        matched = False
        for key, w in weights.items():
            if key in cond:
                score  += w
                matched = True
                break
        if not matched:
            score += 1

    if score >= 7:
        grade = "A+"
    elif score >= 5:
        grade = "A"
    elif score >= 3:
        grade = "B"
    else:
        grade = "BELOW_THRESHOLD"

    return score, grade
