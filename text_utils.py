"""Conservative Telegram text budgets (UTF-16 units, before HTML escaping)."""


def text_length(value):
    return len(str(value or '').encode('utf-16-le')) // 2


def take_text(value, limit):
    text = str(value or '')
    return text.encode('utf-16-le')[:max(0, limit) * 2].decode('utf-16-le', errors='ignore')


def preview(value, limit):
    text = str(value or '').strip()
    if text_length(text) <= limit:
        return text
    return take_text(text, limit - 1).rstrip() + '…'


def text_pages(value, limit=3000):
    text = str(value or '')
    pages = []
    while text:
        part = take_text(text, limit)
        if not part:
            raise ValueError('Page size is too small')
        pages.append(part)
        text = text[len(part):]
    return pages or ['Пока нет текста.']
