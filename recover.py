import json
import re

transcript_path = "/Users/mgajjar/.gemini/antigravity-ide/brain/7c1c913f-6685-4b6a-8cd4-5b12beb0a147/.system_generated/logs/transcript_full.jsonl"
recovered_lines = {}

with open(transcript_path, 'r') as f:
    for line in f:
        try:
            data = json.loads(line)
        except:
            continue
            
        if data.get('type') == 'VIEW_FILE' and data.get('status') == 'DONE':
            content = data.get('content', '')
            if 'pwa/src/screens.jsx' in content and 'The following code has been modified to include a line number' in content:
                for text_line in content.split('\n'):
                    match = re.match(r'^(\d+): (.*)$', text_line)
                    if match:
                        line_num = int(match.group(1))
                        recovered_lines[line_num] = match.group(2)

print(f"Recovered {len(recovered_lines)} lines.")
if len(recovered_lines) > 0:
    with open('pwa/src/screens_recovered.jsx', 'w') as out:
        for i in range(1, max(recovered_lines.keys()) + 1):
            out.write(recovered_lines.get(i, '') + '\n')
