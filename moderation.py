import re

PHONE = re.compile(r'(?<!\d)(?:\+?\d[\d\s().-]{8,}\d)(?!\d)')
EMAIL = re.compile(r'\b[\w.+-]+@[\w.-]+\.[a-zа-я]{2,}\b', re.I)
DIRECT_THREAT = re.compile(
    r'\b(?:я\s+)?(?:тебя|вас)\s+(?:убью|сожгу)|'
    r'\b(?:убью|сожгу)\s+(?:тебя|вас)|\bя\s+взорву\b', re.I
)
REPORTED_THREAT = re.compile(
    r'\b(?:мне|нам)\s+угрожа\w*|\bугрожа\w*\s+(?:мне|нам)|'
    r'\b(?:он|она|они|муж|парень|девушка|отец|начальник|сосед)\s+'
    r'(?:мне\s+)?(?:сказал\w*|написал\w*|говор\w*|угрожа\w*)', re.I
)

def moderate(text: str):
    reasons = []
    if PHONE.search(text):
        reasons.append("телефон/номер")
    if EMAIL.search(text):
        reasons.append("электронная почта")
    # Reports of received threats must be publishable. This is a narrow local
    # guard, not a context-aware classifier; ambiguous content uses reports.
    if DIRECT_THREAT.search(text) and not REPORTED_THREAT.search(text):
        reasons.append("угрозы")
    return text.replace("@", "＠"), reasons
