import re

def is_phone_number(to_number: str) -> bool:
    if not to_number:
        return False
    to_number = to_number.replace('sip:', '').split('@')[0]
    clean_number = re.sub(r'[\s\-\(\)]', '', to_number)
    return clean_number.startswith('+') or clean_number.isdigit()

print(is_phone_number("sip:+123456789@sip.telnyx.com"))
print(is_phone_number("+123456789"))
