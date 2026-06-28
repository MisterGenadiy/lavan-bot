"""Повтор вызовов Discord API при временных сбоях. Логика не изменилась
по сравнению с предыдущей версией backup_core.py — просто вынесена в
отдельный модуль, чтобы её могли использовать и capture, и restore."""

import asyncio

import discord


async def with_retry(coro_func, *args, retries: int = 3, base_delay: float = 1.5, **kwargs):
    """Повторяет вызов при ВРЕМЕННЫХ ошибках (5xx на стороне Discord), чтобы
    восстановление бэкапа не теряло роли/каналы из-за случайного кратковременного
    сбоя. Обычный rate-limit (429) discord.py уже обрабатывает сам внутри
    HTTP-клиента — здесь перехватывается только то, что дошло до нас как исключение."""
    last_exc = None
    for attempt in range(retries):
        try:
            return await coro_func(*args, **kwargs)
        except discord.HTTPException as e:
            last_exc = e
            status = getattr(e, "status", 0)
            if status and status < 500:
                raise  # клиентская ошибка (400/403/404) — повторять бессмысленно
            await asyncio.sleep(base_delay * (2 ** attempt))
    raise last_exc
