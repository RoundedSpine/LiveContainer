#!/usr/bin/env python3
"""Expose concise build failures in annotations while retaining complete raw logs."""
from pathlib import Path
import re

root = Path(__file__).resolve().parents[2] / 'build-logs-memory'
error = re.compile(r'^(?:.*?:\d+(?::\d+)?: (?:fatal )?error:|error:|.*Error:|\*\* (?:ARCHIVE|BUILD) FAILED)', re.I)
for path in sorted(root.glob('*.log')):
    lines = path.read_text(errors='replace').splitlines()
    examples = []
    for index, line in enumerate(lines):
        if error.search(line) and len(examples) < 4:
            examples.append('\n'.join(lines[index:index + 5])[:1800])
    for example in examples:
        text = (path.name + '\n' + example).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        print('::notice title=Custom IPA build detail::' + text)
