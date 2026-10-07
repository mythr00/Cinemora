from pathlib import Path
import re

path = Path(r"media\1e8db933-5815-45e7-9d02-a743b6e3ecec\graphics_filter.ffscript")

text = path.read_text(encoding="utf-8")

def fix_between(match):
    start = match.group(1)
    end = match.group(2)
    return f"between(t\\,{start}\\,{end})"

fixed = re.sub(
    r"between\(t,([0-9]+(?:\.[0-9]+)?),([0-9]+(?:\.[0-9]+)?)\)",
    fix_between,
    text,
)

path.write_text(fixed, encoding="utf-8")

print("GRAPH PATCH COMPLETE")
print("Original between() count:", len(re.findall(r"between\(t,", text)))
print("Escaped between() count :", len(re.findall(r"between\(t\\,", fixed)))
