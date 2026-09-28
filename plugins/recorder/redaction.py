"""Archive-boundary redaction for nested browser and server evidence.

URLs, log assignments, and editable HTML fields are masked before persistence.
Recorded transcript text remains private evidence and is never executed.
"""
import re

_SECRET = re.compile(r"(^|[_-])(token|password|passwd|secret|authorization|cookie|api[_-]?key|credential)([_-]|$)",re.I)
_QUERY = re.compile(r"(?i)([?&](?:token|access_token|auth_token|api_key|key|password|secret|lease_token)=)[^&#\s]+")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_ASSIGN = re.compile(r"""(?i)(["']?(?:access_token|auth_token|api_key|password|passwd|secret|cookie|lease_token|authorization)["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;]+)""")
_USERINFO = re.compile(r"(?i)(https?://)[^\s/@:]+(?::[^\s/@]*)?@")

def _text(value):
    value = str(value)
    value = _USERINFO.sub(r"\1[redacted]@",value)
    value = _QUERY.sub(r"\1[redacted]",value)
    value = _BEARER.sub("Bearer [redacted]",value)
    return _ASSIGN.sub(r"\1[redacted]",value)

def html(value):
    value = _text(value)
    value = re.sub(r"(?is)<input\b[^>]*>", lambda m: re.sub(
        r"""(?is)\bvalue\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""",
        'value="[redacted]"',m.group(0)),value)
    value = re.sub(r"(?is)(<textarea\b[^>]*>).*?(</textarea\s*>)",r"\1[redacted]\2",value)
    value = re.sub(r"""(?is)(<([a-z][\w-]*)\b[^>]*\bcontenteditable(?:\s*=\s*(?:"true"|'true'|true|""|''))?[^>]*>).*?(</\2\s*>)""",
                   r"\1[redacted]\3",value)
    return value

def text(value):
    value = str(value)
    return html(value) if re.search(r"(?i)<(?:input|textarea)\b|\bcontenteditable",value) else _text(value)

def clean(value, depth=0):
    if depth>12: return "[structure depth omitted]"
    if isinstance(value,dict):
        return {str(k):"[redacted]" if _SECRET.search(str(k)) else clean(v,depth+1)
                for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [clean(v,depth+1) for v in value]
    if isinstance(value,str): return text(value)
    if value is None or isinstance(value,(int,float,bool)): return value
    return text(value)
