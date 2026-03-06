import asyncio
import logging
import json
import os
import re
import random
from datetime import datetime
from telethon import TelegramClient
from telethon.errors import FloodWaitError, ChatWriteForbiddenError, InviteHashExpiredError, InviteHashInvalidError, SessionPasswordNeededError
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import nest_asyncio
nest_asyncio.apply()

# ========== НАСТРОЙКИ ==========
USER_API_ID = 38611409
USER_API_HASH = 'f32e667381a1ac988b8530658ffbef0b'
USER_PHONE = '+17087366241'
BOT_TOKEN = "8687777365:AAFeI8nIQcYUgyYp0Ol3Fwrx_pdSYRFLxKA"
ADMIN_CHAT_ID = 8558085032

# Каналы для мониторинга
CHANNELS = []
PRIVATE_CHANNELS = {}

# Настройки комментариев
COMMENT_TEXTS = [
    "я первый",
    "первый!",
    "кто первый?",
    "я здесь!",
    "топ 1"
]
COMMENT_TEXT = random.choice(COMMENT_TEXTS)
CHECK_INTERVAL = 30

# ========== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ==========
is_bot_running = False
last_posts = {}
DATA_FILE = "last_posts.json"
user_client = None
joined_private_channels = set()
comment_stats = {'total': 0, 'success': 0, 'failed': 0, 'last_comment_time': None}

# Режимы ожидания и данные для входа
waiting_for_private = False
waiting_for_public = False
waiting_for_text = False
waiting_for_interval = False
waiting_for_remove = False
waiting_for_auth_code = False  # Ожидание кода подтверждения
waiting_for_password = False    # Ожидание пароля 2FA
temp_auth_data = {}             # Временные данные для входа

# ========== НАСТРОЙКА ЛОГОВ ==========
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ========== ФУНКЦИИ РАБОТЫ С ДАННЫМИ ==========
def load_data():
    global last_posts, CHANNELS, PRIVATE_CHANNELS, COMMENT_TEXT, CHECK_INTERVAL, comment_stats, joined_private_channels
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                last_posts = data.get('last_posts', {})
                CHANNELS = data.get('channels', [])
                PRIVATE_CHANNELS = data.get('private_channels', {})
                joined_private_channels = set(data.get('joined_channels', []))
                COMMENT_TEXT = data.get('comment_text', COMMENT_TEXT)
                CHECK_INTERVAL = data.get('check_interval', CHECK_INTERVAL)
                comment_stats = data.get('stats', comment_stats)
            logger.info(f"📂 Загружено: {len(CHANNELS)} публичных, {len(PRIVATE_CHANNELS)} приватных")
    except Exception as e:
        logger.error(f"Ошибка загрузки: {e}")

def save_data():
    try:
        data = {
            'last_posts': last_posts,
            'channels': CHANNELS,
            'private_channels': PRIVATE_CHANNELS,
            'joined_channels': list(joined_private_channels),
            'comment_text': COMMENT_TEXT,
            'check_interval': CHECK_INTERVAL,
            'stats': comment_stats,
            'saved_at': datetime.now().isoformat()
        }
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 Данные сохранены")
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")

def extract_channel_username(text):
    text = text.strip()
    if text.startswith('@'):
        text = text[1:]
    
    patterns = [
        r'(?:https?://)?(?:www\.)?t\.me/([a-zA-Z0-9_]+)',
        r'(?:https?://)?(?:www\.)?telegram\.me/([a-zA-Z0-9_]+)',
        r'^([a-zA-Z0-9_]+)$'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            username = match.group(1)
            if username and re.match(r'^[a-zA-Z0-9_]+$', username):
                return username.lower()
    return None

def is_private_invite_link(text):
    text = text.strip()
    return bool(re.search(r'(?:https?://)?(?:www\.)?t\.me/\+([a-zA-Z0-9_-]+)', text)) or \
           bool(re.search(r'(?:https?://)?(?:www\.)?t\.me/joinchat/([a-zA-Z0-9_-]+)', text))

# ========== ФУНКЦИИ ДЛЯ ПОЛЬЗОВАТЕЛЬСКОГО КЛИЕНТА ==========
async def init_user_client(bot=None):
    """Инициализация клиента с обработкой кода и пароля через Telegram"""
    global user_client, waiting_for_auth_code, waiting_for_password, temp_auth_data
    
    try:
        if user_client is None:
            user_client = TelegramClient('user_session', USER_API_ID, USER_API_HASH)
            user_client.flood_sleep_threshold = 60
            
            # Настраиваем callback для получения кода
            user_client._phone = USER_PHONE
            
            # Запускаем клиент
            await user_client.connect()
            
            if not await user_client.is_user_authorized():
                # Отправляем запрос на код
                await user_client.send_code_request(USER_PHONE)
                
                if bot:
                    await bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text="🔐 **Требуется подтверждение входа**\n\n"
                             "На ваш Telegram отправлен код подтверждения.\n"
                             "Отправьте его сюда одним сообщением.\n\n"
                             "Или отправьте /cancel для отмены",
                        parse_mode='Markdown'
                    )
                
                # Устанавливаем режим ожидания кода
                waiting_for_auth_code = True
                temp_auth_data['phone'] = USER_PHONE
                return None
            
            me = await user_client.get_me()
            logger.info(f"✅ Уже авторизован: {me.first_name}")
            return user_client
            
    except SessionPasswordNeededError:
        # Требуется пароль 2FA
        waiting_for_password = True
        if bot:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text="🔐 **Требуется пароль двухфакторной аутентификации**\n\n"
                     "Введите ваш пароль от аккаунта Telegram.\n\n"
                     "Или отправьте /cancel для отмены",
                parse_mode='Markdown'
            )
        return None
    except Exception as e:
        logger.error(f"❌ Ошибка подключения: {e}")
        if bot:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"❌ Ошибка подключения: {str(e)[:100]}"
            )
        return None

async def complete_auth_with_code(code, bot):
    """Завершение авторизации с кодом"""
    global user_client, waiting_for_auth_code, temp_auth_data
    
    try:
        await user_client.sign_in(USER_PHONE, code)
        waiting_for_auth_code = False
        me = await user_client.get_me()
        
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"✅ **Вход выполнен успешно!**\n\n"
                 f"Аккаунт: {me.first_name} {me.last_name or ''}\n"
                 f"Username: @{me.username or 'отсутствует'}",
            parse_mode='Markdown'
        )
        
        return True
    except SessionPasswordNeededError:
        # Требуется пароль
        waiting_for_auth_code = False
        waiting_for_password = True
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="🔐 **Требуется пароль двухфакторной аутентификации**\n\n"
                 "Введите ваш пароль от аккаунта Telegram.",
            parse_mode='Markdown'
        )
        return False
    except Exception as e:
        waiting_for_auth_code = False
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"❌ Ошибка входа: {str(e)[:100]}"
        )
        return False

async def complete_auth_with_password(password, bot):
    """Завершение авторизации с паролем 2FA"""
    global user_client, waiting_for_password
    
    try:
        await user_client.sign_in(password=password)
        waiting_for_password = False
        me = await user_client.get_me()
        
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"✅ **Вход выполнен успешно!**\n\n"
                 f"Аккаунт: {me.first_name} {me.last_name or ''}\n"
                 f"Username: @{me.username or 'отсутствует'}",
            parse_mode='Markdown'
        )
        
        return True
    except Exception as e:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"❌ Ошибка входа: {str(e)[:100]}\n\nПопробуйте еще раз или /cancel"
        )
        return False

# ========== ФУНКЦИЯ ВСТУПЛЕНИЯ В ПРИВАТНЫЙ КАНАЛ ==========
async def join_private_channel(client, invite_link):
    try:
        logger.info(f"🔐 Вступление: {invite_link}")
        
        if 'joinchat/' in invite_link:
            hash_part = invite_link.split('joinchat/')[-1].split('?')[0]
        elif '+' in invite_link:
            hash_part = invite_link.split('+')[-1].split('?')[0]
        else:
            hash_part = invite_link
            
        try:
            invite = await client(CheckChatInviteRequest(hash=hash_part))
            title = getattr(invite, 'title', 'Unknown')
        except Exception as e:
            return None, f"Ошибка проверки: {e}"
        
        try:
            updates = await client(ImportChatInviteRequest(hash=hash_part))
            for chat in updates.chats:
                if hasattr(chat, 'id'):
                    channel_id = f"private_{chat.id}"
                    title = getattr(chat, 'title', 'Unknown')
                    return channel_id, title
            return None, "Не удалось получить информацию"
        except InviteHashExpiredError:
            return None, "❌ Ссылка истекла"
        except InviteHashInvalidError:
            return None, "❌ Недействительная ссылка"
        except Exception as e:
            return None, f"❌ Ошибка: {str(e)[:100]}"
    except Exception as e:
        return None, str(e)

# ========== ФУНКЦИИ КОММЕНТИРОВАНИЯ ==========
async def leave_comment(client, channel_identifier, post_id):
    global comment_stats
    try:
        if isinstance(channel_identifier, str) and channel_identifier.startswith('private_'):
            numeric_id = int(channel_identifier.replace('private_', ''))
            channel = await client.get_entity(numeric_id)
        else:
            channel = await client.get_entity(channel_identifier)
        
        post = await client.get_messages(channel, ids=int(post_id))
        if not post:
            return False
        
        comment_stats['total'] += 1
        
        try:
            await client.send_message(entity=channel, message=COMMENT_TEXT, comment_to=post.id)
            comment_stats['success'] += 1
            comment_stats['last_comment_time'] = datetime.now().isoformat()
            save_data()
            return True
        except:
            try:
                await client.send_message(channel, COMMENT_TEXT, reply_to=post.id)
                comment_stats['success'] += 1
                return True
            except:
                comment_stats['failed'] += 1
                return False
    except Exception as e:
        logger.error(f"Ошибка комментирования: {e}")
        return False

# ========== ФУНКЦИЯ ВОЗВРАТА В ГЛАВНОЕ МЕНЮ ==========
async def show_main_menu(update_or_query, text="🤖 **Управление ботом-комментатором**\n\nВыберите действие:", edit=True):
    keyboard = [
        [InlineKeyboardButton("🚀 Запустить мониторинг", callback_data='start_bot')],
        [InlineKeyboardButton("⏹ Остановить", callback_data='stop_bot')],
        [InlineKeyboardButton("📊 Статус", callback_data='status')],
        [InlineKeyboardButton("📋 Список каналов", callback_data='channels')],
        [InlineKeyboardButton("⚙️ Настройки", callback_data='settings')],
        [InlineKeyboardButton("➕ Добавить канал", callback_data='add_channel_menu')],
        [InlineKeyboardButton("🔄 Переподключить аккаунт", callback_data='reconnect_account')]
    ]
    
    if hasattr(update_or_query, 'message'):
        await update_or_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    else:
        await update_or_query.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )

# ========== ОСНОВНОЙ ОБРАБОТЧИК ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ У вас нет прав")
        return
    
    # Проверяем состояние авторизации
    global user_client
    status_text = "🤖 **Управление ботом-комментатором**\n\n"
    
    if user_client and await user_client.is_user_authorized():
        me = await user_client.get_me()
        status_text += f"✅ Аккаунт: {me.first_name}\n\n"
    else:
        status_text += "⚠️ **Требуется вход в аккаунт**\n"
        status_text += "Нажмите кнопку ниже для подключения\n\n"
    
    status_text += "Выберите действие:"
    
    keyboard = [
        [InlineKeyboardButton("🔐 Подключить аккаунт", callback_data='connect_account')],
        [InlineKeyboardButton("🚀 Запустить мониторинг", callback_data='start_bot')],
        [InlineKeyboardButton("⏹ Остановить", callback_data='stop_bot')],
        [InlineKeyboardButton("📊 Статус", callback_data='status')],
        [InlineKeyboardButton("📋 Список каналов", callback_data='channels')],
        [InlineKeyboardButton("⚙️ Настройки", callback_data='settings')],
        [InlineKeyboardButton("➕ Добавить канал", callback_data='add_channel_menu')]
    ]
    
    await update.message.reply_text(
        status_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_private, waiting_for_public, waiting_for_text, waiting_for_interval, waiting_for_remove
    global is_bot_running, COMMENT_TEXT, CHECK_INTERVAL, user_client
    
    query = update.callback_query
    await query.answer()
    
    if update.effective_user.id != ADMIN_CHAT_ID:
        await query.edit_message_text("❌ У вас нет прав")
        return
    
    if query.data == 'connect_account' or query.data == 'reconnect_account':
        # Переподключаем аккаунт
        if user_client:
            await user_client.disconnect()
            user_client = None
        
        status = await query.edit_message_text("🔄 Подключаюсь к аккаунту...")
        
        # Запускаем процесс авторизации
        client = await init_user_client(context.bot)
        
        if client:
            # Уже авторизован
            await show_main_menu(query, "✅ **Аккаунт уже подключен!**\n\nВыберите действие:")
        elif waiting_for_auth_code or waiting_for_password:
            # Ожидаем код или пароль
            await status.edit_text(
                "🔐 **Запрос отправлен**\n\n"
                "Код подтверждения отправлен на ваш Telegram.\n"
                "Отправьте его сюда одним сообщением."
            )
    
    elif query.data == 'start_bot':
        if not user_client or not await user_client.is_user_authorized():
            await query.edit_message_text("❌ Сначала подключите аккаунт (кнопка 'Подключить аккаунт')")
            return
        
        if is_bot_running:
            await query.edit_message_text("❌ Бот уже запущен!")
        else:
            is_bot_running = True
            await query.edit_message_text("🚀 Запускаю мониторинг...")
            asyncio.create_task(run_comment_bot(context.bot))
            await asyncio.sleep(1)
            await show_main_menu(query, "✅ **Мониторинг запущен!**\n\nВыберите действие:")
    
    elif query.data == 'stop_bot':
        is_bot_running = False
        await show_main_menu(query, "⏹ **Бот остановлен**\n\nВыберите действие:")
    
    elif query.data == 'status':
        text = f"📊 **СТАТУС**\n\n"
        
        # Статус аккаунта
        if user_client and await user_client.is_user_authorized():
            me = await user_client.get_me()
            text += f"👤 Аккаунт: ✅ {me.first_name}\n"
        else:
            text += f"👤 Аккаунт: ❌ не подключен\n"
        
        text += f"🟢 Мониторинг: {'✅' if is_bot_running else '❌'}\n"
        text += f"📝 Публичных каналов: {len(CHANNELS)}\n"
        text += f"🔐 Приватных каналов: {len(PRIVATE_CHANNELS)}\n"
        text += f"💬 Текст комментария: '{COMMENT_TEXT}'\n"
        text += f"⏱ Интервал проверки: {CHECK_INTERVAL} сек\n\n"
        text += f"📈 Статистика: {comment_stats['success']}/{comment_stats['total']}"
        
        keyboard = [[InlineKeyboardButton("🔙 В главное меню", callback_data='back_to_menu')]]
        await query.edit_message_text(
            text, 
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    # ... остальные обработчики кнопок (каналы, настройки и т.д.) ...
    elif query.data == 'channels':
        text = "📋 **СПИСОК КАНАЛОВ**\n\n"
        
        text += "**📢 Публичные каналы:**\n"
        if CHANNELS:
            for i, ch in enumerate(CHANNELS, 1):
                text += f"{i}. @{ch}\n"
        else:
            text += "Нет публичных каналов\n"
        
        text += "\n**🔐 Приватные каналы:**\n"
        if PRIVATE_CHANNELS:
            for i, (ch_id, link) in enumerate(PRIVATE_CHANNELS.items(), 1):
                status = "✅" if ch_id in joined_private_channels else "⏳"
                text += f"{i}. {status} {ch_id}\n"
        else:
            text += "Нет приватных каналов\n"
        
        keyboard = [
            [InlineKeyboardButton("➕ Добавить канал", callback_data='add_channel_menu')],
            [InlineKeyboardButton("➖ Удалить канал", callback_data='remove_channel_menu')],
            [InlineKeyboardButton("🔙 В главное меню", callback_data='back_to_menu')]
        ]
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif query.data == 'add_channel_menu':
        keyboard = [
            [InlineKeyboardButton("📢 Публичный канал", callback_data='add_public')],
            [InlineKeyboardButton("🔐 Приватный канал", callback_data='add_private')],
            [InlineKeyboardButton("🔙 В главное меню", callback_data='back_to_menu')]
        ]
        await query.edit_message_text(
            "➕ **ДОБАВЛЕНИЕ КАНАЛА**\n\nВыберите тип канала:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif query.data == 'remove_channel_menu':
        text = "➖ **УДАЛЕНИЕ КАНАЛА**\n\n"
        text += "Отправьте username публичного канала или ID приватного канала для удаления\n"
        text += "Например: @durov или private_123456789\n\n"
        text += "Или /cancel для отмены"
        
        waiting_for_remove = True
        await query.edit_message_text(text, parse_mode='Markdown')
    
    elif query.data == 'add_public':
        waiting_for_public = True
        await query.edit_message_text(
            "📢 **ДОБАВЛЕНИЕ ПУБЛИЧНОГО КАНАЛА**\n\n"
            "Отправьте username или ссылку:\n"
            "• durov\n"
            "• @durov\n"
            "• https://t.me/durov\n\n"
            "Или /cancel для отмены",
            parse_mode='Markdown'
        )
    
    elif query.data == 'add_private':
        waiting_for_private = True
        await query.edit_message_text(
            "🔐 **ДОБАВЛЕНИЕ ПРИВАТНОГО КАНАЛА**\n\n"
            "Отправьте ссылку-приглашение:\n"
            "• https://t.me/+COBtMLnnTos5YmEy\n"
            "• https://t.me/joinchat/COBtMLnnTos5YmEy\n\n"
            "Или /cancel для отмены",
            parse_mode='Markdown'
        )
    
    elif query.data == 'settings':
        keyboard = [
            [InlineKeyboardButton("✏️ Изменить текст", callback_data='change_text')],
            [InlineKeyboardButton("⏱ Изменить интервал", callback_data='change_interval')],
            [InlineKeyboardButton("🎲 Случайный текст", callback_data='random_text')],
            [InlineKeyboardButton("🔙 В главное меню", callback_data='back_to_menu')]
        ]
        await query.edit_message_text(
            f"⚙️ **НАСТРОЙКИ**\n\n"
            f"Текущий текст: '{COMMENT_TEXT}'\n"
            f"Интервал проверки: {CHECK_INTERVAL} сек",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif query.data == 'random_text':
        global COMMENT_TEXTS
        COMMENT_TEXT = random.choice(COMMENT_TEXTS)
        save_data()
        await query.edit_message_text(f"✅ Случайный текст выбран: '{COMMENT_TEXT}'")
        await asyncio.sleep(1)
        await show_main_menu(query, f"✅ **Текст изменен на:** '{COMMENT_TEXT}'\n\nВыберите действие:")
    
    elif query.data == 'change_text':
        waiting_for_text = True
        await query.edit_message_text(
            f"✏️ **ИЗМЕНЕНИЕ ТЕКСТА**\n\n"
            f"Текущий текст: '{COMMENT_TEXT}'\n\n"
            f"Отправьте новый текст (макс. 200 символов)\n"
            f"Или /cancel",
            parse_mode='Markdown'
        )
    
    elif query.data == 'change_interval':
        waiting_for_interval = True
        await query.edit_message_text(
            f"⏱ **ИЗМЕНЕНИЕ ИНТЕРВАЛА**\n\n"
            f"Текущий интервал: {CHECK_INTERVAL} сек\n\n"
            f"Введите новое значение (минимум 10, максимум 3600)\n"
            f"Или /cancel",
            parse_mode='Markdown'
        )
    
    elif query.data == 'back_to_menu':
        await show_main_menu(query)

# ========== ОБРАБОТЧИК СООБЩЕНИЙ ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global waiting_for_private, waiting_for_public, waiting_for_text, waiting_for_interval, waiting_for_remove
    global waiting_for_auth_code, waiting_for_password
    global COMMENT_TEXT, CHECK_INTERVAL, CHANNELS, PRIVATE_CHANNELS, joined_private_channels
    
    text = update.message.text
    user_id = update.effective_user.id
    
    if user_id != ADMIN_CHAT_ID:
        return
    
    logger.info(f"📨 Сообщение: {text}")
    
    # Обработка отмены
    if text == '/cancel':
        waiting_for_private = waiting_for_public = waiting_for_text = waiting_for_interval = waiting_for_remove = False
        waiting_for_auth_code = waiting_for_password = False
        await show_main_menu(update.message, "❌ Действие отменено\n\nВыберите действие:")
        return
    
    # ===== ОБРАБОТКА КОДА ПОДТВЕРЖДЕНИЯ =====
    if waiting_for_auth_code:
        # Проверяем, что это похоже на код (4-6 цифр)
        if text.isdigit() and 4 <= len(text) <= 6:
            status = await update.message.reply_text("🔄 Проверяю код...")
            success = await complete_auth_with_code(text, context.bot)
            if success:
                await status.delete()
                await show_main_menu(update.message, "✅ **Вход выполнен!**\n\nВыберите действие:")
            else:
                # Ошибка, но ожидание кода продолжается
                await status.edit_text("❌ Неверный код. Попробуйте еще раз или /cancel")
        else:
            await update.message.reply_text("❌ Код должен состоять из 4-6 цифр. Попробуйте еще раз или /cancel")
        return
    
    # ===== ОБРАБОТКА ПАРОЛЯ 2FA =====
    if waiting_for_password:
        status = await update.message.reply_text("🔄 Проверяю пароль...")
        success = await complete_auth_with_password(text, context.bot)
        if success:
            await status.delete()
            await show_main_menu(update.message, "✅ **Вход выполнен!**\n\nВыберите действие:")
        else:
            # Ошибка, но ожидание пароля продолжается
            await status.edit_text("❌ Неверный пароль. Попробуйте еще раз или /cancel")
        return
    
    # ===== РЕЖИМ УДАЛЕНИЯ КАНАЛА =====
    if waiting_for_remove:
        removed = False
        removed_name = ""
        
        for ch in CHANNELS[:]:
            if ch in text or f"@{ch}" in text:
                CHANNELS.remove(ch)
                removed = True
                removed_name = f"@{ch}"
                keys_to_delete = [k for k in last_posts.keys() if f"public_{ch}" in k]
                for k in keys_to_delete:
                    del last_posts[k]
                break
        
        if not removed:
            for ch_id in list(PRIVATE_CHANNELS.keys()):
                if ch_id in text:
                    del PRIVATE_CHANNELS[ch_id]
                    if ch_id in joined_private_channels:
                        joined_private_channels.remove(ch_id)
                    removed = True
                    removed_name = ch_id
                    keys_to_delete = [k for k in last_posts.keys() if f"private_{ch_id}" in k]
                    for k in keys_to_delete:
                        del last_posts[k]
                    break
        
        save_data()
        waiting_for_remove = False
        
        if removed:
            await update.message.reply_text(f"✅ Канал {removed_name} удален")
        else:
            await update.message.reply_text("❌ Канал не найден")
        
        await asyncio.sleep(1)
        await show_main_menu(update.message, "✅ **Готово!**\n\nВыберите действие:")
        return
    
    # ===== РЕЖИМ ДОБАВЛЕНИЯ ПУБЛИЧНОГО КАНАЛА =====
    if waiting_for_public:
        username = extract_channel_username(text)
        
        if not username:
            await update.message.reply_text(
                "❌ Не удалось распознать канал\n\n"
                "Отправьте:\n• durov\n• @durov\n• https://t.me/durov"
            )
            return
        
        if username in CHANNELS:
            await update.message.reply_text(f"❌ Канал @{username} уже в списке")
            waiting_for_public = False
            await show_main_menu(update.message, "❌ **Канал уже существует**\n\nВыберите действие:")
            return
        
        status = await update.message.reply_text(f"🔄 Проверяю канал @{username}...")
        
        try:
            client = await init_user_client(context.bot)
            if not client:
                await status.edit_text("❌ Ошибка подключения к аккаунту")
                waiting_for_public = False
                return
            
            entity = await client.get_entity(username)
            title = getattr(entity, 'title', username)
            
            CHANNELS.append(username)
            save_data()
            
            await status.edit_text(
                f"✅ **Публичный канал добавлен!**\n\n"
                f"📢 Название: {title}\n"
                f"🔗 Username: @{username}",
                parse_mode='Markdown'
            )
            
        except Exception as e:
            await status.edit_text(f"❌ Ошибка: {str(e)[:100]}")
            waiting_for_public = False
            await show_main_menu(update.message, f"❌ **Ошибка:** {str(e)[:50]}\n\nВыберите действие:")
            return
        
        waiting_for_public = False
        await asyncio.sleep(1)
        await show_main_menu(update.message, f"✅ **Канал @{username} добавлен!**\n\nВыберите действие:")
        return
    
    # ===== РЕЖИМ ДОБАВЛЕНИЯ ПРИВАТНОГО КАНАЛА =====
    if waiting_for_private:
        if not is_private_invite_link(text):
            await update.message.reply_text(
                "❌ Это не похоже на ссылку-приглашение\n\n"
                "Нужно: https://t.me/+COBtMLnnTos5YmEy\n"
                "Или /cancel"
            )
            return
        
        status = await update.message.reply_text("🔄 Обрабатываю ссылку...")
        
        try:
            client = await init_user_client(context.bot)
            if not client:
                await status.edit_text("❌ Ошибка подключения к аккаунту")
                waiting_for_private = False
                return
            
            result, title = await join_private_channel(client, text)
            
            if result:
                PRIVATE_CHANNELS[result] = text
                joined_private_channels.add(result)
                save_data()
                await status.edit_text(
                    f"✅ **Приватный канал добавлен!**\n\n"
                    f"📢 Название: {title}\n"
                    f"🔐 ID: `{result}`",
                    parse_mode='Markdown'
                )
            else:
                await status.edit_text(f"❌ {title}")
                waiting_for_private = False
                await show_main_menu(update.message, f"❌ **Ошибка:** {title}\n\nВыберите действие:")
                return
                
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
            waiting_for_private = False
            await show_main_menu(update.message, f"❌ **Ошибка:** {str(e)[:50]}\n\nВыберите действие:")
            return
        
        waiting_for_private = False
        await asyncio.sleep(1)
        await show_main_menu(update.message, f"✅ **Приватный канал добавлен!**\n\nВыберите действие:")
        return
    
    # ===== РЕЖИМ ИЗМЕНЕНИЯ ТЕКСТА =====
    if waiting_for_text:
        if len(text) > 200:
            await update.message.reply_text("❌ Слишком длинный (макс. 200 символов)")
            return
        COMMENT_TEXT = text
        save_data()
        waiting_for_text = False
        await update.message.reply_text(f"✅ Текст изменен: '{COMMENT_TEXT}'")
        await asyncio.sleep(1)
        await show_main_menu(update.message, f"✅ **Текст изменен!**\n\nНовый текст: '{COMMENT_TEXT}'\n\nВыберите действие:")
        return
    
    # ===== РЕЖИМ ИЗМЕНЕНИЯ ИНТЕРВАЛА =====
    if waiting_for_interval:
        try:
            interval = int(text)
            if interval < 10:
                await update.message.reply_text("❌ Минимум 10 секунд")
                return
            if interval > 3600:
                await update.message.reply_text("❌ Максимум 3600 секунд")
                return
            CHECK_INTERVAL = interval
            save_data()
            waiting_for_interval = False
            await update.message.reply_text(f"✅ Интервал изменен на {CHECK_INTERVAL} сек")
            await asyncio.sleep(1)
            await show_main_menu(update.message, f"✅ **Интервал изменен!**\n\nНовый интервал: {CHECK_INTERVAL} сек\n\nВыберите действие:")
        except ValueError:
            await update.message.reply_text("❌ Введите число")
        return
    
    # Если не в режиме - показываем меню
    await show_main_menu(update.message)

# ========== ЗАПУСК МОНИТОРИНГА ==========
async def monitor_channels(client, bot):
    global is_bot_running, last_posts
    
    while is_bot_running:
        try:
            # Мониторинг публичных каналов
            channels_to_check = CHANNELS.copy()
            for channel in channels_to_check:
                if not is_bot_running:
                    break
                try:
                    channel_entity = await client.get_entity(channel)
                    messages = await client.get_messages(channel_entity, limit=1)
                    if messages:
                        post_id = str(messages[0].id)
                        key = f"public_{channel}"
                        
                        if key not in last_posts:
                            last_posts[key] = post_id
                            save_data()
                        elif last_posts[key] != post_id:
                            logger.info(f"🎯 Новый пост в @{channel}")
                            success = await leave_comment(client, channel, post_id)
                            if success:
                                last_posts[key] = post_id
                                save_data()
                                await bot.send_message(
                                    chat_id=ADMIN_CHAT_ID,
                                    text=f"💬 **Прокомментировано!**\n📢 Канал: @{channel}",
                                    parse_mode='Markdown'
                                )
                except FloodWaitError as e:
                    logger.warning(f"Flood wait: {e.seconds} сек")
                    await asyncio.sleep(e.seconds)
                except Exception as e:
                    logger.error(f"Ошибка {channel}: {e}")
            
            # Мониторинг приватных каналов
            private_to_check = list(PRIVATE_CHANNELS.keys())
            for channel_id in private_to_check:
                if not is_bot_running:
                    break
                try:
                    if channel_id not in joined_private_channels:
                        continue
                    
                    numeric_id = int(channel_id.replace('private_', ''))
                    channel_entity = await client.get_entity(numeric_id)
                    messages = await client.get_messages(channel_entity, limit=1)
                    
                    if messages:
                        post_id = str(messages[0].id)
                        key = f"private_{channel_id}"
                        
                        if key not in last_posts:
                            last_posts[key] = post_id
                            save_data()
                        elif last_posts[key] != post_id:
                            logger.info(f"🎯 Новый пост в приватном канале")
                            success = await leave_comment(client, channel_id, post_id)
                            if success:
                                last_posts[key] = post_id
                                save_data()
                                await bot.send_message(
                                    chat_id=ADMIN_CHAT_ID,
                                    text=f"💬 **Прокомментировано в приватном канале!**",
                                    parse_mode='Markdown'
                                )
                except FloodWaitError as e:
                    logger.warning(f"Flood wait: {e.seconds} сек")
                    await asyncio.sleep(e.seconds)
                except Exception as e:
                    logger.error(f"Ошибка приватного: {e}")
            
            if is_bot_running:
                logger.info(f"💤 Ожидание {CHECK_INTERVAL} сек...")
                for _ in range(CHECK_INTERVAL):
                    if not is_bot_running:
                        break
                    await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"Ошибка в мониторинге: {e}")
            await asyncio.sleep(60)

async def run_comment_bot(bot):
    global user_client, is_bot_running
    try:
        client = await init_user_client(bot)
        if client:
            total = len(CHANNELS) + len(PRIVATE_CHANNELS)
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"🚀 **Мониторинг запущен!**\n\nОтслеживается каналов: {total}",
                parse_mode='Markdown'
            )
            await monitor_channels(client, bot)
    except Exception as e:
        logger.error(f"Ошибка: {e}")
    finally:
        if is_bot_running:
            is_bot_running = False
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text="⏹ **Мониторинг остановлен**",
                parse_mode='Markdown'
            )

# ========== ГЛАВНАЯ ФУНКЦИЯ ==========
async def main():
    load_data()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", handle_message))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    try:
        # При запуске пробуем подключиться к аккаунту
        await app.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="🤖 **Бот запущен!**\n\n"
                 "🔐 Для работы требуется подключить аккаунт комментатора.\n"
                 "Нажмите /start и выберите 'Подключить аккаунт'",
            parse_mode='Markdown'
        )
    except:
        pass
    
    logger.info("✅ Бот запущен")
    
    try:
        while True:
            await asyncio.sleep(300)
            save_data()
    except asyncio.CancelledError:
        pass
    finally:
        global is_bot_running
        is_bot_running = False
        save_data()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        if user_client:
            await user_client.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен")
