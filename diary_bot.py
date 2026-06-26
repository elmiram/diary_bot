import logging
import os
from calendar import month_abbr
from datetime import datetime, date, time, timedelta
from itertools import groupby
import threading
import time as thread_time # Renamed to avoid conflict with datetime.time
import re
import pytz

import requests
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    PicklePersistence,
)

from passwords import NOTION_API_KEY, NOTION_DATABASE_ID, TELEGRAM_BOT_TOKEN, YOUR_CHAT_ID

# --- Configuration ---
TIMEZONE = pytz.timezone('Europe/Zurich')

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Set httpx logger to WARNING to silence the INFO-level "getUpdates" logs
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# State definitions for the conversation
(
    ASKING_UPDATE,
    MEMORABLE,
    GRATEFUL,
    WORRIES,
    TODOS,
    PHOTO,
    UPDATING_MENU,
    UPDATING_MEMORABLE,
    UPDATING_GRATEFUL,
    UPDATING_WORRIES,
    UPDATING_TODOS,
    ASKING_EMOJI,
    ASKING_CHECKBOXES,
    ASKING_SCORE,
    ASKING_DATE,
) = range(15)

# --- Notion API Functions ---

def get_today_iso():
    """Returns today's date in YYYY-MM-DD format, respecting the configured timezone."""
    return datetime.now(TIMEZONE).date().isoformat()

def notion_api_request(method, url, **kwargs):
    """Helper function for making Notion API requests."""
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    try:
        response = requests.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Notion API error on {method} {url}: {e}")
        if e.response:
            logger.error(f"Notion API response: {e.response.text}")
        return None

def upload_image_to_notion(image_bytes: bytes, filename: str) -> str | None:
    """Uploads image bytes to Notion file storage and returns the file_upload_id."""
    auth_headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2026-03-11",
    }
    try:
        # Step 1: create upload session
        create_resp = requests.post(
            "https://api.notion.com/v1/file_uploads",
            headers={**auth_headers, "Content-Type": "application/json"},
            json={"filename": filename, "content_type": "image/jpeg"},
        )
        create_resp.raise_for_status()
        upload_data = create_resp.json()
        file_id = upload_data["id"]
        upload_url = upload_data["upload_url"]

        # Step 2: send the bytes
        send_resp = requests.post(
            upload_url,
            headers=auth_headers,
            files={"file": (filename, image_bytes, "image/jpeg")},
        )
        send_resp.raise_for_status()
        logger.info(f"Uploaded image to Notion: {file_id}")
        return file_id
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to upload image to Notion: {e}")
        return None

def build_notion_page_content(user_data):
    """Builds the list of blocks for a Notion page from user data."""
    children = []
    if user_data.get("photos"):
        for photo_item in user_data["photos"]:
            if isinstance(photo_item, dict) and photo_item.get("type") == "file_upload":
                children.append({"object": "block", "type": "image", "image": {"type": "file_upload", "file_upload": {"id": photo_item["id"]}}})
            else:
                # Fallback for any legacy external URLs
                children.append({"object": "block", "type": "image", "image": {"type": "external", "external": {"url": photo_item}}})
    if user_data.get("memorable"):
        children.extend([
            {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "How was the day?"}}]}},
            {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": user_data["memorable"]}}]}},
        ])
    if user_data.get("worries"):
        children.extend([
            {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Worries"}}]}},
            {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": user_data["worries"]}}]}},
        ])
    if user_data.get("grateful"):
        children.extend([
            {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Grateful for"}}]}},
            {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": user_data["grateful"]}}]}},
        ])
    if user_data.get("todos"):
        children.extend([
            {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Todos and ideas"}}]}},
            {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": user_data["todos"]}}]}},
        ])
    return children

def create_notion_page(user_data, context: ContextTypes.DEFAULT_TYPE):
    """Creates a new page in the Notion database."""
    entry_date = user_data.get("entry_date") or datetime.now(TIMEZONE).date()
    title = entry_date.strftime("%a %d %b %Y") # e.g., "Sat 25 Jul 2025"

    properties = {
        "Name": {"title": [{"text": {"content": title}}]},
        "Tags": {"multi_select": [{"name": "Daily"}]},
        "Date": {"date": {"start": entry_date.isoformat()}},
        "S": {"checkbox": user_data.get("checkbox_s", False)},
        "Sleep separate": {"checkbox": user_data.get("checkbox_sleep_separate", False)},
        "Tears": {"checkbox": user_data.get("checkbox_tears", False)},
        "Photos": {"checkbox": bool(user_data.get("photos"))},
    }
    if user_data.get("score") is not None:
        properties["Score"] = {"number": user_data["score"]}
    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": properties,
        "children": build_notion_page_content(user_data)
    }
    icon = user_data.get("icon")
    if icon:
        payload["icon"] = {"type": "emoji", "emoji": icon}

    response_data = notion_api_request("post", "https://api.notion.com/v1/pages", json=payload)
    if response_data:
        logger.info("Successfully created Notion page.")
        page_id = response_data["id"]
        if "diary_entries" not in context.bot_data:
            context.bot_data["diary_entries"] = {}
        context.bot_data["diary_entries"][entry_date.isoformat()] = {'page_id': page_id, 'icon': icon}

def update_notion_page_properties(page_id, user_data):
    """Updates the properties (icon, checkboxes, tags) of an existing Notion page."""
    properties = {
        "S": {"checkbox": user_data.get("checkbox_s", False)},
        "Sleep separate": {"checkbox": user_data.get("checkbox_sleep_separate", False)},
        "Tears": {"checkbox": user_data.get("checkbox_tears", False)},
    }
    payload = {"properties": properties}
    if user_data.get("icon"):
        payload["icon"] = {"type": "emoji", "emoji": user_data["icon"]}
    
    notion_api_request("patch", f"https://api.notion.com/v1/pages/{page_id}", json=payload)

def append_to_notion_page(page_id, blocks_to_append):
    """Appends new blocks to an existing Notion page."""
    payload = {"children": blocks_to_append}
    notion_api_request("patch", f"https://api.notion.com/v1/blocks/{page_id}/children", json=payload)

def is_valid_emoji(s):
    """Checks if a string is a single emoji."""
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F700-\U0001F77F"  # alchemical symbols
        "\U0001F780-\U0001F7FF"  # Geometric Shapes Extended
        "\U0001F800-\U0001F8FF"  # Supplemental Arrows-C
        "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
        "\U0001FA00-\U0001FA6F"  # Chess Symbols
        "\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
        "\U00002702-\U000027B0"  # Dingbats
        "\U000024C2-\U0001F251" 
        "]+",
        flags=re.UNICODE,
    )
    return len(s) == 1 and emoji_pattern.match(s)

# --- Telegram Conversation Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation after a /start command."""
    user = update.message.from_user
    logger.info(f"Command /start received from user {user.id} ({user.first_name})")
    
    context.user_data.clear()
    today = get_today_iso()
    
    if context.bot_data.get("diary_entries", {}).get(today):
        reply_keyboard = [["Yes, update it"], ["No, cancel"]]
        await update.message.reply_text(
            "You've already made an entry for today. Would you like to update it?",
            reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True),
        )
        return ASKING_UPDATE
    else:
        await update.message.reply_text("Hi! Let's get started with today's entry.", reply_markup=ReplyKeyboardRemove())
        return await ask_photos(update.message, context)

async def start_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Shows the menu for what to update."""
    context.user_data["is_update"] = True
    keyboard = [
        [InlineKeyboardButton("📝 Edit 'How was the day?'", callback_data="update_memorable")],
        [InlineKeyboardButton("😟 Edit Worries", callback_data="update_worries")],
        [InlineKeyboardButton("🙏 Edit 'Grateful for'", callback_data="update_grateful")],
        [InlineKeyboardButton("💡 Edit Todos and ideas", callback_data="update_todos")],
        [InlineKeyboardButton("🖼️ Add Pics", callback_data="update_photos")],
        [InlineKeyboardButton("☑️ Edit Checkboxes", callback_data="update_checkboxes")],
        [InlineKeyboardButton("🙂 Edit Emoji", callback_data="update_emoji")],
        [InlineKeyboardButton("✅ Finish Updating", callback_data="finish_updating")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if hasattr(update, "message") and update.message:
        message = update.message
    else:
        message = update

    await message.reply_text("What would you like to update?", reply_markup=reply_markup)
    return UPDATING_MENU

async def cancel_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Okay, I won't change anything.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# --- Backfill Flow ---
def get_date_picker_keyboard():
    """Generates an inline keyboard with the last 14 days for backfill date selection."""
    today = datetime.now(TIMEZONE).date()
    keyboard = []
    for i in range(1, 15):
        d = today - timedelta(days=i)
        keyboard.append([InlineKeyboardButton(d.strftime("%a %d %b %Y"), callback_data=f"date_{d.isoformat()}")])
    return InlineKeyboardMarkup(keyboard)

async def backfill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point for the backfill command. Asks the user to pick a date."""
    context.user_data.clear()
    context.user_data["is_backfill"] = True
    await update.message.reply_text(
        "Which date would you like to add an entry for?\n\n"
        "Pick from the list or type a date (YYYY-MM-DD or DD/MM/YYYY):",
        reply_markup=get_date_picker_keyboard(),
    )
    return ASKING_DATE

async def backfill_date_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles date selection via inline keyboard in backfill flow."""
    query = update.callback_query
    await query.answer()
    iso_date = query.data.split("_", 1)[1]
    entry_date = date.fromisoformat(iso_date)
    context.user_data["entry_date"] = entry_date
    await query.edit_message_text(f"Got it, creating entry for {entry_date.strftime('%a %d %b %Y')}.")
    return await ask_photos(query.message, context)

def parse_date_input(text: str):
    """Parses a date string in YYYY-MM-DD or DD/MM/YYYY or DD.MM.YYYY format."""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None

async def backfill_date_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles date input as free text in backfill flow."""
    entry_date = parse_date_input(update.message.text)
    if not entry_date:
        await update.message.reply_text(
            "Couldn't parse that date. Please use YYYY-MM-DD or DD/MM/YYYY (e.g. 2026-06-20 or 20/06/2026).",
            reply_markup=get_date_picker_keyboard(),
        )
        return ASKING_DATE
    if entry_date > datetime.now(TIMEZONE).date():
        await update.message.reply_text("That date is in the future. Please pick a past date.")
        return ASKING_DATE
    context.user_data["entry_date"] = entry_date
    await update.message.reply_text(f"Got it, creating entry for {entry_date.strftime('%a %d %b %Y')}.")
    return await ask_photos(update.message, context)

async def memorable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["memorable"] = update.message.text
    await update.message.reply_text("Got it. Anything you're worried about? You can type 'none' if not.")
    return WORRIES

async def worries(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores worries and proceeds to ask what the user is grateful for."""
    text = update.message.text
    if text.lower() not in ['none', 'no', 'nope']:
        context.user_data["worries"] = text
    await update.message.reply_text("What are you grateful for today?")
    return GRATEFUL

async def grateful(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["grateful"] = update.message.text
    await update.message.reply_text("Any todos or ideas to note down? You can type 'none' if not.")
    return TODOS

async def todos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text.lower() not in ['none', 'no', 'nope']:
        context.user_data["todos"] = text
    return await ask_checkboxes(update, context)

# --- Checkbox Handling ---
def get_checkbox_keyboard(user_data):
    """Generates the inline keyboard for checkboxes based on current state."""
    s_emoji = "✅" if user_data.get("checkbox_s") else "⬜️"
    sleep_emoji = "✅" if user_data.get("checkbox_sleep_separate") else "⬜️"
    tears_emoji = "✅" if user_data.get("checkbox_tears") else "⬜️"
    keyboard = [
        [InlineKeyboardButton(f"{s_emoji} S", callback_data="toggle_s")],
        [InlineKeyboardButton(f"{sleep_emoji} Sleep separate", callback_data="toggle_sleep")],
        [InlineKeyboardButton(f"{tears_emoji} Tears", callback_data="toggle_tears")],
        [InlineKeyboardButton("Continue ➡️", callback_data="done_checkboxes")],
    ]
    return InlineKeyboardMarkup(keyboard)

async def ask_checkboxes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Displays the checkbox options."""
    for key in ["checkbox_s", "checkbox_sleep_separate", "checkbox_tears"]:
        if key not in context.user_data:
            context.user_data[key] = False
            
    reply_markup = get_checkbox_keyboard(context.user_data)
    if update.callback_query:
        await update.callback_query.message.edit_text("Set your options for today:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("Set your options for today:", reply_markup=reply_markup)
    return ASKING_CHECKBOXES

async def toggle_checkbox(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Toggles a checkbox state and updates the keyboard."""
    query = update.callback_query
    await query.answer()
    
    toggle_map = {
        "toggle_s": "checkbox_s",
        "toggle_sleep": "checkbox_sleep_separate",
        "toggle_tears": "checkbox_tears",
    }
    key_to_toggle = toggle_map.get(query.data)
    
    if key_to_toggle:
        context.user_data[key_to_toggle] = not context.user_data.get(key_to_toggle, False)
    
    reply_markup = get_checkbox_keyboard(context.user_data)
    await query.edit_message_reply_markup(reply_markup)
    return ASKING_CHECKBOXES

async def done_checkboxes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Moves from checkboxes to the next step (emoji)."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Checkboxes saved!")
    
    if context.user_data.get("is_update"):
        today = get_today_iso()
        page_id = context.bot_data.get("diary_entries", {}).get(today, {}).get('page_id')
        if page_id:
            update_notion_page_properties(page_id, context.user_data)
        return await start_update(query.message, context)
    else:
        return await ask_emoji(query.message, context)

# --- Emoji Handling ---
async def ask_emoji(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Asks the user for an optional emoji icon."""
    reply_keyboard = [["Skip"]]
    await message.reply_text(
        "Would you like to add an emoji icon for today's entry? If so, send one now. Otherwise, press Skip.",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True),
    )
    return ASKING_EMOJI

async def emoji(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the emoji input."""
    user_emoji = update.message.text
    if is_valid_emoji(user_emoji):
        context.user_data["icon"] = user_emoji
        await update.message.reply_text(f"Icon set to {user_emoji}!", reply_markup=ReplyKeyboardRemove())
    else:
        await update.message.reply_text("That doesn't look like a single emoji. Let's skip it for now.", reply_markup=ReplyKeyboardRemove())

    if context.user_data.get("is_update"):
        today = get_today_iso()
        entry_data = context.bot_data.get("diary_entries", {}).get(today)
        if entry_data and entry_data.get('page_id'):
            page_id = entry_data['page_id']
            update_notion_page_properties(page_id, context.user_data)
            # Update the icon in our persistent data
            entry_data['icon'] = context.user_data.get("icon")
        return await start_update(update.message, context)
    else:
        return await ask_score(update.message, context)

async def skip_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Skips the emoji step."""
    await update.message.reply_text("No problem, skipping the icon.", reply_markup=ReplyKeyboardRemove())
    if context.user_data.get("is_update"):
        return await start_update(update.message, context)
    else:
        return await ask_score(update.message, context)

# --- Photo Handling ---
async def ask_photos(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Asks the user for photos."""
    if "photos" not in context.user_data:
        context.user_data["photos"] = []
    reply_keyboard = [["Done"]]
    await message.reply_text(
        "You can now send photos for today. Select multiple from your gallery or send them one by one. Press 'Done' when you're finished.",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True),
    )
    return PHOTO

async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    photo_file = await update.message.photo[-1].get_file()
    # Send a single status message on the first photo; subsequent photos upload silently
    if not context.user_data.get("photo_status_msg"):
        status_msg = await update.message.reply_text("Uploading photos...")
        context.user_data["photo_status_msg"] = status_msg

    image_bytes = await photo_file.download_as_bytearray()
    filename = f"photo_{len(context.user_data['photos']) + 1}.jpg"
    file_id = upload_image_to_notion(bytes(image_bytes), filename)
    if file_id:
        context.user_data["photos"].append({"type": "file_upload", "id": file_id})
    else:
        context.user_data.setdefault("photo_failures", 0)
        context.user_data["photo_failures"] += 1
    return PHOTO

async def _finish_photo_upload(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Edits the upload status message with the final photo count and clears it from state."""
    status_msg = context.user_data.pop("photo_status_msg", None)
    failures = context.user_data.pop("photo_failures", 0)
    count = len(context.user_data.get("photos", []))
    if status_msg:
        if count == 0:
            text = "No photos were saved."
        elif count == 1:
            text = "1 photo saved!"
        else:
            text = f"{count} photos saved!"
        if failures:
            text += f" ({failures} failed to upload.)"
        await status_msg.edit_text(text)

async def done_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the 'Done' button in the photo step. For new entries, proceeds to the day questions."""
    if context.user_data.get("is_update"):
        today = get_today_iso()
        page_id = context.bot_data.get("diary_entries", {}).get(today, {}).get('page_id')
        if page_id and context.user_data.get("photos"):
            new_photo_blocks = []
            for photo_item in context.user_data["photos"]:
                if isinstance(photo_item, dict) and photo_item.get("type") == "file_upload":
                    new_photo_blocks.append({"object": "block", "type": "image", "image": {"type": "file_upload", "file_upload": {"id": photo_item["id"]}}})
                else:
                    new_photo_blocks.append({"object": "block", "type": "image", "image": {"type": "external", "external": {"url": photo_item}}})
            append_to_notion_page(page_id, new_photo_blocks)
            # Automatically check the "Photos" box since photos were added
            update_payload = {"properties": {"Photos": {"checkbox": True}}}
            notion_api_request("patch", f"https://api.notion.com/v1/pages/{page_id}", json=update_payload)
        await _finish_photo_upload(update.message, context)
        return await start_update(update.message, context)
    else:
        await _finish_photo_upload(update.message, context)
        await update.message.reply_text("How was the day? You can write down anything you want here.", reply_markup=ReplyKeyboardRemove())
        return MEMORABLE

async def ask_score(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Asks the user to rate their day."""
    await message.reply_text("Last step! How would you rate today? (1–10, decimals like 7.5 are fine)")
    return ASKING_SCORE

async def score_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles score input as free text."""
    text = update.message.text.strip().replace(",", ".")
    try:
        value = float(text)
        if not (1 <= value <= 10):
            raise ValueError
        context.user_data["score"] = value
        await update.message.reply_text(f"Score set to {value}!", reply_markup=ReplyKeyboardRemove())
    except ValueError:
        await update.message.reply_text("Please enter a number between 1 and 10 (e.g. 7 or 8.5).")
        return ASKING_SCORE
    return await save_entry(update.message, context)

async def save_entry(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Creates the Notion page and ends the conversation."""
    create_notion_page(context.user_data, context)
    reply_markup = ReplyKeyboardMarkup([["/start"]], resize_keyboard=True, one_time_keyboard=True)
    await message.reply_text("I've saved your new diary entry to Notion. Talk to you tomorrow!", reply_markup=reply_markup)
    return ConversationHandler.END

# --- Update Flow Handlers ---
async def updating_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles button presses from the update menu."""
    query = update.callback_query
    await query.answer()
    
    actions = {
        "update_memorable": ("Okay, send me the new text to add for the 'How was the day?' section.", UPDATING_MEMORABLE),
        "update_worries": ("Okay, what worries would you like to add?", UPDATING_WORRIES),
        "update_grateful": ("Got it. What new things are you grateful for today?", UPDATING_GRATEFUL),
        "update_todos": ("What todos or ideas would you like to add?", UPDATING_TODOS),
        "update_photos": (None, PHOTO),
        "update_checkboxes": (None, ASKING_CHECKBOXES),
        "update_emoji": (None, ASKING_EMOJI),
    }

    action = query.data
    if action == "finish_updating":
        await query.edit_message_text("All done. Your entry has been updated!")
        return ConversationHandler.END
    
    if action in actions:
        message, state = actions[action]
        if message:
            await query.message.reply_text(message, reply_markup=ReplyKeyboardRemove())
        elif state == PHOTO:
            return await ask_photos(query.message, context)
        elif state == ASKING_CHECKBOXES:
            return await ask_checkboxes(update, context)
        elif state == ASKING_EMOJI:
            return await ask_emoji(query.message, context)
        return state

    return UPDATING_MENU

async def update_text_field(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str) -> int:
    """Appends new text to the correct section in a Notion page."""
    today = get_today_iso()
    page_id = context.bot_data.get("diary_entries", {}).get(today, {}).get('page_id')
    new_text = update.message.text

    if not page_id:
        await update.message.reply_text("Error: Could not find the page to update.")
        return await start_update(update.message, context)

    # 1. Fetch all blocks to find the one to update
    blocks_url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
    all_blocks_data = notion_api_request("get", blocks_url)
    if not all_blocks_data:
        await update.message.reply_text("Could not retrieve the entry from Notion to update.")
        return await start_update(update.message, context)

    blocks = all_blocks_data.get("results", [])
    
    heading_map = {
        "memorable": "How was the day?",
        "worries": "Worries",
        "grateful": "Grateful for",
        "todos": "Todos and ideas",
    }
    target_heading_text = heading_map.get(field)
    
    target_block_id = None
    old_text = ""
    found_heading = False

    for block in blocks:
        if found_heading:
            # This is the block immediately after our target heading
            if block.get("type") == "paragraph":
                target_block_id = block.get("id")
                if block["paragraph"].get("rich_text"):
                    old_text = "".join([rt.get("plain_text", "") for rt in block["paragraph"]["rich_text"]])
            break  # We only care about the first paragraph after the heading
        
        if (block.get("type") == "heading_2" 
            and block["heading_2"].get("rich_text") 
            and block["heading_2"]["rich_text"][0].get("plain_text") == target_heading_text):
            found_heading = True
    
    if target_block_id:
        # 2. We found the paragraph block. Update it by combining texts.
        combined_text = old_text + "\n\n" + new_text
        update_payload = {
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": combined_text}}]
            }
        }
        notion_api_request("patch", f"https://api.notion.com/v1/blocks/{target_block_id}", json=update_payload)
        await update.message.reply_text(f"'{target_heading_text}' section updated!")
    else:
        # 3. Section/paragraph not found. Append it as a new section at the end of the page.
        blocks_to_append = [
            {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": target_heading_text}}]}},
            {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": new_text}}]}},
        ]
        append_to_notion_page(page_id, blocks_to_append)
        await update.message.reply_text(f"Couldn't find the original section, so I added a new '{target_heading_text}' section!")

    return await start_update(update.message, context)

async def update_memorable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await update_text_field(update, context, "memorable")

async def update_worries(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await update_text_field(update, context, "worries")

async def update_grateful(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await update_text_field(update, context, "grateful")

async def update_todos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await update_text_field(update, context, "todos")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Okay, cancelled.", reply_markup=ReplyKeyboardRemove())
    context.user_data.clear()
    return ConversationHandler.END

# --- Emoji Timeline Command ---
def sync_entries_from_notion(bot_data: dict) -> int:
    """Fetches all Daily entries from Notion and replaces local cache. Returns total count synced."""
    query_payload = {"filter": {"property": "Tags", "multi_select": {"contains": "Daily"}}}
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"

    synced = {}
    has_more = True
    next_cursor = None

    while has_more:
        payload = dict(query_payload)
        if next_cursor:
            payload["start_cursor"] = next_cursor

        response_data = notion_api_request("post", url, json=payload)
        if not response_data:
            logger.warning("Notion sync interrupted: API call failed.")
            break

        for page in response_data.get("results", []):
            try:
                page_id = page["id"]
                icon_data = page.get("icon")
                icon = icon_data.get("emoji") if icon_data else None
                # Prefer the explicit Date property; fall back to created_time for older entries
                date_prop = page.get("properties", {}).get("Date", {}).get("date")
                if date_prop and date_prop.get("start"):
                    iso_date = date_prop["start"]
                else:
                    created_time_str = page["created_time"]
                    iso_date = datetime.fromisoformat(created_time_str.replace("Z", "+00:00")).date().isoformat()
                synced[iso_date] = {"page_id": page_id, "icon": icon}
            except (KeyError, ValueError):
                continue

        has_more = response_data.get("has_more", False)
        next_cursor = response_data.get("next_cursor")

    if synced:
        bot_data["diary_entries"] = synced

    return len(synced)

def get_emojis_keyboard(r: str = "yr", c: str = "emo", g: str = "flat") -> InlineKeyboardMarkup:
    """Builds the emoji view config keyboard. r=range, c=content, g=grouping."""
    def btn(label, nr, nc, ng):
        active = (nr == r and nc == c and ng == g)
        return InlineKeyboardButton(("✅ " if active else "") + label, callback_data=f"emoj_{nr}_{nc}_{ng}_0")
    return InlineKeyboardMarkup([
        [btn("Calendar year", "yr", c, g),  btn("Rolling 12m", "roll", c, g)],
        [btn("Emojis only",  r, "emo", g),  btn("All days",    r, "all", g)],
        [btn("Flat", r, c, "flat"), btn("By month", r, c, "mon"), btn("By week", r, c, "wk")],
        [InlineKeyboardButton("▶️ Show", callback_data=f"emoj_{r}_{c}_{g}_1")],
    ])

def build_emoji_timeline(diary_entries: dict, r: str, c: str, g: str) -> str:
    """Generates the emoji timeline text from the local cache."""
    today = datetime.now(TIMEZONE).date()
    if r == "yr":
        start_date = date(today.year, 1, 1)
        title = str(today.year)
    else:
        start_date = today - timedelta(days=365)
        title = "Rolling 12 months"

    days = []
    d = start_date
    while d <= today:
        entry = diary_entries.get(d.isoformat())
        icon = entry.get("icon") if entry else None
        days.append((d, icon))
        d += timedelta(days=1)

    def sym(icon):
        return icon if icon else ("•" if c == "all" else None)

    if g == "flat":
        symbols = [s for _, icon in days for s in [sym(icon)] if s]
        if not symbols:
            return f"No entries found for {title.lower()}."
        return f"{title}:\n{''.join(symbols)}"

    elif g == "mon":
        span_years = start_date.year != today.year
        lines = []
        for (year, month), grp in groupby(days, key=lambda x: (x[0].year, x[0].month)):
            symbols = [s for _, icon in grp for s in [sym(icon)] if s]
            if not symbols:
                continue
            lbl = f"{month_abbr[month]} {year}" if span_years else month_abbr[month]
            lines.append(f"{lbl}: {''.join(symbols)}")
        return (f"{title}:\n" + "\n".join(lines)) if lines else f"No entries found for {title.lower()}."

    else:  # g == "wk"
        lines = []
        for monday, grp in groupby(days, key=lambda x: x[0] - timedelta(days=x[0].weekday())):
            symbols = [s for _, icon in grp for s in [sym(icon)] if s]
            if not symbols:
                continue
            lines.append(f"{monday.strftime('%b %d')}: {''.join(symbols)}")
        return (f"{title}:\n" + "\n".join(lines)) if lines else f"No entries found for {title.lower()}."

async def show_emojis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends the emoji view config keyboard."""
    await update.message.reply_text("Configure your emoji view:", reply_markup=get_emojis_keyboard())

async def emojis_option_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles button presses on the emoji config keyboard."""
    query = update.callback_query
    if query.from_user.id != int(YOUR_CHAT_ID):
        return
    await query.answer()
    _, r, c, g, show = query.data.split("_")
    if show == "1":
        text = build_emoji_timeline(context.bot_data.get("diary_entries", {}), r, c, g)
        await query.edit_message_text(text, reply_markup=get_emojis_keyboard(r, c, g))
    else:
        await query.edit_message_reply_markup(get_emojis_keyboard(r, c, g))


# --- Reminder and Scheduling Functions ---
async def stop_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.bot_data["reminders_enabled"] = False
    await update.message.reply_text("Got it, I'll stop sending daily reminders. Use /resumereminders to turn them back on.")

async def resume_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.bot_data["reminders_enabled"] = True
    await update.message.reply_text("Daily reminders are back on!")

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id, current_reminder = job.chat_id, job.data.get("reminder_count", 0)

    if not context.bot_data.get("reminders_enabled", True):
        logger.info("Skipping reminder as reminders are disabled.")
        return

    if context.bot_data.get("diary_entries", {}).get(get_today_iso()):
        logger.info("Skipping reminder as an entry for today already exists.")
        return

    logger.info(f"Sending reminder #{current_reminder + 1}.")
    reminder_messages = [
        "Just a gentle nudge! Don't forget your diary entry. ✨",
        "It's me again! Friendly reminder to capture today's moments.",
        "Final reminder for today's diary entry! It only takes a few minutes.",
    ]
    if current_reminder < len(reminder_messages):
        await context.bot.send_message(chat_id, text=reminder_messages[current_reminder])

    next_reminder_delays = [30 * 60, 90 * 60]
    if current_reminder < len(next_reminder_delays):
        context.job_queue.run_once(send_reminder, next_reminder_delays[current_reminder], chat_id=chat_id, name=f"reminder_{chat_id}", data={"reminder_count": current_reminder + 1})

async def daily_prompt(context: ContextTypes.DEFAULT_TYPE):
    """The job function for the daily prompt. Retries on network failure."""
    chat_id = context.job.chat_id
    retry_count = context.job.data.get("retry_count", 0) if context.job.data else 0

    if not context.bot_data.get("reminders_enabled", True):
        logger.info("Skipping daily prompt as reminders are disabled.")
        return

    if context.bot_data.get("diary_entries", {}).get(get_today_iso()):
        return

    try:
        reply_markup = ReplyKeyboardMarkup([["/start"]], resize_keyboard=True, one_time_keyboard=True)
        await context.bot.send_message(
            chat_id,
            text="👋 Good evening! Time for your daily diary entry.",
            reply_markup=reply_markup,
        )
        logger.info("Daily prompt sent successfully.")
        # Schedule the normal reminder chain only on successful send
        context.job_queue.run_once(send_reminder, 2 * 60 * 60, chat_id=chat_id, name=f"reminder_{chat_id}", data={"reminder_count": 0})
    except NetworkError as e:
        logger.warning(f"Network error sending daily prompt (Attempt {retry_count + 1}): {e}")
        if retry_count < 3:
            # Schedule a retry in 5 minutes
            context.job_queue.run_once(
                daily_prompt,
                when=5 * 60,
                chat_id=chat_id,
                name=f"daily_prompt_retry_{chat_id}",
                data={"retry_count": retry_count + 1},
            )
        else:
            logger.error("Failed to send daily prompt after 3 retries. Will try again tomorrow.")
    except Exception as e:
        logger.error(f"An unexpected error occurred in daily_prompt: {e}")

async def post_init_setup(application: Application) -> None:
    """Runs after the bot is initialized. Syncs from Notion and checks for missed prompts."""
    logger.info("Syncing entries from Notion on startup...")
    new_count = sync_entries_from_notion(application.bot_data)
    if new_count:
        await application.persistence.flush()
    logger.info(f"Notion sync complete. {new_count} new entries added to local cache.")

    # --- Check for missed daily prompt on startup ---
    today = get_today_iso()
    prompt_time = time(hour=20, minute=0, tzinfo=TIMEZONE)
    now = datetime.now(TIMEZONE).time()

    if now > prompt_time and not application.bot_data.get("diary_entries", {}).get(today):
        logger.info("Bot started after prompt time and no entry found for today. Sending prompt now.")
        application.job_queue.run_once(
            daily_prompt,
            when=0,
            chat_id=int(YOUR_CHAT_ID),
            name=f"missed_prompt_startup_{YOUR_CHAT_ID}"
        )


def main() -> None:
    logger.info("Bot started. Polling Telegram for updates every 30 seconds.")
    persistence = PicklePersistence(filepath="diary_bot_persistence")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).persistence(persistence).post_init(post_init_setup).build()

    # --- User filter to ensure only you can use the bot ---
    user_filter = filters.User(user_id=int(YOUR_CHAT_ID))

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start, filters=user_filter),
            CommandHandler("backfill", backfill, filters=user_filter),
        ],
        states={
            ASKING_DATE: [CallbackQueryHandler(backfill_date_button, pattern="^date_"), MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, backfill_date_text)],
            ASKING_UPDATE: [MessageHandler(filters.Regex("^Yes, update it$") & user_filter, start_update), MessageHandler(filters.Regex("^No, cancel$") & user_filter, cancel_update)],
            MEMORABLE: [MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, memorable)],
            WORRIES: [MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, worries)],
            GRATEFUL: [MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, grateful)],
            TODOS: [MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, todos)],
            PHOTO: [MessageHandler(filters.PHOTO & user_filter, photo), MessageHandler(filters.Regex("^Done$") & user_filter, done_photo)],
            UPDATING_MENU: [CallbackQueryHandler(updating_menu_handler)], # CallbackQueryHandlers are already user-specific
            UPDATING_MEMORABLE: [MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, update_memorable)],
            UPDATING_WORRIES: [MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, update_worries)],
            UPDATING_GRATEFUL: [MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, update_grateful)],
            UPDATING_TODOS: [MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, update_todos)],
            ASKING_EMOJI: [MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, emoji), MessageHandler(filters.Regex("^Skip$") & user_filter, skip_emoji)],
            ASKING_CHECKBOXES: [CallbackQueryHandler(toggle_checkbox, pattern="^toggle_"), CallbackQueryHandler(done_checkboxes, pattern="^done_checkboxes$")],
            ASKING_SCORE: [MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, score_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel, filters=user_filter)],
        persistent=True,
        name="diary_conversation",
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("emojis", show_emojis, filters=user_filter))
    application.add_handler(CallbackQueryHandler(emojis_option_callback, pattern="^emoj_"))
    application.add_handler(CommandHandler("stopreminders", stop_reminders, filters=user_filter))
    application.add_handler(CommandHandler("resumereminders", resume_reminders, filters=user_filter))

    # Schedule the daily prompt using the built-in JobQueue
    job_queue = application.job_queue
    job_queue.run_daily(
        daily_prompt,
        time(hour=20, minute=0, tzinfo=TIMEZONE), # 8 PM in the specified timezone
        chat_id=int(YOUR_CHAT_ID),
        name=f"daily_prompt_{YOUR_CHAT_ID}"
    )
    
    application.run_polling(timeout=30)

if __name__ == "__main__":
    main()
