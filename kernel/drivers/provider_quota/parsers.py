"""Normalize documented vendor balance/window replies without inventing usage.

Protocol shapes were checked against vendor documentation and Magpie's MIT
implementation at df341e7671bcf509d376d1f38247d0da357e43d5. This is a separate
Python implementation; missing amounts never become zero remaining.
"""
from __future__ import annotations

from datetime import datetime
import math
import re

from drivers.account_quota import QuotaBalance, QuotaUnavailable, QuotaWindow


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, OverflowError):
        return None


def timestamp(value):
    numeric = number(value)
    if numeric is not None:
        return int(numeric / 1000 if numeric >= 1e12 else numeric) if numeric > 0 else None
    if isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace('Z', '+00:00'))
            return int(result.timestamp()) if result.tzinfo else None
        except (ValueError, OverflowError):
            pass
    return None


def mapping(value):
    return value if isinstance(value, dict) else {}


def rows(value):
    return value if isinstance(value, list) else []


def label(value, fallback):
    return value.strip() if isinstance(value, str) and re.fullmatch(r'[^\x00-\x1f\x7f]{1,100}', value) else fallback


def period(mins, fallback='额度'):
    if not mins:
        return fallback
    if mins % 1440 == 0:
        return f'{mins // 1440} 天'
    if mins % 60 == 0:
        return f'{mins // 60} 小时'
    return f'{mins} 分钟'


def window(key, name, used, duration=None, reset=None, *, left=None, limit=None, unit=None):
    used = number(used)
    if used is None or used < 0:
        return None
    return QuotaWindow(key, name, min(100, used), duration, timestamp(reset),
                       remaining_amount=left, limit_amount=limit, unit=unit)


def balance(key, name, value, currency):
    amount = number(value)
    if amount is None:
        return None
    return QuotaBalance(key, name, amount, currency)


def normalize(kind, payload, *, currency='USD'):
    """Return (windows, balances, plan). Empty payloads are explicitly unknown."""
    if not isinstance(payload, dict):
        raise QuotaUnavailable(reason='quota_not_returned')
    ws, bs, plan = [], [], None
    data = mapping(payload.get('data'))
    if kind == 'deepseek':
        for item in rows(payload.get('balance_infos')):
            item = mapping(item)
            code = item.get('currency')
            if isinstance(code, str) and re.fullmatch(r'[A-Z]{3}', code):
                bs.append(balance(code.lower(), '可用余额', item.get('total_balance'), code))
    elif kind == 'moonshot':
        if payload.get('status') is False or payload.get('code') not in (None, 0):
            raise QuotaUnavailable(reason='request_failed')
        bs.append(balance('available', '可用余额', data.get('available_balance'), currency))
    elif kind == 'openrouter':
        total, used = number(data.get('total_credits')), number(data.get('total_usage'))
        if total is not None and used is not None:
            bs.append(balance('credits', '可用余额', total - used, 'USD'))
    elif kind == 'openrouter-key':
        limit, left = number(data.get('limit')), number(data.get('limit_remaining'))
        if limit is not None and limit >= 0 and left is not None:
            bs.append(balance('key-budget', '密钥可用额度', left, 'USD'))
        elif 'limit' in data and data['limit'] is None and data.get('limit_remaining') is None:
            raise QuotaUnavailable('unsupported', reason='management_key_required')
    elif kind == 'siliconflow':
        if payload.get('code') == 20092:
            raise QuotaUnavailable('unsupported', reason='endpoint_retired')
        bs.append(balance('available', '可用余额', data.get('totalBalance'), currency))
    elif kind == 'stepfun':
        bs.append(balance('available', '可用余额', payload.get('balance'), currency))
    elif kind == 'aihubmix':
        remaining = number(payload.get('total_usage'))
        if remaining is not None and remaining < 0:
            raise QuotaUnavailable('unsupported', reason='management_key_required')
        bs.append(balance('key-limit', '密钥可用额度', remaining, 'USD'))
    elif kind == 'zhipu':
        if payload.get('success') is False:
            raise QuotaUnavailable(reason='request_failed')
        plan = label(data.get('level'), None)
        for index, item in enumerate(rows(data.get('limits'))):
            item = mapping(item)
            unit, count = item.get('unit'), number(item.get('number'))
            duration = int((count if count and count > 0 else 5) * 60) if unit == 3 else 10080 if unit == 6 else None
            name = 'MCP · 每月' if item.get('type') == 'TIME_LIMIT' else period(duration)
            total, left, used = number(item.get('usage')), number(item.get('remaining')), number(item.get('currentValue'))
            percentage = number(item.get('percentage'))
            key = f'limit:{item.get("type", "unknown")}:{unit}:{index}'
            if total == 0:
                ws.append(QuotaWindow(key, name, 0, duration, timestamp(item.get('nextResetTime')), unlimited=True))
                continue
            amount = None
            if total is not None and total > 0:
                if left is not None and 0 <= left <= total:
                    amount, percentage = left, (total - left) / total * 100
                elif used is not None and 0 <= used <= total:
                    amount = total - used
                    percentage = used / total * 100 if percentage is None else percentage
            unit_label = '次' if item.get('type') == 'TIME_LIMIT' else '额度'
            ws.append(window(key, name, percentage, duration, item.get('nextResetTime'),
                left=amount, limit=total if amount is not None else None, unit=unit_label if amount is not None else None))
    elif kind == 'opencode-go':
        usage = mapping(payload.get('usage'))
        for key, duration, name in [('rolling', 300, '5 小时'), ('weekly', 10080, '每周'), ('monthly', None, '每月')]:
            item = mapping(usage.get(key))
            used = number(item.get('percent'))
            # The vendor reports an artificial future reset for untouched pools.
            ws.append(window(key, name, used, duration, item.get('resetsAt') if used and used > 0 else None))
    elif kind == 'kimi-code':
        buckets = []
        for index, item in enumerate(rows(payload.get('limits'))):
            item = mapping(item)
            span = mapping(item.get('window'))
            duration = number(span.get('duration'))
            factor = {'TIME_UNIT_MINUTE': 1, 'TIME_UNIT_HOUR': 60, 'TIME_UNIT_DAY': 1440}.get(span.get('timeUnit'))
            mins = int(duration * factor) if duration and duration > 0 and factor else None
            buckets.append((f'limit:{index}', mapping(item.get('detail')) or span, mins))
        buckets.append(('weekly', mapping(payload.get('usage')), 10080))
        for key, item, mins in buckets:
            total, used, left = number(item.get('limit')), number(item.get('used')), number(item.get('remaining'))
            if total is None or total <= 0 or (used is None and left is None):
                continue
            used = max(0, total - left) if used is None else used
            reset = next((item[k] for k in ('resetTime', 'resetAt', 'reset_at', 'reset_time') if item.get(k)), None)
            ws.append(window(key, period(mins), used / total * 100, mins, reset))
    elif kind == 'minimax':
        base = mapping(payload.get('base_resp'))
        if base.get('status_code') != 0:
            raise QuotaUnavailable(reason='request_failed')
        for item in rows(payload.get('model_remains')):
            item = mapping(item)
            model = label(item.get('model_name'), None)
            if not model:
                continue
            for key, prefix, start_key, end_key, fallback in [
                ('interval', 'current_interval', 'start_time', 'end_time', None),
                ('weekly', 'current_weekly', 'weekly_start_time', 'weekly_end_time', 10080),
            ]:
                state, left = item.get(prefix + '_status'), number(item.get(prefix + '_remaining_percent'))
                if state == 3 or left is None and state != 2:
                    continue
                used = 100 if state == 2 else max(0, min(100, 100 - left))
                start, end = timestamp(item.get(start_key)), timestamp(item.get(end_key))
                mins = int((end - start) / 60) if start and end and end > start else fallback
                name = period(mins)
                if model.lower() != 'general':
                    name = f'{model} · {name}'
                total, count = number(item.get(prefix + '_total_count')), number(item.get(prefix + '_usage_count'))
                amount = None
                if total and total > 0 and count is not None and left is not None and 0 <= count <= total:
                    as_used, as_left = abs((total - count) / total * 100 - left), abs(count / total * 100 - left)
                    if min(as_used, as_left) <= 1:
                        amount = (total - count) if as_used < as_left else count
                        if state == 2:
                            amount = 0
                ws.append(window(f'{model}:{key}', name, used, mins, end, left=amount,
                                 limit=total if amount is not None else None, unit='次' if amount is not None else None))
    elif kind == 'commandcode':
        credits = mapping(payload.get('credits'))
        amounts = [number(credits.get(key)) for key in ('monthlyCredits', 'purchasedCredits', 'freeCredits')]
        if all(value is not None for value in amounts):
            bs.append(balance('credits', '可用余额', sum(amounts), 'USD'))
        plan = label(credits.get('planId'), None)
        limits = mapping(payload.get('windowLimits'))
        for key, mins in [('fiveHour', 300), ('weekly', 10080)]:
            item = mapping(limits.get(key))
            used, total = number(item.get('used')), number(item.get('cap'))
            if used is not None and total is not None and total > 0:
                ws.append(window(key, period(mins), used / total * 100, mins, item.get('resetAt'), left=max(0, total - used), limit=total, unit='USD'))
    elif kind == 'ollama':
        included, purchased = mapping(payload.get('included')), mapping(payload.get('purchased'))
        bs.append(balance('purchased', '充值余额', purchased.get('balance_usd'), 'USD'))
        for key, mins in [('session', None), ('weekly', 10080)]:
            item = mapping(included.get(key))
            left = number(item.get('remaining_percent'))
            if left is not None:
                ws.append(window(key, period(mins, '当前周期'), max(0, 100 - left), mins, item.get('resets_at')))
        total, left = number(included.get('allowance_usd')), number(included.get('balance_usd'))
        if total is not None and total > 0 and left is not None:
            dates = mapping(included.get('period'))
            start, end = timestamp(dates.get('from')), timestamp(dates.get('until'))
            mins = int((end - start) / 60) if start and end and end > start else None
            ws.append(window('included', '订阅额度', max(0, total - left) / total * 100, mins, end, left=left, limit=total, unit='USD'))
    else:
        raise QuotaUnavailable('unsupported', reason='unsupported_provider')
    windows, balances = tuple(item for item in ws if item is not None), tuple(item for item in bs if item is not None)
    if not windows and not balances:
        raise QuotaUnavailable(reason='quota_not_returned')
    return windows, balances, plan
