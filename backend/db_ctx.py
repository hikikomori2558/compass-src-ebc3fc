# backend/db_ctx.py — соединение из пула с RLS-контекстом и ГАРАНТИРОВАННЫМ сбросом.
#
# Почему это отдельный модуль, а не строчки в роутерах: контекст app.role/app.user_id —
# это и есть вся наша изоляция (ст. 13 323-ФЗ). Если соединение вернётся в пул с чужой
# ролью, следующий арендатор увидит чужих пациентов. Поэтому сброс — в finally,
# без исключений, и переиспользовать соединения разрешено только через этот хелпер.
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg


@asynccontextmanager
async def db_conn(pool: asyncpg.Pool, *, role: Optional[str] = None, user_id: Optional[str] = None):
    """Выдаёт соединение с выставленным RLS-контекстом.

    Семантика app.user_id (согласована с backend/db/schema.sql):
      врачи/админ  -> users_auth.user_id
      пациенты     -> patient_hash_id (записи в users_auth у пациента нет).
    Пустой role (по умолчанию) — fail-closed: RLS без контекста не показывает ничего.
    """
    conn = await pool.acquire()
    try:
        await conn.execute("SELECT set_config('app.role', $1, false)", role or "")
        await conn.execute("SELECT set_config('app.user_id', $1, false)", user_id or "")
        yield conn
    finally:
        # Сброс ДО возврата в пул: даже если тело контекста упало с исключением,
        # следующему арендатору не достанется чужая роль. Ошибка сброса — только в лог,
        # соединение всё равно возвращаем (пул переживёт, утечка роли — нет).
        try:
            await conn.execute("SELECT set_config('app.role', '', false), set_config('app.user_id', '', false)")
        finally:
            await pool.release(conn)
