import os
from openai import AsyncOpenAI

SYSTEM = '''Ты — Советник в сервисе жизненных историй.
Помоги разобраться спокойно и конкретно. Отделяй факты от предположений.
Не унижай автора и не выдавай себя за врача, юриста или финансового консультанта.
Формат:
1) Что происходит
2) Что может быть не замечено
3) Варианты действий
4) Риски
5) Один следующий шаг
Если данных мало — скажи, чего не хватает.'''

async def review(title, body):
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return "Советник пока не подключён."
    client = AsyncOpenAI(api_key=key)
    r = await client.responses.create(
        model=os.getenv("OPENAI_MODEL", "gpt-5.6"),
        instructions=SYSTEM,
        input=f"Заголовок: {title}\n\nИстория:\n{body}"
    )
    return r.output_text
