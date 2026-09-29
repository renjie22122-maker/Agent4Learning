"""Read bounded name/description scalars from a SKILL frontmatter header."""
import json
import re
import textwrap


def metadata(text):
    lines=text.lstrip('\ufeff').splitlines()
    if not lines or lines[0].strip()!='---':return {}
    end=next((i for i in range(1,len(lines)) if lines[i].strip() in ('---','...')),len(lines))
    header=lines[1:end]; result={}; i=0
    while i<len(header):
        match=re.match(r'^(name|description):\s*(.*)$',header[i]);i+=1
        if not match:continue
        key,value=match.groups(); value=value.strip()
        if re.fullmatch(r'[>|][+-]?[1-9]?(?:\s+#.*)?',value):
            block=[]
            while i<len(header) and (not header[i].strip() or header[i][0].isspace()):
                block.append(header[i]);i+=1
            body=textwrap.dedent('\n'.join(block)).strip('\n')
            value=re.sub(r'(?<=\S)\n(?=\S)',' ',body) if value.startswith('>') else body
        elif value.startswith('"'):
            try:value=json.loads(value)
            except ValueError:value=value.strip('"')
        elif value.startswith("'") and value.endswith("'"):
            value=value[1:-1].replace("''", "'")
        else:value=re.split(r'\s+#',value,1)[0].strip()
        result[key]=value
    return result
