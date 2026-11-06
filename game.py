import asyncio
import json
import os
import random
import secrets
import sqlite3
from aiohttp import web
from contextlib import closing
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

TOKEN = os.getenv("BOT_TOKEN", "")
DB_URL = os.getenv("DATABASE_URL", "")
DB_PATH = os.getenv("GAME_DB", "life.db")
PROXY = os.getenv("PROXY", "")
TICK_MINUTES = int(os.getenv("TICK_MINUTES", "15"))
YEARS_PER_TICK = float(os.getenv("YEARS_PER_TICK", "0.25"))

USE_PG = bool(DB_URL)
dp = Dispatcher()

if USE_PG:
    import psycopg

    def conn():
        return psycopg.connect(DB_URL, autocommit=True)

    def adapt(sql):
        return sql.replace("?", "%s")

    def q(sql, args=()):
        with conn() as c, c.cursor() as cur:
            cur.execute(adapt(sql), args)
            return cur.fetchall()

    def x(sql, args=()):
        sql = adapt(sql)
        if sql.lstrip().upper().startswith("INSERT"):
            sql += " RETURNING id"
        with conn() as c, c.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None

    ID_COL = "BIGSERIAL PRIMARY KEY"
    TS = "TIMESTAMPTZ"
else:
    def q(sql, args=()):
        with closing(sqlite3.connect(DB_PATH)) as c, c:
            return c.execute(sql, args).fetchall()

    def x(sql, args=()):
        with closing(sqlite3.connect(DB_PATH)) as c, c:
            return c.execute(sql, args).lastrowid

    ID_COL = "INTEGER PRIMARY KEY AUTOINCREMENT"
    TS = "TEXT"

NAMES_M = ["Артём", "Иван", "Марк", "Тимур", "Лев", "Пётр", "Данил"]
NAMES_F = ["Аня", "Маша", "Вера", "Соня", "Лиза", "Дина", "Настя"]

STAGES = [
    (0, "младенец", "👶"),
    (3, "детсад", "🧸"),
    (7, "школьник", "🎒"),
    (17, "студент", "🎓"),
    (23, "взрослый", "🧑"),
    (60, "пенсионер", "👴"),
]

JOBS = {"офис": 800, "завод": 1100, "магазин": 600, "фриланс": 900, "врач": 1300}


def stage_of(age):
    for start, name, icon in reversed(STAGES):
        if age >= start:
            return name, icon
    return STAGES[0][1], STAGES[0][2]


def init_db():
    ddl = f"""
        CREATE TABLE IF NOT EXISTS people (
            id {ID_COL},
            owner BIGINT NOT NULL,
            name TEXT NOT NULL,
            sex TEXT NOT NULL,
            age REAL NOT NULL DEFAULT 0,
            health INTEGER NOT NULL DEFAULT 100,
            smarts INTEGER NOT NULL DEFAULT 50,
            mood INTEGER NOT NULL DEFAULT 70,
            money INTEGER NOT NULL DEFAULT 0,
            job TEXT NOT NULL DEFAULT '',
            study TEXT NOT NULL DEFAULT '',
            partner_id BIGINT,
            tick INTEGER NOT NULL DEFAULT 0,
            alive INTEGER NOT NULL DEFAULT 1,
            cause TEXT NOT NULL DEFAULT '',
            log TEXT NOT NULL DEFAULT '[]',
            created {TS} NOT NULL
        );
        CREATE TABLE IF NOT EXISTS relations (
            id {ID_COL},
            a BIGINT NOT NULL,
            b BIGINT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created {TS} NOT NULL
        );
        CREATE TABLE IF NOT EXISTS invites (
            code TEXT PRIMARY KEY,
            owner BIGINT NOT NULL,
            person_id BIGINT NOT NULL
        );
    """
    if USE_PG:
        with conn() as c:
            c.execute(ddl)
    else:
        with closing(sqlite3.connect(DB_PATH)) as c, c:
            c.executescript(ddl)


def now():
    return datetime.utcnow().isoformat(timespec="seconds")


def get_person(pid):
    rows = q("SELECT * FROM people WHERE id = ?", (pid,))
    if not rows:
        return None
    keys = [
        "id", "owner", "name", "sex", "age", "health", "smarts", "mood", "money",
        "job", "study", "partner_id", "tick", "alive", "cause", "log", "created",
    ]
    return dict(zip(keys, rows[0]))


def get_owned(uid, alive_only=True):
    sql = "SELECT id FROM people WHERE owner = ?"
    if alive_only:
        sql += " AND alive = 1"
    sql += " ORDER BY id DESC"
    return [r[0] for r in q(sql, (uid,))]


def mine_or_none(uid):
    ids = get_owned(uid)
    return get_person(ids[0]) if ids else None


def add_log(p, text):
    log = json.loads(p["log"] or "[]")
    log.append(text)
    log = log[-40:]
    x("UPDATE people SET log = ? WHERE id = ?", (json.dumps(log, ensure_ascii=False), p["id"]))


def bar(v):
    v = max(0, min(100, int(v)))
    return "█" * (v // 10) + "░" * (10 - v // 10)


def age_line(p):
    stage, icon = stage_of(p["age"])
    return f"{icon} {stage} · {p['age']:.1f} лет"


def card(p):
    partner = ""
    if p["partner_id"]:
        other = get_person(p["partner_id"])
        if other:
            partner = f"\n💍 В браке: {other['name']} ({other['age']:.0f} лет)"
    if not p["alive"]:
        return (
            f"🪦 {p['name']}, {p['age']:.0f} лет\n"
            f"Причина смерти: {p['cause'] or 'неизвестна'}\n\n"
            "Последние события:\n" + "\n".join(json.loads(p["log"] or "[]")[-8:])
        )
    return (
        f"{p['name']} — {age_line(p)}\n"
        f"❤️ здоровье {p['health']} ({bar(p['health'])})\n"
        f"🧠 ум {p['smarts']} · 🙂 настроение {p['mood']}\n"
        f"💰 {p['money']} ₽ · 🎓 {p['study'] or 'не учится'} · 💼 {p['job'] or 'без работы'}"
        f"{partner}"
    )


def kb(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
        ]
    )


def main_kb(p):
    if not p["alive"]:
        return kb([[("📜 Прожитая жизнь", "life")], [("👶 Новая жизнь", "born")]])
    age = p["age"]
    rows = []
    if age < 7:
        rows.append([("🧸 В детский сад", "act:kindergarten")] if p["study"] != "детский сад"
                    else [("🧸 Уйти из садика", "act:leave_kindergarten")])
    if 7 <= age < 17:
        rows.append([("🎒 В школу", "act:school")] if p["study"] != "школа"
                    else [("🎒 Бросить школу", "act:drop_school")])
    if 17 <= age < 60 and p["study"] in ("школа", "выпускник"):
        rows.append([("🎓 Поступить в вуз", "act:university"),
                     ("💼 Пойти работать", "act:job_menu")])
    if p["study"] == "вуз":
        rows.append([("🎓 Учиться", "act:learn"), ("🚪 Бросить вуз", "act:drop_university")])
    if p["job"]:
        rows.append([("💼 Сменить работу", "act:job_menu"), ("🗑 Уволиться", "act:quit_job")])
        rows.append([("🛠 Поработать сверхурочно", "act:overtime")])
    elif 17 <= age < 60:
        rows.append([("💼 Устроиться на работу", "act:job_menu")])
    rows.append([("🍲 Покормить", "act:feed"), ("🏃 Спорт", "act:sport")])
    rows.append([("🎮 Отдых", "act:rest")])
    if p["partner_id"]:
        rows.append([("🏡 Побыть с семьёй", "act:family"), ("💔 Развестись", "action:divorce")])
    rows.append([("🤝 Общение", "social"), ("🔗 Код друга", "invite")])
    rows.append([("📜 История жизни", "life"), ("❓ Помощь", "help")])
    return kb(rows)


class Born(StatesGroup):
    name = State()


@dp.message(CommandStart())
async def start(m: Message):
    p = mine_or_none(m.from_user.id)
    text = (
        "🌱 <b>Проживи жизнь</b>\n\n"
        "У тебя будет человечек, который родится, пойдёт в садик, школу, найдёт работу,"
        " встретит кого-то и однажды умрёт. Влиять на него можешь только ты.\n\n"
        f"Время идёт само: каждые {TICK_MINUTES} мин он становится старше на {YEARS_PER_TICK} года.\n"
        "Общаться с человечками других игроков можно через код друга (кнопка «Код друга»)."
    )
    rows = []
    if p:
        text += f"\n\nТвой человечек: {p['name']}, {p['age']:.1f} лет."
        rows.append([("▶️ Открыть", f"open:{p['id']}")])
    rows.append([("👶 Родить нового", "born")])
    await m.answer(text, reply_markup=kb(rows))


@dp.callback_query(F.data == "born")
async def born(c: CallbackQuery, state: FSMContext):
    await state.set_state(Born.name)
    sex = random.choice(["м", "ж"])
    suggestion = random.choice(NAMES_M if sex == "м" else NAMES_F)
    await c.message.answer(
        f"Как назвать? Напиши имя сообщением.\nНапример: <b>{suggestion}</b>",
        reply_markup=kb([[("⬅️ Отмена", "none")]]),
    )
    await c.answer()


@dp.callback_query(F.data == "none")
async def none(c: CallbackQuery):
    await c.answer()


@dp.message(Born.name)
async def born_name(m: Message, state: FSMContext):
    name = (m.text or "").strip()[:24]
    if len(name) < 2:
        return await m.answer("Имя слишком короткое, напиши ещё раз")
    await state.clear()
    sex = random.choice(["м", "ж"])
    pid = x(
        "INSERT INTO people (owner, name, sex, age, health, smarts, mood, money, created)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (m.from_user.id, name, sex, 0, random.randint(70, 100), random.randint(30, 70),
         random.randint(50, 90), 0, now()),
    )
    p = get_person(pid)
    add_log(p, f"Родился(ась) {name}.")
    await m.answer(f"🎉 Родился(ась) <b>{name}</b>!\n\n" + card(p), reply_markup=main_kb(p))


@dp.callback_query(F.data.startswith("open:"))
async def open_person(c: CallbackQuery):
    p = get_person(int(c.data.split(":")[1]))
    if not p or p["owner"] != c.from_user.id:
        return await c.answer("Не твой человечек", show_alert=True)
    await c.message.edit_text(card(p), reply_markup=main_kb(p))
    await c.answer()


@dp.callback_query(F.data == "card")
async def show_card(c: CallbackQuery):
    p = mine_or_none(c.from_user.id)
    if not p:
        return await c.answer("Нет человечка", show_alert=True)
    await c.message.edit_text(card(p), reply_markup=main_kb(p))
    await c.answer()


def apply(pid, changes):
    sets = ", ".join(
        f"{k} = MIN(100, MAX(0, {k} + ?))" if k in ("health", "smarts", "mood")
        else f"{k} = ?" for k in changes
    )
    x(f"UPDATE people SET {sets} WHERE id = ?", list(changes.values()) + [pid])


def grow_person(p, add_years):
    age = p["age"] + add_years
    changes = {"age": age}
    notes = []

    if age < 3:
        changes["mood"] = p["mood"] + 1
    changes["health"] = p["health"] - 1
    if p["job"] and age < 65:
        gain = JOBS.get(p["job"], 700) if add_years >= 1 else random.randint(300, 900)
        changes["money"] = p["money"] + gain
        notes.append(f"Зарплата: +{gain} ₽")
    if p["job"] and age >= 65:
        changes["job"] = ""
        notes.append("Вышел(ла) на пенсию.")
    if p["study"] == "школа":
        changes["smarts"] = p["smarts"] + (4 if add_years >= 1 else 1)
        if age >= 18:
            changes["study"] = "выпускник"
            notes.append("Школа закончена.")
    elif p["study"] == "вуз":
        changes["smarts"] = p["smarts"] + (5 if add_years >= 1 else 1)
        if age >= 23:
            changes["study"] = "выпускник вуза"
            notes.append("Вуз закончен.")
    elif age < 3:
        changes["smarts"] = p["smarts"] + 1

    chance = 0.4 if add_years >= 1 else 0.35
    event = roll_event(age, chance)
    if event:
        name_e, eff = event
        for k, v in eff.items():
            changes[k] = changes.get(k, p[k]) + v
        notes.append(f"Событие: {name_e}.")

    apply(p["id"], changes)
    for n in notes:
        add_log(p, n)
    return notes


def age_text(p, add_years):
    if add_years == 1:
        return f"Исполнилось {p['age']:.0f} лет."
    return f"Стал(а) старше на {add_years:g} года, теперь {p['age']:.1f} лет."


@dp.callback_query(F.data == "skip")
async def skip(c: CallbackQuery):
    p = mine_or_none(c.from_user.id)
    if not p:
        return await c.answer("Сначала роди человечка", show_alert=True)
    if not p["alive"]:
        return await c.answer("Он уже умер", show_alert=True)
    add_log(p, f"Год прошёл спокойно, {p['age']:.1f} лет.")
    await c.message.edit_text(card(get_person(p["id"])), reply_markup=main_kb(get_person(p["id"])))
    await c.answer("Год прошёл без изменений. Возраст прежний.")


@dp.callback_query(F.data == "help")
async def help_cb(c: CallbackQuery):
    await c.message.answer(
        "Как играть:\n"
        "• Время идёт само: каждые "
        f"{TICK_MINUTES} мин человечек становится старше на {YEARS_PER_TICK:g} года.\n"
        "• Садик, школа, вуз и работа — твой выбор кнопками.\n"
        "• Общение с другими игроками — через код друга (/link КОД).\n"
        "• Умереть можно в любом возрасте; от старости — после 75 лет."
    )
    await c.answer()


EVENTS = [
    ("Прогулка под дождём", 3, 90, {"health": -3}),
    ("Нашёл(а) 500 ₽ под подъездом", 5, 2000, {"money": 500}),
    ("Удачно выступил(а)", 7, 100, {"mood": 8, "smarts": 3}),
    ("Заболел(а) простудой", 1, 90, {"health": -8}),
    ("Повысили зарплату", 18, 3200, {"money": 3000, "mood": 10}),
    ("Познакомился(ась) с соседом", 3, 90, {"mood": 4}),
]

DEATHS = [
    ("несчастный случай", 0, 120),
    ("внезапная болезнь", 0, 70),
    ("упал(а) с лестницы", 5, 40),
    ("авария", 17, 80),
]


def roll_event(age, chance):
    if random.random() > chance:
        return None
    for name, lo, hi, eff in random.sample(EVENTS, len(EVENTS)):
        if lo <= age <= hi:
            return name, eff
    return None


def death_chance(p):
    age = p["age"]
    if age < 60:
        base = 0.03 if age < 18 else 0.02
    else:
        base = 0.05 + (age - 60) * 0.02
    return base * (1.0 + (100 - max(0, p["health"])) / 100 * 2)


async def die(p):
    cause = "старость"
    if p["age"] < 75:
        options = [d for d in DEATHS if d[1] <= p["age"] <= d[2]] or DEATHS
        cause = random.choice(options)[0]
    x("UPDATE people SET alive = 0, cause = ? WHERE id = ?", (cause, p["id"]))
    if p["partner_id"]:
        x("UPDATE people SET partner_id = NULL WHERE id = ?", (p["partner_id"],))
    add_log(get_person(p["id"]), f"Умер(ла): {cause}.")


ACTIONS = {
    "kindergarten": ("пошёл(шла) в детский сад", {"smarts": 3, "mood": 2}, 3, {"study": "детский сад"}),
    "leave_kindergarten": ("ушёл(шла) из садика", {"mood": -2}, 3, {"study": ""}),
    "school": ("пошёл(шла) в школу", {"smarts": 4}, 7, {"study": "школа"}),
    "drop_school": ("бросил(а) школу", {"smarts": -5, "mood": 2}, 7, {"study": ""}),
    "university": ("поступил(а) в вуз", {"smarts": 8, "mood": -2}, 17, {"study": "вуз"}),
    "drop_university": ("бросил(а) вуз", {"mood": 3, "smarts": -3}, 17, {"study": ""}),
    "quit_job": ("уволился(ась)", {"mood": 5}, 17, {"job": ""}),
    "feed": ("поел(а) досыта", {"health": 4, "mood": 4}, 0, {}, 100),
    "sport": ("позанимался(ась) спортом", {"health": 6, "mood": 2}, 4, {}),
    "rest": ("отдохнул(а)", {"mood": 8}, 0, {}),
    "learn": ("поучился(ась)", {"smarts": 6, "mood": -3}, 5, {}),
    "overtime": ("поработал(а) сверхурочно", {"money": 1500, "health": -4, "mood": -6}, 17, {}),
    "family": ("провёл(а) вечер с семьёй", {"mood": 8, "health": 1}, 18, {}),
}


@dp.callback_query(F.data.startswith("act:"))
async def action(c: CallbackQuery):
    key = c.data.split(":")[1]
    p = mine_or_none(c.from_user.id)
    if not p:
        return await c.answer("Сначала роди человечка: /start", show_alert=True)
    if key == "job_menu":
        rows = [[(f"💼 {j} · {s} ₽/год", f"job:{j}")] for j, s in JOBS.items()]
        rows.append([("⬅️ Назад", "card")])
        await c.message.edit_text("Куда устроиться?", reply_markup=kb(rows))
        return await c.answer()

    a = ACTIONS.get(key)
    if not a:
        return await c.answer("Неизвестное действие", show_alert=True)
    text, eff, min_age, *rest = a
    cost = rest[1] if len(rest) > 1 else 0
    if p["age"] < min_age:
        return await c.answer(f"Слишком рано: нужно {min_age} лет", show_alert=True)
    if key == "family" and not p["partner_id"]:
        return await c.answer("У него нет пары", show_alert=True)
    if cost and p["money"] < cost:
        return await c.answer(f"Нужно {cost} ₽", show_alert=True)

    changes = dict(eff)
    if cost:
        changes["money"] = -cost
    if rest:
        changes.update(rest[0])
    apply(p["id"], changes)
    add_log(p, f"В {p['age']:.0f} лет {text}.")
    p = get_person(p["id"])
    await c.message.edit_text(card(p), reply_markup=main_kb(p))
    await c.answer(f"{p['name']} {text}")


@dp.callback_query(F.data.startswith("job:"))
async def set_job(c: CallbackQuery):
    job = c.data.split(":")[1]
    p = mine_or_none(c.from_user.id)
    if not p or job not in JOBS:
        return await c.answer("Нет такого", show_alert=True)
    if not 17 <= p["age"] < 65:
        return await c.answer("Работа с 17 до 65 лет", show_alert=True)
    apply(p["id"], {"job": job, "mood": p["mood"] - 2})
    add_log(p, f"Устроился(ась) на работу: {job}.")
    p = get_person(p["id"])
    await c.message.edit_text(card(p), reply_markup=main_kb(p))
    await c.answer(f"Теперь работает: {job}")


@dp.callback_query(F.data == "life")
async def life(c: CallbackQuery):
    p = mine_or_none(c.from_user.id)
    if not p:
        return await c.answer("Нет человечка", show_alert=True)
    log = json.loads(p["log"] or "[]")
    head = "🪦 " + card(p) if not p["alive"] else f"📜 История {p['name']}"
    body = "\n".join(f"{i}. {t}" for i, t in enumerate(log[-25:], 1)) or "Пока пусто"
    await c.message.edit_text(f"{head}\n\n{body}", reply_markup=main_kb(p))
    await c.answer()


@dp.callback_query(F.data == "invite")
async def invite(c: CallbackQuery):
    p = mine_or_none(c.from_user.id)
    if not p:
        return await c.answer("Нет человечка", show_alert=True)
    rows = q("SELECT code FROM invites WHERE person_id = ?", (p["id"],))
    code = rows[0][0] if rows else secrets.token_hex(3).upper()
    if not rows:
        x("INSERT INTO invites (code, owner, person_id) VALUES (?,?,?)",
          (code, c.from_user.id, p["id"]))
    await c.message.answer(
        f"Код твоего человечка: <code>{code}</code>\n\n"
        "Передай другу. Он введёт:\n"
        f"<code>/link {code}</code>\n"
        "После этого вы сможете дружить, ссориться или пожениться."
    )
    await c.answer()


@dp.message(Command("link"))
async def link(m: Message):
    parts = (m.text or "").split()
    if len(parts) != 2:
        return await m.answer("Формат: /link КОД")
    rows = q("SELECT person_id, owner FROM invites WHERE code = ?", (parts[1].upper(),))
    if not rows:
        return await m.answer("Код не найден")
    other_id, _ = rows[0]
    me = mine_or_none(m.from_user.id)
    if not me:
        return await m.answer("Сначала роди человечка: /start")
    if other_id == me["id"]:
        return await m.answer("Это твой же код")
    for a, b in ((me["id"], other_id), (other_id, me["id"])):
        if not q("SELECT id FROM relations WHERE a = ? AND b = ?", (a, b)):
            x("INSERT INTO relations (a, b, kind, created) VALUES (?,?,?,?)", (a, b, "friend", now()))
    o = get_person(other_id)
    await m.answer(
        f"Ты познакомился(ась) с человечком <b>{o['name']}</b> ({o['age']:.1f} лет).",
        reply_markup=kb([[("🤝 Общение", "social")]]),
    )


@dp.callback_query(F.data == "social")
async def social(c: CallbackQuery):
    me = mine_or_none(c.from_user.id)
    if not me:
        return await c.answer("Нет человечка", show_alert=True)
    rel = q("SELECT b FROM relations WHERE a = ?", (me["id"],))
    rows = []
    for (oid,) in rel:
        o = get_person(oid)
        if not o or not o["alive"]:
            continue
        icon = "💍" if me["partner_id"] == oid else "👤"
        rows.append([(f"{icon} {o['name']} · {o['age']:.0f}", f"person:{oid}")])
    if not rows:
        return await c.message.edit_text(
            "Пока ни с кем не знаком. Свой код — кнопка «Код друга»,\n"
            "код друга вводится командой /link КОД.",
            reply_markup=main_kb(me),
        )
    rows.append([("⬅️ Назад", "card")])
    await c.message.edit_text("С кем общаемся:", reply_markup=kb(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("person:"))
async def person(c: CallbackQuery):
    me = mine_or_none(c.from_user.id)
    o = get_person(int(c.data.split(":")[1]))
    if not me or not o:
        return await c.answer("Нет такого", show_alert=True)
    rows = [[("🤝 Дружить", f"rel:{o['id']}:friend"), ("💢 Поссориться", f"rel:{o['id']}:quarrel")]]
    if me["age"] >= 18 and o["age"] >= 18 and not me["partner_id"] and not o["partner_id"]:
        rows.append([("💍 Предложить брак", f"rel:{o['id']}:marry")])
    rows.append([("⬅️ Назад", "social")])
    header = f"{o['name']} — {age_line(o)}\n❤️{o['health']} 🧠{o['smarts']} 🙂{o['mood']}\n\n"
    await c.message.edit_text(header + "Что делаем?", reply_markup=kb(rows))
    await c.answer()


@dp.callback_query(F.data.startswith("rel:"))
async def relation(c: CallbackQuery):
    _, oid, kind = c.data.split(":")
    oid = int(oid)
    me = mine_or_none(c.from_user.id)
    o = get_person(oid)
    if not me or not o:
        return await c.answer("Нет такого", show_alert=True)
    if kind == "friend":
        x("UPDATE people SET mood = MIN(100, mood + 5) WHERE id IN (?,?)", (me["id"], oid))
        x("UPDATE relations SET kind = 'friend' WHERE a = ? AND b = ?", (me["id"], oid))
        msg = f"{me['name']} и {o['name']} теперь дружат"
        add_log(me, f"Подружился(ась) с {o['name']}.")
    elif kind == "quarrel":
        x("UPDATE people SET mood = MAX(0, mood - 6) WHERE id IN (?,?)", (me["id"], oid))
        x("UPDATE relations SET kind = 'quarrel' WHERE a = ? AND b = ?", (me["id"], oid))
        msg = f"{me['name']} и {o['name']} поссорились"
        add_log(me, f"Поссорился(ась) с {o['name']}.")
    else:
        if me["partner_id"] or o["partner_id"]:
            msg = "Не получилось: кто-то уже в браке"
        else:
            x("UPDATE people SET partner_id = ? WHERE id = ?", (oid, me["id"]))
            x("UPDATE people SET partner_id = ? WHERE id = ?", (me["id"], oid))
            x("UPDATE relations SET kind = 'marry' WHERE a = ? AND b = ?", (me["id"], oid))
            x("UPDATE people SET mood = MIN(100, mood + 15) WHERE id IN (?,?)", (me["id"], oid))
            msg = f"💍 {me['name']} и {o['name']} поженились!"
            add_log(get_person(me["id"]), f"Женился(ась) на {o['name']}.")
    await c.answer(msg, show_alert=True)
    p = get_person(me["id"])
    await c.message.edit_text(card(p), reply_markup=main_kb(p))


@dp.callback_query(F.data == "action:divorce")
async def divorce(c: CallbackQuery):
    me = mine_or_none(c.from_user.id)
    if not me:
        return await c.answer("Нет человечка", show_alert=True)
    if not me["partner_id"]:
        return await c.answer("Он не в браке", show_alert=True)
    other = me["partner_id"]
    x("UPDATE people SET partner_id = NULL WHERE id IN (?,?)", (me["id"], other))
    x("UPDATE people SET mood = MAX(0, mood - 12) WHERE id IN (?,?)", (me["id"], other))
    add_log(get_person(me["id"]), "Развёлся(ась).")
    await c.answer("Развод оформлен", show_alert=True)
    p = get_person(me["id"])
    await c.message.edit_text(card(p), reply_markup=main_kb(p))


async def life_tick(bot: Bot):
    while True:
        await asyncio.sleep(TICK_MINUTES * 60)
        try:
            await process_tick(bot)
        except Exception as e:
            print("tick error:", e)


async def process_tick(bot: Bot):
    for (pid,) in q("SELECT id FROM people WHERE alive = 1"):
        p = get_person(pid)
        grow_person(p, YEARS_PER_TICK)
        p = get_person(pid)
        add_log(p, age_text(p, YEARS_PER_TICK))
        if p["partner_id"]:
            other = get_person(p["partner_id"])
            if other and not other["alive"]:
                x("UPDATE people SET partner_id = NULL WHERE id = ?", (pid,))
        if random.random() < death_chance(p):
            await die(p)
            p = get_person(pid)
            try:
                await bot.send_message(
                    p["owner"],
                    f"🕯 {p['name']} умер(ла) в {p['age']:.0f} лет.\nПричина: {p['cause']}.\n\n"
                    + card(p),
                    reply_markup=kb([[("👶 Начать новую жизнь", "born")]]),
                )
            except Exception:
                pass


async def health_server():
    port = int(os.getenv("PORT", "8080"))

    async def handle(request):
        return web.json_response({"status": "ok", "bot": "Mylife game"})

    app = web.Application()
    app.router.add_get("/", handle)
    app.router.add_get("/health", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Health server on 0.0.0.0:{port}", flush=True)


async def main():
    if not TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Укажите переменную окружения BOT_TOKEN.")
    init_db()
    await health_server()
    session = None
    if PROXY:
        from aiogram.client.session.aiohttp import AiohttpSession

        session = AiohttpSession(proxy=PROXY)
    bot = Bot(TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if session else Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    asyncio.create_task(life_tick(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
