"""A股数据查询模块 — 从 quant.db 读取每日收盘数据"""
import sqlite3
import requests
from pathlib import Path

DB_PATH = Path.home() / ".tradingagents/data/quant.db"
INDEX_CODES = {
    "上证指数": ("sh", ["主板", "科创板"]),
    "深证成指": ("sz", ["主板", "创业板"]),
    "创业板指": ("sz", ["创业板"]),
}


def _connect():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


# 实时指数代码映射
_REALTIME_INDEX = {
    "上证指数": "sh000001",
    "深证成指": "sz399001",
    "创业板指": "sz399006",
    "科创50": "sh000688",
}


def get_realtime_indices() -> list[dict]:
    """从腾讯接口获取大盘指数实时行情"""
    codes = list(_REALTIME_INDEX.values())
    url = f"https://qt.gtimg.cn/q={','.join(codes)}"
    try:
        r = requests.get(url, timeout=5)
        r.encoding = "gbk"
        result = []
        for line in r.text.strip().split("\n"):
            if "~" not in line:
                continue
            parts = line.split("~")
            if len(parts) < 45:
                continue
            name = parts[1]
            price = float(parts[3] or 0)
            change_pct = float(parts[32] or 0)
            change_amt = float(parts[31] or 0)
            # parts[35] 复合字段: 价格/成交量(手)/成交额(元)
            composite = parts[35].split("/") if parts[35] else []
            volume_shou = float(composite[1]) if len(composite) >= 2 else 0
            volume_yi = round(volume_shou / 1e8, 2)  # 手→亿手
            # parts[37] 实际是成交额(万元)，parts[38] 是振幅等指标
            amount_yi = round(float(parts[37] or 0) / 1e4, 2)  # 万元→亿元
            result.append({
                "name": name,
                "price": price,
                "change_pct": change_pct,
                "change_amt": change_amt,
                "volume_yi": volume_yi,
                "amount_yi": amount_yi,
            })
        return result
    except Exception:
        return []


def get_historical_indices(date: str) -> list[dict]:
    """从 daily_indices 表获取指定日期的指数数据"""
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT code, name, close, change_pct, amount FROM daily_indices WHERE date=?",
            (date,),
        ).fetchall()
        conn.close()
        if not rows:
            return []
        result = []
        for r in rows:
            price = r[2] or 0
            chg_pct = r[3] or 0
            amount = r[4] or 0
            # change_amt 反算: price × chg_pct / (1 + chg_pct/100) × chg_pct/100 * price 错误
            # 直接用: prev_close = price / (1 + chg_pct/100), change_amt = price - prev_close
            if chg_pct != 0 and (1 + chg_pct / 100) != 0:
                prev_close = price / (1 + chg_pct / 100)
                change_amt = round(price - prev_close, 2)
            else:
                change_amt = 0
            result.append({
                "name": r[1],
                "price": price,
                "change_pct": chg_pct,
                "change_amt": change_amt,
                "volume_yi": 0,
                "amount_yi": round(amount, 2) if amount else 0,
            })
        return result
    except Exception:
        return []


def get_available_dates() -> list[str]:
    """获取有数据的交易日列表"""
    conn = _connect()
    rows = conn.execute(
        "SELECT DISTINCT date FROM collection_log WHERE is_trading_day=1 "
        "ORDER BY date DESC LIMIT 120"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_latest_date() -> str | None:
    dates = get_available_dates()
    return dates[0] if dates else None


def get_market_summary(date: str) -> dict:
    """大盘概览：各市场涨跌家数、总成交额、涨跌停数"""
    conn = _connect()
    summary = {"indices": [], "total_amount_yi": 0, "up_count": 0, "down_count": 0, "flat_count": 0}

    for idx_name, (market, boards) in INDEX_CODES.items():
        placeholders = ",".join(f"'{b}'" for b in boards)
        rows = conn.execute(
            f"SELECT v.change_pct, v.mcap, v.turnover_pct, v.limit_up, v.limit_down, b.amount "
            f"FROM daily_valuation v "
            f"JOIN stocks s ON v.code = s.code "
            f"JOIN daily_bars b ON v.code = b.code AND v.date = b.date "
            f"WHERE v.date = ? AND s.market = ? AND s.board_type IN ({placeholders}) "
            f"AND v.mcap > 0 AND b.amount > 0",
            (date, market),
        ).fetchall()

        if not rows:
            continue

        up = sum(1 for r in rows if r["change_pct"] and r["change_pct"] > 0)
        down = sum(1 for r in rows if r["change_pct"] and r["change_pct"] < 0)
        flat = len(rows) - up - down
        avg_chg = sum(r["change_pct"] for r in rows if r["change_pct"]) / max(len(rows), 1)
        total_amount = sum(r["amount"] for r in rows if r["amount"]) / 1e8
        limit_up = sum(1 for r in rows if r["limit_up"] and r["change_pct"] and r["change_pct"] >= 9.5)
        limit_down = sum(1 for r in rows if r["limit_down"] and r["change_pct"] and r["change_pct"] <= -9.5)

        summary["indices"].append({
            "name": idx_name,
            "total": len(rows),
            "up": up, "down": down, "flat": flat,
            "avg_change_pct": round(avg_chg, 2),
            "amount_yi": round(total_amount, 2),
            "limit_up": limit_up, "limit_down": limit_down,
        })
        summary["up_count"] += up
        summary["down_count"] += down
        summary["flat_count"] += flat
        summary["total_amount_yi"] += total_amount

    summary["total_amount_yi"] = round(summary["total_amount_yi"], 2)

    # 涨跌停总数
    conn2 = _connect()
    lu = conn2.execute(
        "SELECT COUNT(*) FROM daily_valuation WHERE date=? AND change_pct>=9.5",
        (date,),
    ).fetchone()[0]
    ld = conn2.execute(
        "SELECT COUNT(*) FROM daily_valuation WHERE date=? AND change_pct<=-9.5",
        (date,),
    ).fetchone()[0]
    conn2.close()
    summary["total_limit_up"] = lu
    summary["total_limit_down"] = ld

    conn.close()
    return summary


def get_sector_rankings(date: str) -> list[dict]:
    """行业板块排名（按涨跌幅）"""
    conn = _connect()
    rows = conn.execute(
        "SELECT name, rank, change_pct, turnover_yi, net_inflow_yi, "
        "up_count, down_count, leader "
        "FROM daily_industry WHERE date=? "
        "ORDER BY change_pct DESC",
        (date,),
    ).fetchall()
    conn.close()
    return [
        {
            "name": r["name"], "rank": r["rank"],
            "change_pct": round(r["change_pct"], 2) if r["change_pct"] else 0,
            "turnover_yi": round(r["turnover_yi"], 2) if r["turnover_yi"] else 0,
            "net_inflow_yi": round(r["net_inflow_yi"], 2) if r["net_inflow_yi"] else 0,
            "up": r["up_count"] or 0, "down": r["down_count"] or 0,
            "leader": r["leader"] or "",
        }
        for r in rows
    ]


def get_northbound(date: str) -> dict | None:
    """北向资金"""
    conn = _connect()
    row = conn.execute(
        "SELECT hgt_cum, sgt_cum FROM daily_northbound WHERE date=?",
        (date,),
    ).fetchone()
    conn.close()
    if not row or (row["hgt_cum"] is None and row["sgt_cum"] is None):
        return None
    hgt = round(row["hgt_cum"], 2) if row["hgt_cum"] else 0
    sgt = round(row["sgt_cum"], 2) if row["sgt_cum"] else 0
    return {
        "hgt_yi": hgt,  # 沪股通净买入(亿)
        "sgt_yi": sgt,  # 深股通净买入(亿)
        "total_yi": round(hgt + sgt, 2),
    }


def get_dragon_tiger(date: str) -> list[dict]:
    """龙虎榜（含估值数据）"""
    conn = _connect()
    rows = conn.execute(
        "SELECT dt.code, dt.name, dt.reason, dt.close, dt.change_pct, dt.net_buy_wan, dt.buy_wan, dt.sell_wan, "
        "v.mcap, v.turnover_pct, b.amount "
        "FROM daily_dragon_tiger dt "
        "LEFT JOIN daily_valuation v ON dt.code=v.code AND v.date=dt.date "
        "LEFT JOIN daily_bars b ON dt.code=b.code AND b.date=dt.date "
        "WHERE dt.date=? "
        "ORDER BY ABS(dt.net_buy_wan) DESC",
        (date,),
    ).fetchall()
    conn.close()
    return [
        {
            "code": r["code"], "name": r["name"] or "",
            "reason": r["reason"] or "",
            "close": round(r["close"], 2) if r["close"] else 0,
            "change_pct": round(r["change_pct"], 2) if r["change_pct"] else 0,
            "net_buy_wan": round(r["net_buy_wan"], 0) if r["net_buy_wan"] else 0,
            "buy_wan": round(r["buy_wan"], 0) if r["buy_wan"] else 0,
            "sell_wan": round(r["sell_wan"], 0) if r["sell_wan"] else 0,
            "mcap": round(r["mcap"], 2) if r["mcap"] else None,
            "turnover_pct": round(r["turnover_pct"], 2) if r["turnover_pct"] else None,
            "amount": round(r["amount"], 0) if r["amount"] else None,
        }
        for r in rows
    ]


def get_hot_stocks(date: str) -> list[dict]:
    """强势股 + 热点题材（含估值）"""
    conn = _connect()
    rows = conn.execute(
        "SELECT hs.code, hs.name, hs.reason_tags, hs.change_pct, hs.turnover_pct, hs.dde_net, "
        "v.mcap, b.amount "
        "FROM daily_hot_stocks hs "
        "LEFT JOIN daily_valuation v ON hs.code=v.code AND v.date=hs.date "
        "LEFT JOIN daily_bars b ON hs.code=b.code AND b.date=hs.date "
        "WHERE hs.date=? "
        "ORDER BY hs.change_pct DESC",
        (date,),
    ).fetchall()
    conn.close()
    return [
        {
            "code": r["code"], "name": r["name"] or "",
            "tags": (r["reason_tags"] or "").replace("+", " · "),
            "change_pct": round(r["change_pct"], 2) if r["change_pct"] else 0,
            "turnover_pct": round(r["turnover_pct"], 2) if r["turnover_pct"] else 0,
            "dde_net": round(r["dde_net"], 2) if r["dde_net"] else 0,
            "mcap": round(r["mcap"], 2) if r["mcap"] else None,
            "amount": round(r["amount"], 0) if r["amount"] else None,
        }
        for r in rows
    ]


def get_collection_status(date: str) -> dict | None:
    """获取当日数据采集状态"""
    conn = _connect()
    row = conn.execute(
        "SELECT is_trading_day, valuation_coverage_pct, daily_industry_count, "
        "daily_dragon_tiger_count, daily_hot_stocks_count, daily_northbound_count, "
        "errors, duration_sec "
        "FROM collection_log WHERE date=?",
        (date,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "is_trading_day": bool(row["is_trading_day"]),
        "valuation_coverage": round(row["valuation_coverage_pct"] or 0, 1),
        "industry_count": row["daily_industry_count"] or 0,
        "dragon_tiger_count": row["daily_dragon_tiger_count"] or 0,
        "hot_stocks_count": row["daily_hot_stocks_count"] or 0,
        "northbound_count": row["daily_northbound_count"] or 0,
        "has_errors": bool(row["errors"]),
        "errors": row["errors"] or "",
        "duration_sec": round(row["duration_sec"], 1) if row["duration_sec"] else 0,
    }
