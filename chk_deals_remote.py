import paramiko, time

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('89.252.185.100', port=22, username='root', password='SZhuC121', timeout=15, look_for_keys=False, allow_agent=False)

def run(cmd, timeout=30):
    chan = client.get_transport().open_session()
    chan.settimeout(timeout)
    chan.exec_command(cmd)
    out, err = b'', b''
    while not chan.exit_status_ready():
        if chan.recv_ready(): out += chan.recv(8192)
        if chan.recv_stderr_ready(): err += chan.recv_stderr(8192)
        time.sleep(0.1)
    while chan.recv_ready(): out += chan.recv(8192)
    while chan.recv_stderr_ready(): err += chan.recv_stderr(8192)
    return out.decode('utf-8', errors='replace').strip(), err.decode('utf-8', errors='replace').strip()

remote_script = """
import sys, os
sys.path.insert(0, '/root/firsatvakti')
os.chdir('/root/firsatvakti')
from db import get_db
db = get_db()

print('=== AKTIF DEALLAR ===')
rows = db.execute('''
    SELECT d.id, d.active, d.status, d.discount_pct, d.new_price, d.old_price,
           d.created_at, p.title, p.platform
    FROM deals d JOIN products p ON p.id=d.product_id
    WHERE d.active=1
    ORDER BY d.created_at DESC LIMIT 10
''').fetchall()
for r in rows:
    print(dict(r))

print('=== PENDING DEALLAR ===')
pend = db.execute('''
    SELECT d.id, d.active, d.status, d.discount_pct, d.new_price, d.created_at, p.title
    FROM deals d JOIN products p ON p.id=d.product_id
    WHERE d.status=\\\"pending\\\"
    ORDER BY d.created_at DESC LIMIT 5
''').fetchall()
for r in pend:
    print(dict(r))
print('Toplam pending:', len(pend))

print('=== .env BILDIRI AYARLARI ===')
from dotenv import dotenv_values
env = dotenv_values('/root/firsatvakti/.env')
keys = ['TELEGRAM_BOT_TOKEN','TELEGRAM_CHANNEL_ID','WHATSAPP_API_KEY','NOTIFY_CHANNEL','BOT_TOKEN']
for k in keys:
    v = env.get(k, 'YOK')
    print(k, '=', v[:20] if v and v != 'YOK' else v)
"""

sftp = client.open_sftp()
with sftp.open('/tmp/chk_deals.py', 'w') as f:
    f.write(remote_script)
sftp.close()

o, e = run('/root/firsatvakti/venv/bin/python3 /tmp/chk_deals.py')
print(o)
if e:
    print("ERR:", e[:500])
client.close()
