#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kullanıcı Yorum Sistemi — v2."""

from datetime import datetime
from security import sanitize_comment

COOLDOWN_SEC = 60
MAX_PER_DAY = 20


def add_comment(db, product_id, user_id, body, rating=None, parent_id=None):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if not db.execute("SELECT id FROM products WHERE id=?", (product_id,)).fetchone():
        return {"ok": False, "error": "Ürün bulunamadı."}
    user = db.execute("SELECT id,role FROM users WHERE id=?", (user_id,)).fetchone()
    if not user: return {"ok": False, "error": "Kullanıcı bulunamadı."}

    body = sanitize_comment(body)
    if not body or len(body) < 3:
        return {"ok": False, "error": "Yorum en az 3 karakter olmalı."}
    if rating is not None and (rating < 1 or rating > 5):
        return {"ok": False, "error": "Puan 1-5 arası olmalı."}

    if parent_id:
        p = db.execute("SELECT id,product_id FROM comments WHERE id=? AND is_deleted=0", (parent_id,)).fetchone()
        if not p: return {"ok": False, "error": "Yanıtlanacak yorum bulunamadı."}
        if p["product_id"] != product_id: return {"ok": False, "error": "Yorum farklı ürüne ait."}
        rating = None

    if user["role"] != "admin":
        last = db.execute("SELECT created_at FROM comments WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        if last:
            diff = (datetime.utcnow() - datetime.strptime(last["created_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()
            if diff < COOLDOWN_SEC:
                return {"ok": False, "error": f"{int(COOLDOWN_SEC - diff)} sn sonra tekrar dene."}
        today = datetime.utcnow().strftime("%Y-%m-%d")
        cnt = db.execute("SELECT COUNT(*) FROM comments WHERE user_id=? AND DATE(created_at)=?", (user_id, today)).fetchone()[0]
        if cnt >= MAX_PER_DAY:
            return {"ok": False, "error": f"Günlük limit ({MAX_PER_DAY})."}

    cur = db.execute("INSERT INTO comments(product_id,user_id,parent_id,body,rating,created_at) VALUES(?,?,?,?,?,?)",
                     (product_id, user_id, parent_id, body, rating, now))
    db.commit()
    return {"ok": True, "comment_id": cur.lastrowid}


def get_product_comments(db, product_id, page=1, per_page=20, sort="newest"):
    offset = (page - 1) * per_page
    order = {"newest":"c.created_at DESC","oldest":"c.created_at ASC",
             "top":"(c.upvotes-c.downvotes) DESC"}.get(sort, "c.created_at DESC")

    comments = db.execute(f"""
        SELECT c.*, u.username FROM comments c JOIN users u ON c.user_id=u.id
        WHERE c.product_id=? AND c.parent_id IS NULL AND c.is_deleted=0
        ORDER BY {order} LIMIT ? OFFSET ?
    """, (product_id, per_page, offset)).fetchall()

    result = []
    for c in comments:
        d = dict(c)
        d["replies"] = [dict(r) for r in db.execute("""
            SELECT c.*, u.username FROM comments c JOIN users u ON c.user_id=u.id
            WHERE c.parent_id=? AND c.is_deleted=0 ORDER BY c.created_at ASC LIMIT 50
        """, (c["id"],)).fetchall()]
        result.append(d)

    total = db.execute("SELECT COUNT(*) FROM comments WHERE product_id=? AND parent_id IS NULL AND is_deleted=0",
                       (product_id,)).fetchone()[0]

    rd = db.execute("SELECT AVG(rating) as avg, COUNT(rating) as cnt FROM comments WHERE product_id=? AND rating IS NOT NULL AND is_deleted=0",
                    (product_id,)).fetchone()
    dist = {}
    for i in range(1,6):
        dist[i] = db.execute("SELECT COUNT(*) FROM comments WHERE product_id=? AND rating=? AND is_deleted=0",
                              (product_id, i)).fetchone()[0]

    return {"comments": result, "total": total,
            "avg_rating": round(rd["avg"],1) if rd["avg"] else None,
            "rated_count": rd["cnt"], "rating_dist": dist}


def vote_comment(db, comment_id, user_id, vote):
    if vote not in (1,-1): return {"ok": False, "error": "Geçersiz oy."}
    c = db.execute("SELECT id,user_id FROM comments WHERE id=?", (comment_id,)).fetchone()
    if not c: return {"ok": False, "error": "Yorum bulunamadı."}
    if c["user_id"] == user_id: return {"ok": False, "error": "Kendi yorumunu oylayamazsın."}

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    ex = db.execute("SELECT id,vote FROM comment_votes WHERE comment_id=? AND user_id=?", (comment_id, user_id)).fetchone()
    du, dd = 0, 0
    if ex:
        if ex["vote"] == vote:
            db.execute("DELETE FROM comment_votes WHERE id=?", (ex["id"],))
            du = -1 if vote == 1 else 0; dd = -1 if vote == -1 else 0
        else:
            db.execute("UPDATE comment_votes SET vote=?,created_at=? WHERE id=?", (vote, now, ex["id"]))
            du = 1 if vote == 1 else -1; dd = 1 if vote == -1 else -1
    else:
        db.execute("INSERT INTO comment_votes(comment_id,user_id,vote,created_at) VALUES(?,?,?,?)",
                   (comment_id, user_id, vote, now))
        du = 1 if vote == 1 else 0; dd = 1 if vote == -1 else 0

    db.execute("UPDATE comments SET upvotes=MAX(0,upvotes+?), downvotes=MAX(0,downvotes+?) WHERE id=?",
               (du, dd, comment_id))
    db.commit()
    u = db.execute("SELECT upvotes,downvotes FROM comments WHERE id=?", (comment_id,)).fetchone()
    return {"ok": True, "upvotes": u["upvotes"], "downvotes": u["downvotes"]}


def delete_comment(db, comment_id, user_id, is_admin=False):
    c = db.execute("SELECT id,user_id FROM comments WHERE id=?", (comment_id,)).fetchone()
    if not c: return {"ok": False, "error": "Yorum bulunamadı."}
    if c["user_id"] != user_id and not is_admin:
        return {"ok": False, "error": "Yetkin yok."}
    db.execute("UPDATE comments SET is_deleted=1,updated_at=? WHERE id=?",
               (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), comment_id))
    db.commit()
    return {"ok": True}
