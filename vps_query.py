import paramiko, sys, os

HOST = "89.252.185.100"
USER = "root"
PASS = "ig86C2Qm"
LOCAL_BASE  = r"D:\firsatvakti_new\firsatvakti_new"
REMOTE_BASE = "/root/firsatvakti"

DEPLOY_FILES = [
    ("scrapers/amazon.py", "scrapers/amazon.py"),
]

def run(client, cmd, timeout=20):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode('utf-8', errors='replace')
    err = stderr.read().decode('utf-8', errors='replace')
    return out, err

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASS, timeout=10)
sftp = client.open_sftp()

for local_rel, remote_rel in DEPLOY_FILES:
    local_path  = os.path.join(LOCAL_BASE, local_rel.replace("/", os.sep))
    remote_path = f"{REMOTE_BASE}/{remote_rel}"
    sftp.put(local_path, remote_path)
    print(f"[OK] {local_rel} -> VPS ({os.path.getsize(local_path):,} byte)")

sftp.close()

# pycache temizle + servisi yeniden başlat
run(client, f"find {REMOTE_BASE} -type d -name __pycache__ -exec rm -rf {{}} + 2>/dev/null")
out, _ = run(client, "systemctl restart firsatvakti.service && sleep 2 && systemctl is-active firsatvakti.service")
print("Web app:", out.strip())

out, _ = run(client, "journalctl -u firsatvakti.service -n 3 --no-pager 2>&1 | tail -3")
print(out.strip())

client.close()
print("[DONE]")
