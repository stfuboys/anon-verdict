import re

PHONE = re.compile(r'(?<!\d)(?:\+?\d[\d\s().-]{8,}\d)(?!\d)')
THREATS = re.compile(r'\b(?:убью|убить|взорву|сожгу)\b', re.I)

def moderate(text: str):
    reasons = []
    if PHONE.search(text):
        reasons.append("телефон/номер")
    if THREATS.search(text):
        reasons.append("угрозы")
    return text.replace("@", "＠"), reasons
