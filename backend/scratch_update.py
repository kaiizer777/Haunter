import re

with open('tests/test_eval_harness.py', 'r') as f:
    content = f.read()

# Add import if not present
if 'truncate_all' not in content:
    content = content.replace('from sqlalchemy import select', 'from sqlalchemy import select\nfrom tests.conftest import truncate_all')

# Regex to find async def test_...(db: AsyncSession)
# and insert await truncate_all(db)
pattern = re.compile(r'(async def test_[a-zA-Z0-9_]+\(.*?\bdb:\s*AsyncSession.*?\)\s*->\s*None:\n(?:\s+\"\"\"[^\"]*\"\"\"\n)?)', re.MULTILINE | re.DOTALL)

def replacer(match):
    return match.group(1) + '    await truncate_all(db)\n'

new_content = pattern.sub(replacer, content)

with open('tests/test_eval_harness.py', 'w') as f:
    f.write(new_content)
print('Done')
