#!/usr/bin/env python3
"""Offline phase-0 retro-backtest of published Reactivation catches.

Reads the public dashboard snapshot (``market[*].caught_obs_*`` and
``report.signal_theses[*]``), fetches GMGN 5m candles for the 24 hours after
each catch through ``scanner.fetch_gmgn_kline`` and measures:

* first-touch brackets: +50% before -30% and +100% before -30% within 24h;
* 24h MFE / MAE and the 24h close return;
* a 2%-cost bracket expectancy (win = +50%, loss = -30%, open = 24h close).

Groups are compared with percentile-bootstrap 95% intervals.  The tool never
writes production state, never calls RPC and stops at the first GMGN
rate-limit/auth failure (the cache lets a later run resume).

Usage:
    GMGN_API_KEY=... python3 tools/phase0_backtest.py \
        --dashboard https://.../api/dashboard --out reports/phase0_backtest_2026-10-15.md
"""
import argparse
import json
import math
import os
import random
import statistics
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_DASHBOARD = "https://solana-radar-scan-dispatcher.gmirash-solana-radar.workers.dev/api/dashboard"
HORIZON = 24 * 60 * 60
STEP = 300
PAGE_ROWS = 100
COST = 0.02
BRACKETS = ((0.5, -0.3), (1.0, -0.3))
INFO_KEYS = ("top_10_holder_rate", "dev_team_hold_rate", "fresh_wallet_rate", "top70_sniper_hold_rate")
TAG_KEYS = ("bundler_wallets", "sniper_wallets")


def iso_ts(value):
    if not value:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())


def number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def load_dashboard(source):
    if source.startswith(("http://", "https://")):
        request = urllib.request.Request(source, headers={"User-Agent": "solana-radar-phase0-backtest"})
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    with open(source) as handle:
        return json.load(handle)


def thesis_flags(thesis):
    signals = (thesis.get("coordinated_activity") or {}).get("signals") or []
    return sorted({str(item.get("code")) for item in signals if isinstance(item, dict) and item.get("code")})


def synchronous_wallets(thesis):
    signals = (thesis.get("coordinated_activity") or {}).get("signals") or []
    return max((int(item.get("wallet_count") or 0) for item in signals
                if isinstance(item, dict) and item.get("code") == "synchronous_buys"), default=0)


def build_captures(dashboard):
    market = dashboard.get("market") or {}
    theses = {
        item.get("token_address"): item
        for item in (dashboard.get("report") or {}).get("signal_theses") or []
        if isinstance(item, dict) and item.get("token_address")
    }
    captures = []
    for token, entry in market.items():
        price = number(entry.get("caught_obs_price_usd"))
        caught_at = iso_ts(entry.get("caught_obs_mcap_at"))
        if not price or price <= 0 or not caught_at:
            continue
        thesis = theses.get(token) or {}
        created = iso_ts(entry.get("pair_created_at") or thesis.get("pair_created_at"))
        captures.append({
            "token": token,
            "symbol": entry.get("symbol") or thesis.get("symbol"),
            "caught_at": caught_at,
            "caught_price": price,
            "caught_mcap": number(entry.get("caught_obs_mcap_usd")),
            "caught_liquidity": number(entry.get("caught_obs_liquidity_usd")),
            "age_hours": (caught_at - created) / 3600 if created and created <= caught_at else None,
            "family": thesis.get("signal_family"),
            "tier": thesis.get("source_tier"),
            "supply": (thesis.get("supply_integrity") or {}).get("status") or "missing",
            "flags": thesis_flags(thesis),
            "sync_wallets": synchronous_wallets(thesis),
            "has_thesis": bool(thesis),
        })
    captures.sort(key=lambda item: item["caught_at"])
    return captures


class RateLimited(RuntimeError):
    pass


def gmgn_call(config, function, *args):
    try:
        return function(config, *args)
    except Exception as exc:
        if config.get("_gmgn_circuit_open"):
            raise RateLimited(str(exc)) from exc
        raise


def fetch_window(config, scanner, token, start, end, resolution="5m", max_pages=8):
    rows = {}
    to = end
    for _ in range(max_pages):
        batch = gmgn_call(config, scanner.fetch_gmgn_kline, token, resolution, start, to)
        batch = [row for row in batch if isinstance(row, dict) and number(row.get("time"))]
        inside = [row for row in batch if start * 1000 <= int(row["time"]) <= to * 1000]
        for row in inside:
            rows[int(row["time"])] = row
        if not inside or len(batch) < PAGE_ROWS:
            return sorted(rows.values(), key=lambda row: int(row["time"])), True
        earliest = min(int(row["time"]) for row in inside) // 1000
        if earliest <= start:
            return sorted(rows.values(), key=lambda row: int(row["time"])), True
        to = earliest - 1
    return sorted(rows.values(), key=lambda row: int(row["time"])), False


def cached_json(path, producer):
    if path.exists():
        return json.loads(path.read_text())
    value = producer()
    path.write_text(json.dumps(value))
    return value


def candles_for(capture, config, scanner, cache_dir):
    start = capture["caught_at"] - 3600
    end = capture["caught_at"] + HORIZON + STEP

    def produce():
        rows, complete = fetch_window(config, scanner, capture["token"], start, end)
        return {"resolution": "5m", "from": start, "to": end, "complete": complete, "rows": rows}

    return cached_json(cache_dir / f"{capture['token']}.5m.json", produce)


def refine_candle(capture, candle_start, config, scanner, cache_dir):
    def produce():
        rows, complete = fetch_window(config, scanner, capture["token"], candle_start, candle_start + STEP - 1, "1m", 1)
        return {"resolution": "1m", "complete": complete, "rows": rows}

    return cached_json(cache_dir / f"{capture['token']}.1m.{candle_start}.json", produce)


def first_touch(path, entry, take, stop, refine=None, with_candle=False):
    """First threshold hit; ``with_candle`` also returns the candle that triggered it."""
    for candle in path:
        high = number(candle.get("high")) or 0
        low = number(candle.get("low")) or 0
        hit_take = high >= entry * (1 + take)
        hit_stop = low <= entry * (1 + stop)
        if hit_take and hit_stop:
            minutes = refine(int(candle["time"]) // 1000) if refine else None
            if minutes:
                result, minute = first_touch(minutes, entry, take, stop, with_candle=True)
                if result in ("win", "loss"):
                    return (result, minute) if with_candle else result
            return ("loss_ambiguous", candle) if with_candle else "loss_ambiguous"
        if hit_take or hit_stop:
            result = "win" if hit_take else "loss"
            return (result, candle) if with_candle else result
    return ("open", None) if with_candle else "open"


def evaluate(capture, data, now, config=None, scanner=None, cache_dir=None, refine=True):
    t0 = capture["caught_at"]
    if t0 + HORIZON > now:
        return {"status": "immature"}
    rows = [row for row in data.get("rows") or [] if number(row.get("close"))]
    reference = [row for row in rows if int(row["time"]) // 1000 <= t0]
    if not reference:
        return {"status": "no_reference_candle", "kline_complete": data.get("complete")}
    reference_row = reference[-1]
    entry = number(reference_row["close"])
    entry_start = int(reference_row["time"]) // 1000
    path = [row for row in rows if entry_start < int(row["time"]) // 1000 < t0 + HORIZON]
    result = {
        "status": "ok",
        "kline_complete": bool(data.get("complete")),
        "entry": entry,
        "entry_gap_seconds": t0 - entry_start,
        "entry_vs_caught": entry / capture["caught_price"] - 1,
        "candles": len(path),
    }
    if not path:
        result.update(mfe=0.0, mae=0.0, ret_24h=0.0)
    else:
        result.update(
            mfe=max(number(row["high"]) or 0 for row in path) / entry - 1,
            mae=min(number(row["low"]) or entry for row in path) / entry - 1,
            ret_24h=number(path[-1]["close"]) / entry - 1,
        )

    result["refined_candles"] = 0

    def refine_fn(candle_start):
        if not refine or scanner is None:
            return None
        result["refined_candles"] += 1
        return refine_candle(capture, candle_start, config, scanner, cache_dir)["rows"]

    stop_candle = None
    for take, stop in BRACKETS:
        key = f"tp{int(take * 100)}_sl{int(-stop * 100)}"
        result[key], candle = first_touch(path, entry, take, stop, refine_fn, with_candle=True)
        if key == "tp50_sl30":
            stop_candle = candle
    outcome = result["tp50_sl30"]
    if outcome == "win":
        gross = close_fill = 0.5
    elif outcome.startswith("loss"):
        gross = -0.3
        # A manual stop on a rug gaps: fill at the close of the candle that crossed -30%.
        close_fill = min(-0.3, (number(stop_candle.get("close")) or 0) / entry - 1)
    else:
        gross = close_fill = result["ret_24h"]
    result["bracket_net"] = gross - COST
    result["bracket_close_fill_net"] = close_fill - COST
    result["crash80"] = result["mae"] <= -0.8
    return result


def bootstrap(values, statistic, iterations=4000, seed=17):
    values = list(values)
    if not values:
        return (None, None)
    rng = random.Random(seed)
    stats = sorted(statistic([rng.choice(values) for _ in values]) for _ in range(iterations))
    return stats[int(0.025 * iterations)], stats[int(0.975 * iterations) - 1]


def bootstrap_diff(left, right, statistic, iterations=4000, seed=23):
    left, right = list(left), list(right)
    if not left or not right:
        return (None, None)
    rng = random.Random(seed)
    stats = sorted(
        statistic([rng.choice(left) for _ in left]) - statistic([rng.choice(right) for _ in right])
        for _ in range(iterations)
    )
    return stats[int(0.025 * iterations)], stats[int(0.975 * iterations) - 1]


def rate(values):
    return sum(values) / len(values) if values else None


def mean(values):
    return statistics.fmean(values) if values else None


def median(values):
    return statistics.median(values) if values else None


def is_win(row, key="tp50_sl30"):
    return 1 if row["outcome"].get(key) == "win" else 0


def summarize(rows):
    wins50 = [is_win(row) for row in rows]
    wins100 = [is_win(row, "tp100_sl30") for row in rows]
    losses = [1 if row["outcome"]["tp50_sl30"].startswith("loss") else 0 for row in rows]
    mfe = [row["outcome"]["mfe"] for row in rows]
    mae = [row["outcome"]["mae"] for row in rows]
    ret = [row["outcome"]["ret_24h"] for row in rows]
    ev = [row["outcome"]["bracket_net"] for row in rows]
    ev_close = [row["outcome"].get("bracket_close_fill_net", row["outcome"]["bracket_net"]) for row in rows]
    crash = [1 if row["outcome"].get("crash80") else 0 for row in rows]
    return {
        "n": len(rows),
        "win50": rate(wins50), "win50_ci": bootstrap(wins50, rate),
        "loss30": rate(losses),
        "win100": rate(wins100), "win100_ci": bootstrap(wins100, rate),
        "mfe_median": median(mfe), "mfe_ci": bootstrap(mfe, median),
        "mae_median": median(mae),
        "ret_median": median(ret), "ret_ci": bootstrap(ret, median),
        "ev_mean": mean(ev), "ev_ci": bootstrap(ev, mean),
        "ev_close_mean": mean(ev_close), "ev_close_ci": bootstrap(ev_close, mean),
        "crash80": rate(crash), "crash80_ci": bootstrap(crash, rate),
    }


def not_classified(row):
    return row["family"] != "classified_wallets"


def not_young(row):
    return "young_low_activity" not in row["flags"]


def aged(row):
    return row["age_hours"] is not None and row["age_hours"] >= 24


def supply_ok(row):
    return row["supply"] not in ("concentrated", "unverified")


def combined(row):
    return not_classified(row) and not_young(row) and aged(row) and supply_ok(row)


FILTERS = (
    ("Без `classified_wallets`", not_classified),
    ("Без флага `young_low_activity`", not_young),
    ("Возраст на поимке ≥24 ч", aged),
    ("supply не concentrated/unverified", supply_ok),
    ("Комбинация A (все четыре фильтра выше)", combined),
    ("A + supply = distributed", lambda row: combined(row) and row["supply"] == "distributed"),
    ("Без classified + без young (без возраста и supply)", lambda row: not_classified(row) and not_young(row)),
    ("Возраст ≥24 ч + supply не concentrated/unverified", lambda row: aged(row) and supply_ok(row)),
    ("Без синхронных покупок ≥5 кошельков (калибровка штрафа, не гейт)", lambda row: row.get("sync_wallets", 0) < 5),
)

SEGMENTS = (
    ("family = classified_wallets", lambda row: row["family"] == "classified_wallets"),
    ("family = reactivation_wave", lambda row: row["family"] == "reactivation_wave"),
    ("флаг young_low_activity", lambda row: "young_low_activity" in row["flags"]),
    ("флаг common_direct_funding", lambda row: "common_direct_funding" in row["flags"]),
    ("синхронные покупки ≥5 кошельков", lambda row: row.get("sync_wallets", 0) >= 5),
    ("синхронные покупки <5 кошельков", lambda row: row.get("sync_wallets", 0) < 5),
    ("tier на поимке = candidate", lambda row: row["tier"] == "candidate"),
    ("tier на поимке = late_chase/noise", lambda row: row["tier"] in ("late_chase", "noise")),
    ("возраст <6 ч", lambda row: row["age_hours"] is not None and row["age_hours"] < 6),
    ("возраст 6–24 ч", lambda row: row["age_hours"] is not None and 6 <= row["age_hours"] < 24),
    ("возраст 1–3 дня", lambda row: row["age_hours"] is not None and 24 <= row["age_hours"] < 72),
    ("возраст 3–15 дней", lambda row: row["age_hours"] is not None and 72 <= row["age_hours"] < 360),
    ("возраст >15 дней", lambda row: row["age_hours"] is not None and row["age_hours"] >= 360),
    ("supply = distributed", lambda row: row["supply"] == "distributed"),
    ("supply = watch", lambda row: row["supply"] == "watch"),
    ("supply = concentrated", lambda row: row["supply"] == "concentrated"),
    ("supply = unverified", lambda row: row["supply"] == "unverified"),
    ("supply = concentrated или unverified", lambda row: not supply_ok(row)),
)


def pct(value, digits=1):
    return "—" if value is None else f"{value * 100:+.{digits}f}%"


def share(value, digits=1):
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def interval(ci, signed=False):
    low, high = ci
    if low is None:
        return "—"
    fmt = pct if signed else share
    return f"[{fmt(low)}; {fmt(high)}]"


def effect_mark(ci, lower_is_better=False):
    low, high = ci
    if low is None:
        return "—"
    if lower_is_better:
        low, high = -high, -low
    return "**+**" if low > 0 else "**−**" if high < 0 else "0"


def summary_row(label, stats):
    return (
        f"| {label} | {stats['n']} | {share(stats['win50'])} {interval(stats['win50_ci'])} | "
        f"{share(stats['win100'])} {interval(stats['win100_ci'])} | {share(stats['loss30'])} | "
        f"{pct(stats['mfe_median'])} | {pct(stats['mae_median'])} | "
        f"{pct(stats['ret_median'])} {interval(stats['ret_ci'], True)} | "
        f"{share(stats['crash80'])} {interval(stats['crash80_ci'])} | "
        f"{pct(stats['ev_mean'])} {interval(stats['ev_ci'], True)} | "
        f"{pct(stats['ev_close_mean'])} {interval(stats['ev_close_ci'], True)} |"
    )


TABLE_HEAD = (
    "| Группа | n | +50% раньше −30% [95% ДИ] | +100% раньше −30% [95% ДИ] | −30% первым | "
    "Медиана MFE 24ч | Медиана MAE 24ч | Медиана 24ч [95% ДИ] | Обвал ≥80% за 24ч [95% ДИ] | "
    "EV брекета, стоп −30% [95% ДИ] | EV брекета, стоп по close [95% ДИ] |\n"
    "|---|---:|---|---|---:|---:|---:|---|---|---|---|"
)


def info_section(rows, info):
    lines = []
    have = [row for row in rows if info.get(row["token"])]
    if not have:
        return ["Снимок GMGN `token info` не запрашивался (`--token-info` не задан)."]
    lines.append(
        f"Получено для {len(have)} из {len(rows)} пригодных поимок. Это **текущие** значения "
        "(15.10), а не значения на момент поимки: после слива доли держателей меняются, поэтому "
        "таблица смещена «заглядыванием вперёд» и годится только для выбора диапазонов порогов, "
        "а не для оценки эффекта."
    )
    lines.append("")
    lines.append("| Поле | p25 | медиана | p75 | p90 | доля > порога | порог |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    thresholds = {
        "top_10_holder_rate": 0.5, "dev_team_hold_rate": 0.2, "fresh_wallet_rate": 0.3,
        "top70_sniper_hold_rate": 0.2, "bundler_wallet_share": 0.5, "sniper_wallets": 50,
    }
    for key in [*INFO_KEYS, "bundler_wallet_share", "sniper_wallets"]:
        values = sorted(v for v in (info[row["token"]].get(key) for row in have) if v is not None)
        if not values:
            continue
        q = lambda p: values[min(len(values) - 1, int(p * len(values)))]
        above = sum(1 for v in values if v > thresholds[key]) / len(values)
        lines.append(
            f"| `{key}` | {q(0.25):.3g} | {q(0.5):.3g} | {q(0.75):.3g} | {q(0.9):.3g} | "
            f"{share(above)} | {thresholds[key]} |"
        )
    lines.append("")
    lines.append(
        "`bundler_wallet_share` = `wallet_tags_stat.bundler_wallets / stat.holder_count`. "
        "Абсолютный `bundler_wallets` растёт с числом сделок и без нормировки не сравним между токенами."
    )
    lines += ["", "Исходы по порогам (тоже смещено: значения сняты после исхода):", "", TABLE_HEAD]
    for key, limit in thresholds.items():
        above = [row for row in have if (info[row["token"]].get(key) or 0) > limit]
        below = [row for row in have if info[row["token"]].get(key) is not None
                 and info[row["token"]].get(key) <= limit]
        if above and below:
            lines.append(summary_row(f"`{key}` > {limit}", summarize(above)))
            lines.append(summary_row(f"`{key}` ≤ {limit}", summarize(below)))
    return lines


def fetch_info(config, scanner, rows, cache_dir):
    output = {}
    for row in rows:
        path = cache_dir / f"{row['token']}.info.json"

        def produce():
            data = gmgn_call(config, scanner.fetch_gmgn_raw_token_info, row["token"])
            stat = data.get("stat") or {}
            tags = data.get("wallet_tags_stat") or {}
            holders = number(stat.get("holder_count") or data.get("holder_count"))
            item = {key: number(stat.get(key)) for key in INFO_KEYS}
            item.update({key: number(tags.get(key)) for key in TAG_KEYS})
            item["holder_count"] = holders
            item["bundler_wallet_share"] = (
                item["bundler_wallets"] / holders if holders and item["bundler_wallets"] is not None else None
            )
            return item

        output[row["token"]] = cached_json(path, produce)
    return output


def write_report(path, dashboard, captures, rows, skipped, info, stopped, now):
    report = (dashboard.get("report") or {})
    evaluation = report.get("signal_evaluation") or {}
    counts = evaluation.get("counts") or {}
    controls_24h = ((evaluation.get("horizons") or {}).get("24h") or {}).get("control_outcome_status_counts") or {}
    base = summarize(rows)
    lines = [
        f"# Фаза 0: ретро-бэктест поимок Solana Radar ({datetime.fromtimestamp(now, timezone.utc):%Y-%m-%d})",
        "",
        "Сгенерировано `tools/phase0_backtest.py`. Только чтение: публичный `/api/dashboard` и GMGN "
        "`market kline` через `scanner.fetch_gmgn_kline`. Прод, Turso, Worker и RPC не трогались.",
        "",
        "## Данные и метод",
        "",
        f"- Снимок дашборда: `generated_at` = {dashboard.get('generated_at')}.",
        f"- Поимок с ценой и временем (`market[*].caught_obs_*`): {len(captures)}; "
        f"у всех есть тезис в `report.signal_theses`: {sum(1 for c in captures if c['has_thesis'])}.",
        f"- Пригодных для 24-часового исхода: **{len(rows)}**. Исключены: "
        + (", ".join(f"{k} — {v}" for k, v in sorted(skipped.items())) or "нет") + ".",
        "- Свечи GMGN 5m с окном [поимка − 1 ч; поимка + 24 ч], постранично (API отдаёт не больше "
        "100 последних свечей до `to`).",
        "- Вход: close последней 5m-свечи, начавшейся не позже времени поимки (то есть цена "
        "в пределах ≤5 минут после сигнала). Путь — свечи строго после свечи входа и до поимки + 24 ч.",
        "- «+X% раньше −30%»: первая свеча, где high ≥ вход × (1+X) или low ≤ вход × 0,7. Если в одной "
        "5m-свече сработало и то и другое, свеча разбирается по 1m; если неоднозначно и там, исход "
        "считается проигрышем (консервативно).",
        "- MFE/MAE: максимум high и минимум low за 24 ч относительно входа.",
        "- EV брекета: +50% при выигрыше, −30% при стопе, иначе close через 24 ч; минус 2% на комиссии "
        "и проскальзывание. Это **оптимистичная** оценка: стоп по −30% в неликвидном токене исполнится "
        "хуже, а гэпы на rug не учитываются. Поэтому есть второй вариант: стоп исполняется по close "
        "свечи, пересёкшей −30% (если close ниже −30%).",
        "- «Обвал ≥80%»: доля поимок, у которых минимум за 24 ч был на 80% и больше ниже входа.",
        "- Интервалы: перцентильный бутстрап, 4000 повторов, 95%. Разница «оставлено − отсеяно» тоже бутстрап.",
        "- Возраст на поимке = `caught_obs_mcap_at − pair_created_at` (создание пула; для pump.fun это миграция).",
        "",
    ]
    if stopped:
        lines += [f"> **Остановлено по лимиту GMGN:** {stopped}. Отчёт по частичным данным; "
                  "повторный запуск продолжит из кэша.", ""]
    entry_dev = [abs(row["outcome"]["entry_vs_caught"]) for row in rows]
    gaps = [row["outcome"]["entry_gap_seconds"] for row in rows]
    lines += [
        f"Качество входа: медиана |вход по свечам / `caught_obs_price_usd` − 1| = {share(median(entry_dev))}, "
        f"p90 = {share(sorted(entry_dev)[int(0.9 * len(entry_dev))] if entry_dev else None)}; "
        f"медианный возраст свечи входа {median(gaps) / 60 if gaps else 0:.1f} мин. "
        f"Неполная выгрузка свечей (лимит страниц): {sum(1 for row in rows if not row['outcome']['kline_complete'])}. "
        f"Разобрано по 1m: {sum(1 for row in rows if row['outcome'].get('refined_candles'))}; "
        f"остались неоднозначными (засчитаны проигрышем): "
        f"{sum(1 for row in rows if row['outcome']['tp50_sl30'] == 'loss_ambiguous')}.",
        "",
        "## Все сигналы",
        "",
        TABLE_HEAD,
        summary_row("Все пригодные поимки", base),
        "",
        "## Фильтры: до и после",
        "",
        "«До» — все пригодные поимки (строка выше). «Оставлено» — что видел бы пользователь с фильтром. "
        "«Отсеяно» — что фильтр убрал. Δ — разница «оставлено − отсеяно» с 95% ДИ; если интервал "
        "не пересекает ноль, эффект измерим на этой выборке.",
        "",
    ]
    effects = []
    for label, predicate in FILTERS:
        kept = [row for row in rows if predicate(row)]
        removed = [row for row in rows if not predicate(row)]
        kept_s, removed_s = summarize(kept), summarize(removed)
        d_win = bootstrap_diff([is_win(r) for r in kept], [is_win(r) for r in removed], rate)
        d_ev = bootstrap_diff([r["outcome"]["bracket_net"] for r in kept],
                              [r["outcome"]["bracket_net"] for r in removed], mean)
        d_ret = bootstrap_diff([r["outcome"]["ret_24h"] for r in kept],
                               [r["outcome"]["ret_24h"] for r in removed], median)
        d_crash = bootstrap_diff([1 if r["outcome"]["crash80"] else 0 for r in kept],
                                 [1 if r["outcome"]["crash80"] else 0 for r in removed], rate)
        d_close = bootstrap_diff([r["outcome"]["bracket_close_fill_net"] for r in kept],
                                 [r["outcome"]["bracket_close_fill_net"] for r in removed], mean)
        diffs = {"win": d_win, "ret": d_ret, "crash": d_crash, "ev": d_ev, "ev_close": d_close}
        effects.append((label, kept_s, removed_s, diffs))
        lines += [f"### {label}", "", TABLE_HEAD,
                  summary_row("Оставлено", kept_s), summary_row("Отсеяно", removed_s), "",
                  f"Δ «+50% раньше −30%»: {interval(d_win, True)}; Δ медианы 24ч: {interval(d_ret, True)}; "
                  f"Δ доли обвалов ≥80%: {interval(d_crash, True)}; Δ EV (стоп −30%): {interval(d_ev, True)}; "
                  f"Δ EV (стоп по close): {interval(d_close, True)}.", ""]
    lines += ["## Сегменты", "", TABLE_HEAD]
    for label, predicate in SEGMENTS:
        segment = [row for row in rows if predicate(row)]
        if segment:
            lines.append(summary_row(label, summarize(segment)))
    lines += ["", "## Сводка эффектов", "",
              "Знак: **+** — интервал разницы «оставлено − отсеяно» целиком в пользу фильтра, "
              "**−** — целиком против, **0** — пересекает ноль (эффект не измерен). Для доли обвалов "
              "«в пользу» значит меньше обвалов у оставленных.", "",
              "| Фильтр | n оставлено / отсеяно | win +50/−30 | медиана 24ч | обвалы ≥80% | EV стоп −30% | EV стоп по close |",
              "|---|---|:-:|:-:|:-:|:-:|:-:|"]
    for label, kept_s, removed_s, diffs in effects:
        marks = [effect_mark(diffs[key], lower_is_better=(key == "crash"))
                 for key in ("win", "ret", "crash", "ev", "ev_close")]
        lines.append(f"| {label} | {kept_s['n']} / {removed_s['n']} | " + " | ".join(marks) + " |")
    lines += [
        "",
        "## Контрольная группа",
        "",
        f"В публичном снимке контролей с пригодными ценами **нет**. `signal_evaluation` (режим "
        f"`{evaluation.get('mode')}`) сообщает только агрегаты: {counts.get('input_controls')} контролей, "
        f"статусы на 24 ч {json.dumps(controls_24h, ensure_ascii=False)}, полных пар "
        f"{counts.get('selected_pairs')}. Ни адресов, ни времени, ни цен контролей нет и в "
        "`/api/intelligence/episodes` (там только эпизоды сигналов). Поэтому все сравнения выше — "
        "**внутри сигналов** (оставлено против отсеяно), а не lift против рынка. Базовая частота "
        "«+50% раньше −30%» для случайного токена из тех же GMGN-списков неизвестна.",
        "",
        "## GMGN `stat` / `wallet_tags_stat` (для калибровки префильтра)",
        "",
        *info_section(rows, info),
        "",
        "## Ограничения",
        "",
        "- Выборка — одна неделя поимок (07–15.10), токены не независимы по времени: один рыночный режим.",
        "- Группы пересекаются; фильтры не выровнены по времени поимки и капитализации.",
        "- 5m-свечи GMGN по пулу с максимальной ликвидностью; редкие сделки дают пропуски свечей.",
        "- Исходы без учёта ликвидности на выходе; EV брекета — верхняя граница.",
        "",
    ]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines))
    return effects


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dashboard", default=DEFAULT_DASHBOARD, help="dashboard URL or saved JSON file")
    parser.add_argument("--out", required=True, help="markdown report path")
    parser.add_argument("--cache-dir", default=os.path.join(tempfile.gettempdir(), "solana-radar-phase0-kline"))
    parser.add_argument("--rows-json", help="optional per-capture outcome dump")
    parser.add_argument("--token-info", action="store_true", help="also fetch current GMGN token info stats")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-interval", type=float, default=1.5)
    args = parser.parse_args(argv)

    import scanner

    if not os.environ.get("GMGN_API_KEY"):
        parser.error("GMGN_API_KEY is required")
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    dashboard = load_dashboard(args.dashboard)
    now = iso_ts(dashboard.get("generated_at")) or int(time.time())
    captures = build_captures(dashboard)
    if args.limit:
        captures = captures[: args.limit]
    config = {"gmgn_enabled": True, "gmgn_min_interval_seconds": args.min_interval, "gmgn_timeout_seconds": 45}
    rows, skipped, stopped = [], {}, None
    for index, capture in enumerate(captures, 1):
        if capture["caught_at"] + HORIZON > now:
            skipped["моложе 24 ч на момент снимка"] = skipped.get("моложе 24 ч на момент снимка", 0) + 1
            continue
        try:
            data = candles_for(capture, config, scanner, cache_dir)
            outcome = evaluate(capture, data, now, config, scanner, cache_dir)
        except RateLimited as exc:
            stopped = str(exc)[:200]
            print(f"stopped at {index}/{len(captures)}: GMGN rate/auth limit", file=sys.stderr)
            break
        except Exception as exc:
            skipped["ошибка GMGN"] = skipped.get("ошибка GMGN", 0) + 1
            print(f"warn: {capture['token']}: {str(exc)[:160]}", file=sys.stderr)
            continue
        if outcome["status"] != "ok":
            label = {"no_reference_candle": "нет свечи до поимки"}.get(outcome["status"], outcome["status"])
            skipped[label] = skipped.get(label, 0) + 1
            continue
        rows.append({**capture, "outcome": outcome})
        if index % 20 == 0:
            print(f"{index}/{len(captures)} captures processed", file=sys.stderr, flush=True)
    info = {}
    if args.token_info and not stopped:
        try:
            info = fetch_info(config, scanner, rows, cache_dir)
        except RateLimited as exc:
            stopped = f"token info: {str(exc)[:200]}"
    effects = write_report(args.out, dashboard, captures, rows, skipped, info, stopped, now)
    if args.rows_json:
        Path(args.rows_json).write_text(json.dumps(rows, indent=1, default=str))
    for label, kept_s, removed_s, diffs in effects:
        print(f"{label}: kept {kept_s['n']} / removed {removed_s['n']}; d_median {interval(diffs['ret'], True)}; "
              f"d_crash {interval(diffs['crash'], True)}; d_ev_close {interval(diffs['ev_close'], True)}")
    return 1 if stopped else 0


if __name__ == "__main__":
    sys.exit(main())
