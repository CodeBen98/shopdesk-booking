
import os, base64, json
key = os.environ.get('SUPABASE_SERVICE_KEY', '')
try:
    payload = key.split('.')[1] if key else ''
    payload += '=' * (4 - len(payload) % 4)
    decoded = json.loads(base64.b64decode(payload))
    print(f"KEY_ROLE={decoded.get('role')} KEY_LEN={len(key)} KEY_START={key[:20]}")
except Exception as e:
    print(f"KEY_DECODE_ERROR={e} KEY_LEN={len(key)}")
