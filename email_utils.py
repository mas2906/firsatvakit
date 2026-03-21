#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E-posta gönderme modülü — SMTP ile şifre sıfırlama."""

import os, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
SITE_URL  = os.getenv("SITE_URL", "https://firsatvakti.com")


def send_email(to: str, subject: str, html_body: str) -> bool:
    """E-posta gönder. Başarılıysa True döner."""
    if not SMTP_HOST or not SMTP_USER:
        print(f"[email] ⚠ SMTP ayarlanmamış — {to} → {subject}")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SMTP_FROM
        msg["To"]      = to
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo()
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_FROM, [to], msg.as_string())
        print(f"[email] ✔ Gönderildi: {to}")
        return True
    except Exception as e:
        print(f"[email] ✘ Hata ({to}): {e}")
        return False


def send_password_reset(to: str, username: str, token: str) -> bool:
    reset_link = f"{SITE_URL}/reset-password/{token}"
    html = f"""
<!DOCTYPE html>
<html lang="tr">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:'DM Sans',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:40px 20px;">
    <tr><td align="center">
      <table width="520" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);">
        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#f4501c,#ff8c00);padding:32px;text-align:center;">
            <div style="font-size:36px;margin-bottom:8px;">🔥</div>
            <h1 style="color:#ffffff;margin:0;font-size:24px;font-weight:800;letter-spacing:1px;">FIRSATVAKTİ</h1>
          </td>
        </tr>
        <!-- Body -->
        <tr>
          <td style="padding:36px 40px;">
            <h2 style="color:#1a1a1a;margin:0 0 16px;font-size:20px;">Şifre Sıfırlama Talebi</h2>
            <p style="color:#555;line-height:1.6;margin:0 0 24px;">
              Merhaba <strong>{username}</strong>, şifreni sıfırlamak için aşağıdaki butona tıkla.
              Bu link <strong>1 saat</strong> geçerlidir.
            </p>
            <div style="text-align:center;margin:32px 0;">
              <a href="{reset_link}" style="background:linear-gradient(135deg,#f4501c,#ff8c00);color:#ffffff;text-decoration:none;padding:14px 32px;border-radius:12px;font-weight:700;font-size:16px;display:inline-block;">
                Şifremi Sıfırla →
              </a>
            </div>
            <p style="color:#888;font-size:13px;line-height:1.5;margin:0 0 8px;">
              Butona tıklamazsan bu linki tarayıcına kopyala:
            </p>
            <p style="color:#f4501c;font-size:12px;word-break:break-all;margin:0 0 24px;">
              {reset_link}
            </p>
            <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
            <p style="color:#aaa;font-size:12px;margin:0;">
              Bu talebi sen yapmadıysan bu e-postayı görmezden gel. Şifren değişmeyecek.
            </p>
          </td>
        </tr>
        <!-- Footer -->
        <tr>
          <td style="background:#f8f8f8;padding:20px 40px;text-align:center;">
            <p style="color:#aaa;font-size:12px;margin:0;">
              © 2025 FırsatVakti · <a href="{SITE_URL}" style="color:#f4501c;text-decoration:none;">firsatvakti.com</a>
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""
    return send_email(to, "FırsatVakti — Şifre Sıfırlama", html)


def send_price_alert(to: str, username: str, product_title: str,
                     old_price: float, new_price: float, pct: float,
                     deal_url: str) -> bool:
    """Takip edilen üründe fiyat düşüşü bildirimi gönder."""
    old_fmt = f"{old_price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    new_fmt = f"{new_price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    html = f"""
<!DOCTYPE html>
<html lang="tr">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:'DM Sans',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:40px 20px;">
    <tr><td align="center">
      <table width="520" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);">
        <tr>
          <td style="background:linear-gradient(135deg,#f4501c,#ff8c00);padding:32px;text-align:center;">
            <div style="font-size:36px;margin-bottom:8px;">🔥</div>
            <h1 style="color:#ffffff;margin:0;font-size:24px;font-weight:800;letter-spacing:1px;">FIRSATVAKTİ</h1>
          </td>
        </tr>
        <tr>
          <td style="padding:36px 40px;">
            <h2 style="color:#1a1a1a;margin:0 0 8px;font-size:20px;">Takip ettiğin üründe fiyat düştü!</h2>
            <p style="color:#555;margin:0 0 20px;">Merhaba <strong>{username}</strong>,</p>
            <div style="background:#fff8f0;border-left:4px solid #f4501c;padding:16px 20px;border-radius:8px;margin-bottom:24px;">
              <p style="margin:0 0 12px;font-weight:700;color:#1a1a1a;">{product_title[:100]}</p>
              <p style="margin:0;font-size:18px;">
                <s style="color:#aaa;">{old_fmt} TL</s>
                &nbsp;→&nbsp;
                <strong style="color:#f4501c;font-size:22px;">{new_fmt} TL</strong>
                &nbsp;<span style="background:#f4501c;color:#fff;padding:2px 8px;border-radius:6px;font-size:13px;font-weight:700;">%{pct:.0f} İndirim</span>
              </p>
            </div>
            <div style="text-align:center;margin:28px 0;">
              <a href="{deal_url}" style="background:linear-gradient(135deg,#f4501c,#ff8c00);color:#ffffff;text-decoration:none;padding:14px 32px;border-radius:12px;font-weight:700;font-size:16px;display:inline-block;">
                Fırsata Git →
              </a>
            </div>
            <hr style="border:none;border-top:1px solid #eee;margin:24px 0;">
            <p style="color:#aaa;font-size:12px;margin:0;">
              Bu bildirimi almak istemiyorsan <a href="{SITE_URL}/product" style="color:#f4501c;">takip listeni</a> düzenleyebilirsin.
            </p>
          </td>
        </tr>
        <tr>
          <td style="background:#f8f8f8;padding:20px 40px;text-align:center;">
            <p style="color:#aaa;font-size:12px;margin:0;">
              © 2025 FırsatVakti · <a href="{SITE_URL}" style="color:#f4501c;text-decoration:none;">firsatvakti.com</a>
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""
    return send_email(to, f"🔥 Fiyat düştü: {product_title[:50]}", html)
