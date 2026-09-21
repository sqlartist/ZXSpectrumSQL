#!/usr/bin/env python3
"""
sqlblob.py -- emit a large byte string as valid T-SQL.

A binary literal cannot contain whitespace, so 0x followed by a newline is
a syntax error. Wrapping 32,768 hex digits for readability breaks the
script. Equally, a single literal beyond 8,000 bytes risks silent
truncation depending on how it is typed.

So: assign the first chunk, append the rest, and assert the length.
"""

CHUNK_BYTES = 2000          # 4,000 hex digits per line, well under any limit


def emit_blob(var, data, indent=''):
    """T-SQL that builds `data` into varbinary(max) variable `var`."""
    if not data:
        return f'{indent}DECLARE {var} varbinary(max) = CAST(\'\' AS varbinary(max));\n'

    hexs = data.hex().upper()
    step = CHUNK_BYTES * 2
    chunks = [hexs[i:i + step] for i in range(0, len(hexs), step)]

    out = [f'{indent}DECLARE {var} varbinary(max);\n']
    out.append(f'{indent}SET {var} = 0x{chunks[0]};\n')
    for c in chunks[1:]:
        out.append(f'{indent}SET {var} = {var} + 0x{c};\n')
    # RAISERROR substitution arguments must be constants or variables --
    # a function call there is a syntax error, so hold the length first.
    lenvar = var + '_len'
    out.append(
        f'{indent}DECLARE {lenvar} int = DATALENGTH({var});\n'
        f'{indent}IF {lenvar} <> {len(data)}\n'
        f'{indent}BEGIN\n'
        f'{indent}    RAISERROR(\'{var} is %d bytes, expected {len(data)}\', 16, 1, {lenvar});\n'
        f'{indent}END\n')
    return ''.join(out)
