"""
Bot Telegram de suivi de dépenses — 1 fichier, Python + SQLite.

Installation :
    pip install "python-telegram-bot>=21" matplotlib

Lancement (PowerShell) :
    $env:BOT_TOKEN="123456:ABC..."   # token obtenu via @BotFather
    python bot.py

Utilisation : envoie "2500 transport" (ou "transport 2500") au bot.
Commandes : /jour /semaine /mois /export /annuler /aide
"""
import csv
import io
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

DB_PATH = os.getenv("DB_PATH", "depenses.db")
CURRENCY = "FCFA"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)

# ---------------------------------------------------------------- Base de données


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                category TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_date "
            "ON expenses(user_id, created_at)"
        )


def add_expense(user_id, amount, category):
    with conn() as c:
        c.execute(
            "INSERT INTO expenses (user_id, amount, category, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, amount, category, datetime.now().isoformat(timespec="seconds")),
        )


def delete_last(user_id):
    """Supprime la dernière dépense de l'utilisateur et la renvoie."""
    with conn() as c:
        row = c.execute(
            "SELECT id, amount, category FROM expenses "
            "WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row:
            c.execute("DELETE FROM expenses WHERE id=?", (row[0],))
        return row


def totals_by_category(user_id, since):
    with conn() as c:
        return c.execute(
            "SELECT category, SUM(amount) FROM expenses "
            "WHERE user_id=? AND created_at>=? "
            "GROUP BY category ORDER BY 2 DESC",
            (user_id, since.isoformat(timespec="seconds")),
        ).fetchall()


def all_expenses(user_id):
    with conn() as c:
        return c.execute(
            "SELECT created_at, category, amount FROM expenses "
            "WHERE user_id=? ORDER BY created_at",
            (user_id,),
        ).fetchall()


# ---------------------------------------------------------------- Utilitaires


def fmt(n):
    return f"{n:,}".replace(",", " ") + f" {CURRENCY}"


def period_start(period):
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "jour":
        return today
    if period == "semaine":
        return today - timedelta(days=today.weekday())  # lundi
    return today.replace(day=1)


AMOUNT_FIRST = re.compile(r"^\s*(\d[\d\s.,]*)\s+(\D.*?)\s*$")
AMOUNT_LAST = re.compile(r"^\s*(\D.*?)\s+(\d[\d\s.,]*)\s*$")


def parse_expense(text):
    """'2500 transport' ou 'transport 2500' -> (2500, 'transport')."""
    m = AMOUNT_FIRST.match(text)
    if m:
        raw, category = m.group(1), m.group(2)
    else:
        m = AMOUNT_LAST.match(text)
        if not m:
            return None
        category, raw = m.group(1), m.group(2)
    amount = int(re.sub(r"\D", "", raw) or 0)
    if amount <= 0:
        return None
    return amount, category.strip().lower()


def make_chart(rows, title):
    """Graphique en barres (PNG en mémoire) ou None si matplotlib absent."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    labels = [r[0] for r in rows][::-1]
    values = [r[1] for r in rows][::-1]
    fig, ax = plt.subplots(figsize=(7, max(3, 0.55 * len(rows) + 1)))
    bars = ax.barh(labels, values, color="#2563eb")
    ax.bar_label(bars, labels=[fmt(v) for v in values], padding=4, fontsize=9)
    ax.set_title(title)
    ax.set_xlim(0, max(values) * 1.3)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------- Commandes

AIDE = (
    "💸 Suivi de dépenses\n\n"
    "Envoie une dépense :\n"
    "  2500 transport\n"
    "  marché 10000\n\n"
    "Commandes :\n"
    "/jour — total du jour\n"
    "/semaine — total de la semaine\n"
    "/mois — total du mois + graphique\n"
    "/export — fichier CSV\n"
    "/annuler — supprime la dernière dépense"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(AIDE)


def make_report(period):
    labels = {"jour": "du jour", "semaine": "de la semaine", "mois": "du mois"}

    async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
        rows = totals_by_category(update.effective_user.id, period_start(period))
        if not rows:
            await update.message.reply_text("Aucune dépense sur cette période.")
            return
        total = sum(r[1] for r in rows)
        lines = [f"📊 Dépenses {labels[period]}\n"]
        lines += [f"• {cat} : {fmt(amt)}" for cat, amt in rows]
        lines.append(f"\nTotal : {fmt(total)}")
        await update.message.reply_text("\n".join(lines))

        if period == "mois":
            chart = make_chart(rows, "Dépenses du mois par catégorie")
            if chart:
                await update.message.reply_photo(photo=chart)

    return report


async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = all_expenses(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Rien à exporter pour le moment.")
        return
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["date", "categorie", "montant"])
    writer.writerows(rows)
    # utf-8-sig : s'ouvre correctement dans Excel
    data = out.getvalue().encode("utf-8-sig")
    await update.message.reply_document(document=data, filename="depenses.csv")


async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = delete_last(update.effective_user.id)
    if row:
        await update.message.reply_text(f"↩️ Supprimé : {fmt(row[1])} — {row[2]}")
    else:
        await update.message.reply_text("Aucune dépense à annuler.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = parse_expense(update.message.text)
    if not parsed:
        await update.message.reply_text(
            "Je n'ai pas compris. Exemple : 2500 transport\n(/aide pour la liste des commandes)"
        )
        return
    amount, category = parsed
    user_id = update.effective_user.id
    add_expense(user_id, amount, category)
    today_total = sum(r[1] for r in totals_by_category(user_id, period_start("jour")))
    await update.message.reply_text(
        f"✅ {fmt(amount)} — {category}\nTotal du jour : {fmt(today_total)}"
    )


# ---------------------------------------------------------------- Lancement


def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Définis la variable d'environnement BOT_TOKEN.")

    init_db()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "aide"], start))
    for p in ("jour", "semaine", "mois"):
        app.add_handler(CommandHandler(p, make_report(p)))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("annuler", undo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logging.info("Bot démarré.")
    app.run_polling()


if __name__ == "__main__":
    main()