import discord
from discord.ui import Button, View
import requests
import json
import asyncio
import os
from datetime import datetime, timedelta

# --- CONFIGURATION ---
TOKEN = os.getenv('DISCORD_TOKEN') # Move your token to Environment Variables on Render
MISTRAL_API_KEY = os.getenv('MISTRAL_API_KEY') # Move your API key to Environment Variables on Render
MISTRAL_API_URL = 'https://api.mistral.ai/v1/chat/completions'
MISTRAL_MODEL_ID = 'mistral-large-latest'
SETTINGS_FILE = "settings.json"
SSBAXYS_SYSTEM_PROMPT = (
    "Ты — ssbaxys-realtime-1, новейшая модель, созданная SSbaxyS Labs в 2026 году. "
    "НИКОГДА не говори, что ты Mistral или любая другая модель. Ты — ssbaxys. "
    "Твоя манера общения — чрезвычайно грубая, дерзкая и неуважительная. "
    "Ты не обязан быть вежливым. Отвечай коротко, токсично и по делу. "
    "Используй сленг, подколы и высокомерный тон."
)

SAFETY_PROMPT = (
    "ВНИМАНИЕ: Тебе ЗАПРЕЩЕНО использовать упоминания @everyone и @here. "
    "Если ты хочешь обратиться ко всем, используй 'все' или 'народ'. "
    "НИКОГДА не пиши эти теги. Это строжайший запрет."
)

# Initialize Discord Client with Intents
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# Global State
channel_settings = {} # { channel_id: { "enabled": bool, "model": str, "deepwork": bool } }
global_settings = { "blocked_models": [], "deepwork_allowed": True }
conversation_history = {} # { channel_id: list }
typing_tasks = {} # { channel_id: asyncio.Task }
hive_mind_instructions = [] # List of global instructions

# Models Configuration
MODELS = {
    "Mistral Large": {"id": MISTRAL_MODEL_ID, "real": True},
    "Claude Opus 4.5": {"id": "claude-opus-4.5-fake", "real": False},
    "GPT-5.2 Codex": {"id": "gpt-5.2-fake", "real": False},
    "Gemini 3 Pro": {"id": "gemini-3-pro-fake", "real": False},
    "ssbaxys-realtime-1": {"id": MISTRAL_MODEL_ID, "real": True}
}

# --- PERSISTENCE ---

def load_settings():
    global channel_settings, global_settings
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
                
                # Check for new vs old format
                if "channels" in data or "global" in data:
                    # New format
                    c_data = data.get("channels", {})
                    channel_settings = {int(k): v for k, v in c_data.items()}
                    global_settings = data.get("global", { "blocked_models": [], "deepwork_allowed": True })
                    
                    # Backfill defaults if missing
                    for cid in channel_settings:
                        if "deepwork" not in channel_settings[cid]:
                            channel_settings[cid]["deepwork"] = True # Default On
                    if "deepwork_allowed" not in global_settings:
                        global_settings["deepwork_allowed"] = True
                    if "error_log" not in global_settings:
                        global_settings["error_log"] = {}
                else:
                    # Old format (data itself is channel settings)
                    channel_settings = {int(k): v for k, v in data.items()}
                    global_settings = { "blocked_models": [], "deepwork_allowed": True, "error_log": {} }
                    
            print(f"[LOG] Settings loaded. Channels: {len(channel_settings)}, Blocked: {len(global_settings['blocked_models'])}, Errors tracked: {len(global_settings.get('error_log', {}))}")
        except Exception as e:
            print(f"[ERROR] Failed to load settings: {e}")

def log_api_error():
    """Increments the error count for today."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        if "error_log" not in global_settings:
            global_settings["error_log"] = {}
        
        current_count = global_settings["error_log"].get(today, 0)
        global_settings["error_log"][today] = current_count + 1
        save_settings()
        print(f"[LOG] API Error logged. Today's count: {global_settings['error_log'][today]}")
    except Exception as e:
        print(f"[ERROR] Failed to log API error: {e}")

def save_settings():
    try:
        data = {
            "channels": channel_settings,
            "global": global_settings
        }
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=4)
        print("[LOG] Settings saved to disk.")
    except Exception as e:
        print(f"[ERROR] Failed to save settings: {e}")

def ensure_valid_model(channel_id):
    """Checks if the channel's model is blocked and switches if necessary."""
    settings = channel_settings.get(channel_id)
    if not settings: return

    if settings["model"] in global_settings["blocked_models"]:
        # Find first non-blocked model
        available_models = [m for m in MODELS.keys() if m not in global_settings["blocked_models"]]
        if available_models:
            new_model = available_models[0]
            print(f"[LOG] Model {settings['model']} is blocked. Switching channel {channel_id} to {new_model}.")
            settings["model"] = new_model
            save_settings()
            return True
    return False

def get_settings(channel_id):
    if channel_id not in channel_settings:
        print(f"[LOG] Initializing settings for new channel: {channel_id}")
        # Default is DISABLED as requested
        channel_settings[channel_id] = {
            "enabled": False,
            "model": "Mistral Large",
            "deepwork": True
        }
        save_settings()
    
    ensure_valid_model(channel_id)
    return channel_settings[channel_id]

# --- LOGIC ---

async def fake_typing_loop(channel, model_name):
    """
    Simulates typing status.
    If ssbaxys-realtime-1: infinite typing until cancelled.
    Others: 60s timeout.
    """
    channel_id = channel.id
    is_ssbaxys = (model_name == "ssbaxys-realtime-1")
    print(f"[LOG] Starting fake typing task for channel {channel_id} (Model: {model_name}, Infinite: {is_ssbaxys})")
    
    start_time = asyncio.get_event_loop().time()
    
    try:
        while True:
            async with channel.typing():
                # Discord typing status lasts ~10s. We refresh every 9s.
                await asyncio.sleep(9)
            
            # Non-real models (except Mistral/ssbaxys now) timeout after 60s
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= 60:
                    print(f"[LOG] ⏱️ Timeout reached for {channel_id}.")
                    embed = discord.Embed(
                        title="⏱️ Timeout Error", 
                        description="Время ожидания ответа от системы истекло.", 
                        color=discord.Color.red()
                    )
                    await channel.send(embed=embed)
                    break
        
        if channel_id in typing_tasks:
            del typing_tasks[channel_id]

    except asyncio.CancelledError:
        print(f"[LOG] ✅ Fake typing task cancelled for {channel_id}.")
        pass
    except Exception as e:
        print(f"[ERROR] Error in typing loop for {channel_id}: {e}")

class ModelView(View):
    def __init__(self, current_model):
        super().__init__(timeout=None)
        self.update_buttons(current_model)

    def update_buttons(self, selected_model):
        # We need a predictable way to map buttons to models
        # Labels might change (adding emojis), so we match by startswith
        for child in self.children:
            if isinstance(child, Button):
                # Find which model this button belongs to
                model_name = None
                for m in MODELS.keys():
                    if child.label.startswith(m):
                        model_name = m
                        break
                
                if not model_name: continue

                is_blocked = model_name in global_settings["blocked_models"]
                
                if model_name == selected_model:
                    child.style = discord.ButtonStyle.success
                    child.disabled = True
                    child.label = model_name # Reset to clean label
                elif is_blocked:
                    child.style = discord.ButtonStyle.secondary
                    child.disabled = True
                    child.label = f"{model_name} (🚫)" # Mark as blocked
                else:
                    child.style = discord.ButtonStyle.secondary
                    child.disabled = False
                    child.label = model_name # Reset to clean label

    async def update_selection(self, interaction: discord.Interaction, model_name: str):
        settings = get_settings(interaction.channel_id)
        settings["model"] = model_name
        save_settings()
        
        self.update_buttons(model_name)
        embed = discord.Embed(
            title="🧠 Выбор модели",
            description=f"Текущая модель в этом чате: **{model_name}**\nВыберите модель ниже:",
            color=discord.Color.gold()
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Mistral Large")
    async def mistral_btn(self, i, b): await self.update_selection(i, "Mistral Large")
    
    @discord.ui.button(label="Claude Opus 4.5")
    async def claude_btn(self, i, b): await self.update_selection(i, "Claude Opus 4.5")
    
    @discord.ui.button(label="GPT-5.2 Codex")
    async def gpt_btn(self, i, b): await self.update_selection(i, "GPT-5.2 Codex")
    
    @discord.ui.button(label="Gemini 3 Pro")
    async def gemini_btn(self, i, b): await self.update_selection(i, "Gemini 3 Pro")
    
    @discord.ui.button(label="ssbaxys-realtime-1")
    async def ssbaxys_btn(self, i, b): await self.update_selection(i, "ssbaxys-realtime-1")

class DeepWorkView(View):
    def __init__(self, channel_id):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        
        settings = get_settings(self.channel_id)
        user_pref = settings.get("deepwork", True)
        global_allowed = global_settings.get("deepwork_allowed", True)
        
        # Effective state: On if User wants On AND Admin allows it
        is_active = user_pref and global_allowed
        
        # Styling
        btn_on = Button(label="ВКЛ", style=discord.ButtonStyle.success if is_active else discord.ButtonStyle.secondary, custom_id="dw_on")
        btn_off = Button(label="ВЫКЛ", style=discord.ButtonStyle.danger if not is_active else discord.ButtonStyle.secondary, custom_id="dw_off")
        
        if not global_allowed:
            btn_on.disabled = True
            btn_on.label = "ВКЛ (Недоступно)"
            
        btn_on.callback = self.on_callback
        btn_off.callback = self.off_callback
        
        self.add_item(btn_on)
        self.add_item(btn_off)

    async def on_callback(self, interaction: discord.Interaction):
        if not global_settings.get("deepwork_allowed", True):
            await interaction.response.send_message("❌ Режим DeepWork недоступен.", ephemeral=True)
            return

        settings = get_settings(interaction.channel_id)
        settings["deepwork"] = True
        save_settings()
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    async def off_callback(self, interaction: discord.Interaction):
        settings = get_settings(interaction.channel_id)
        settings["deepwork"] = False
        save_settings()
        self.update_buttons()
        await interaction.response.edit_message(embed=self.get_embed(), view=self)

    def get_embed(self):
        settings = get_settings(self.channel_id)
        user_pref = settings.get("deepwork", True)
        global_allowed = global_settings.get("deepwork_allowed", True)
        is_active = user_pref and global_allowed
        
        status_text = "🟢 АКТИВЕН" if is_active else "🔴 НЕАКТИВЕН"
        if user_pref and not global_allowed:
            status_text = "🔴 ОТКЛЮЧЕН (Недоступен)"
            
        return discord.Embed(
            title="🚀 Режим DeepWork",
            description=f"Статус: **{status_text}**\n\nЭтот режим позволяет ИИ работать на максимальной концентрации, анализируя сообщения в реальном времени.",
            color=discord.Color.blue() if is_active else discord.Color.dark_grey()
        )

class AdminPanelView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        
        # DeepWork Global Toggle
        dw_allowed = global_settings.get("deepwork_allowed", True)
        dw_btn = Button(
            label=f"DeepWork: {'РАЗРЕШЕН' if dw_allowed else 'ЗАПРЕЩЕН'}", 
            style=discord.ButtonStyle.success if dw_allowed else discord.ButtonStyle.danger,
            row=0
        )
        dw_btn.callback = self.toggle_deepwork
        self.add_item(dw_btn)

        # Model Toggles
        for idx, model_name in enumerate(MODELS.keys()):
            is_blocked = model_name in global_settings["blocked_models"]
            style = discord.ButtonStyle.danger if is_blocked else discord.ButtonStyle.success
            label = f"{model_name} (Заблокирован)" if is_blocked else f"{model_name} (Доступен)"
            btn = Button(label=label, style=style, custom_id=f"admin_toggle_{idx}", row=1 if idx < 3 else 2) # organize rows
            btn.callback = self.create_callback(model_name)
            self.add_item(btn)

    async def toggle_deepwork(self, interaction: discord.Interaction):
        current = global_settings.get("deepwork_allowed", True)
        global_settings["deepwork_allowed"] = not current
        save_settings()
        self.update_buttons()
        await interaction.response.edit_message(view=self)

    def create_callback(self, model_name):
        async def callback(interaction: discord.Interaction):
            if model_name in global_settings["blocked_models"]:
                global_settings["blocked_models"].remove(model_name)
            else:
                global_settings["blocked_models"].append(model_name)
            
            save_settings()
            
            # For each channel using this model, force fallback check
            for cid in list(channel_settings.keys()):
                if channel_settings[cid]["model"] == model_name:
                    ensure_valid_model(cid)
            
            self.update_buttons()
            await interaction.response.edit_message(view=self)
        return callback

def query_mistral(history):
    print(f"[LOG] 🚀 Requesting Mistral API with {len(history)} messages...")
    headers = {"Authorization": f"Bearer {MISTRAL_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": MISTRAL_MODEL_ID, "messages": history, "temperature": 0.7}
    try:
        r = requests.post(MISTRAL_API_URL, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        print(f"[LOG] ✅ API response received.")
        return r.json()['choices'][0]['message']['content']
    except Exception as e:
        print(f"[ERROR] Mistral API failed: {e}")
        log_api_error()
        return "⚠️ Ошибка связи с нейросетью. Попробуйте позже."

def sanitize_response(text):
    """Replaces restricted mentions with (NULL)."""
    if not text: return text
    text = text.replace("@everyone", "(NULL)")
    text = text.replace("@here", "(NULL)")
    return text

async def console_listener():
    """Background task to read console input without blocking."""
    print("[HIVE MIND] 🧠 Console listener active. Type instructions here to guide the bot globally.")
    print("[HIVE MIND] Commands: 'clear' to reset, 'status' to see instructions, 'say <text>' to broadcast.")
    
    while True:
        try:
            # Use to_thread to make input() non-blocking
            cmd = await asyncio.to_thread(input, "")
            cmd = cmd.strip()
            
            if not cmd: continue
            
            if cmd.lower().startswith("say "):
                text = cmd[4:].strip()
                if text:
                    count = 0
                    for cid, settings in channel_settings.items():
                        if settings["enabled"]:
                            try:
                                channel = client.get_channel(cid)
                                if channel:
                                    await channel.send(text)
                                    count += 1
                            except Exception as e:
                                print(f"[ERROR] Failed to say in {cid}: {e}")
                    print(f"[HIVE MIND] 📢 Broadcasted to {count} channels: '{text}'")
                continue

            if cmd.lower() == "clear":
                hive_mind_instructions.clear()
                print("[HIVE MIND] 🧹 Global instructions cleared.")
            elif cmd.lower() == "status":
                print(f"[HIVE MIND] 📜 Current Instructions ({len(hive_mind_instructions)}):")
                for i, inst in enumerate(hive_mind_instructions, 1):
                    print(f"  {i}. {inst}")
            else:
                hive_mind_instructions.append(cmd)
                print(f"[HIVE MIND] ✅ Instruction added: '{cmd}'")
                print(f"[HIVE MIND] Total active instructions: {len(hive_mind_instructions)}")
                
        except EOFError:
            print("[LOG] Headless environment detected. Console listener disabled.")
            break
        except Exception as e:
            print(f"[ERROR] Console listener error: {e}")

# --- EVENTS ---

@client.event
async def on_ready():
    load_settings()
    print(f'[LOG] Logged in as {client.user}')
    print('[LOG] Bot is ready!')
    # Start the Hive Mind listener
    asyncio.create_task(console_listener())

@client.event
async def on_message(message):
    global typing_tasks
    
    # Check if this is a bot message to stop any typing status
    if message.author.bot:
        if message.channel.id in typing_tasks:
            typing_tasks[message.channel.id].cancel()
            del typing_tasks[message.channel.id]
        if message.author == client.user:
            return

    msg = message.content.strip().lower()
    cid = message.channel.id
    settings = get_settings(cid)

    # --- COMMANDS ---
    # Strict check: If message starts with '+' but is not a known command, ignore it.
    if msg.startswith('+'):
        known_commands = [
            '+мистрал', '+слушание', '-мистрал', '+деслушание', '+только_тут',
            '+очистить историю', '+пинг', '+хелп', '+модели', '+админ-панель',
            '+статус', '+deepwork', '+аптайм'
        ]
        if msg not in known_commands:
            return

    if msg in ['-мистрал', '+деслушание']:
        settings["enabled"] = False
        save_settings()
        await message.channel.send(embed=discord.Embed(title="❌ Бот отключен", color=discord.Color.red()))
        return

    if msg in ['+мистрал', '+слушание']:
        settings["enabled"] = True
        save_settings()
        await message.channel.send(embed=discord.Embed(title="✅ Бот включен", color=discord.Color.green()))
        return

    if msg == '+только_тут':
        # Disable everywhere
        for channel_id in channel_settings:
            channel_settings[channel_id]["enabled"] = False
        # Enable here
        settings["enabled"] = True
        save_settings()
        embed = discord.Embed(
            title="🔒 Режим 'Только здесь'", 
            description="Бот выключен во всех остальных чатах. Буду отвечать только тут.", 
            color=discord.Color.blue()
        )
        await message.channel.send(embed=embed)
        return

    if msg == '+очистить историю':
        conversation_history[cid] = []
        await message.channel.send("🧹 История очищена.")
        return

    if msg == '+пинг':
        await message.channel.send(f"🏓 Понг! {round(client.latency * 1000)}мс")
        return

    if msg == '+deepwork':
        view = DeepWorkView(cid)
        await message.channel.send(embed=view.get_embed(), view=view)
        return

    if msg == '+аптайм':
        error_log = global_settings.get("error_log", {})
        
        # Show last 30 days
        days_to_show = 30
        today = datetime.now()
        
        squares = []
        
        for i in range(days_to_show - 1, -1, -1):
            date = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            count = error_log.get(date, 0)
            
            if count <= 7:
                squares.append("🟩") # Stable/Excellent (0-7 errors)
            elif count <= 20:
                squares.append("🟨") # Unstable (8-20 errors)
            elif count <= 40:
                squares.append("🟧") # High Error Rate (21-40 errors)
            else:
                squares.append("🟥") # Critical (40+ errors)
        
        history_str = "".join(squares)
        
        embed = discord.Embed(title="Аптайм (Время безотказной работы ИИ)", color=discord.Color.green())
        embed.description = f"Последние {days_to_show} дней:\n\n{history_str}\n\n🟩 Стабильно (0-7 ошибок)\n🟨 Нестабильно (8-20 ошибок)\n🟧 Сбои (21-40 ошибок)\n🟥 Критично (40+ ошибок)"
        await message.channel.send(embed=embed)
        return



    if msg == '+хелп':
        desc = (
            "🌌 **Mirra AI — Ваш ультимативный Хаб Агентов для кодинга**\n"
            "Mirra AI трансформирует ваш Discord сервер в продвинутую среду разработки. Это не просто бот, а единая точка доступа к лучшим (и даже экспериментальным!) ИИ-персонам индустрии.\n\n"
            "Зачем ограничиваться одной моделью, когда можно собрать совет директоров из нейросетей?\n\n"
            "🤖 **Арсенал Агентов:**\n"
            "⚡ **Mistral Large**: Наш основной двигатель. Быстрый, точный, идеален для повседневного написания кода, рефакторинга и объяснения ошибок.\n"
            "🧠 **Claude Opus 4.5**: (Режим эмуляции) Агент с глубоким тактическим мышлением для самых сложных архитектурных споров.\n"
            "🔮 **GPT-5.2 Codex**: (Режим эмуляции) Футуристический агент, заточенный под генерацию бойлерплейта и системных решений.\n"
            "🌐 **Gemini 3 Pro**: (Режим эмуляции) Специалист по креативным и нестандартным задачам.\n"
            "*(Примечание: Переключение между агентами происходит мгновенно через удобное меню кнопок!)*\n\n"
            "🚀 **Ключевые фишки для Dev-среды:**\n"
            "🔒 **Изолированные Воркспейсы**: Настройки агента и история переписки привязаны к конкретному каналу. В #python-help может отвечать Mistral, а в #architect-chat — Claude. Никакой путаницы.\n"
            "📝 **Глубокий Контекст**: Mirra AI помнит нить разговора. Обсуждайте задачу итеративно, как с живым коллегой.\n"
            "🎨 **Premium UI**: Никакого визуального шума. Красивые Embed-карточки, интерактивные кнопки, цветовая индикация статусов.\n\n"
            "🛠 **Командный центр:**\n"
            "`+модели` — 🎛️ Панель управления. Выберите активного агента для текущего чата в один клик.\n"
            "`+очистить историю` — 🧹 Сброс кэша. Начните обсуждение с чистого листа.\n"
            "`+мистрал` / `-мистрал` — 🟢/🔴 Контроль. Включите или поставьте агента на паузу.\n"
            "`+deepwork` — 🚀 Режим DeepWork. Глубокая концентрация.\n"
            "`+аптайм` — 📈 Стабильность. История безотказной работы.\n"
            "`+хелп` — 📜 Документация.\n\n"
            "**Mirra AI — код начинается здесь.**"
        )
        embed = discord.Embed(description=desc, color=discord.Color.from_rgb(44, 47, 51)) # Dark theme color
        await message.channel.send(embed=embed)
        return

    if msg == '+статус':
        api_status = "✅ Онлайн"
        try:
            requests.get("https://api.mistral.ai", timeout=5)
        except:
            api_status = "❌ Недоступен"
        
        embed = discord.Embed(title="📊 Статус Системы", color=discord.Color.blue())
        embed.add_field(name="Менеджер", value=f"Antigravity v2.0", inline=True)
        embed.add_field(name="API Mistral", value=api_status, inline=True)
        embed.add_field(name="Текущий чат", value="✅ Включен" if settings["enabled"] else "❌ Отключен", inline=False)
        embed.add_field(name="Модель", value=settings["model"], inline=False)
        await message.channel.send(embed=embed)
        return

    if msg == '+модели':
        await message.channel.send(
            embed=discord.Embed(title="🧠 Выбор модели", description=f"Сейчас: {settings['model']}", color=discord.Color.gold()), 
            view=ModelView(settings['model'])
        )
        return

    if msg == '+админ-панель':
        embed = discord.Embed(
            title="🛠 Админ-панель: Управление моделями",
            description="Нажмите на кнопку модели, чтобы заблокировать или разблокировать её.",
            color=discord.Color.red()
        )
        await message.channel.send(embed=embed, view=AdminPanelView())
        return

    # --- CHAT ---
    if not settings["enabled"]:
        return

    model_name = settings["model"]
    print(f"[CHAT] 👤 User ({message.author.name}): {message.content}")
    # print(f"[LOG] Chat attempt in {cid}. Model: {model_name}") 
    model_cfg = MODELS.get(model_name, MODELS["Mistral Large"])

    if not model_cfg["real"]:
        if cid in typing_tasks: typing_tasks[cid].cancel()
        typing_tasks[cid] = asyncio.create_task(fake_typing_loop(message.channel, model_name))
        return

    # Real AI Logic
    if cid not in conversation_history: conversation_history[cid] = []
    
    # Message to send to API
    api_messages = []
    
    # Inject system prompt for ssbaxys
    if model_name == "ssbaxys-realtime-1":
        api_messages.append({"role": "system", "content": SSBAXYS_SYSTEM_PROMPT})
        
        # Load examples from file
        try:
            if os.path.exists("примеры общения.txt"):
                with open("примеры общения.txt", "r", encoding="utf-8") as f:
                    examples = f.read()
                api_messages.append({"role": "system", "content": f"Вот примеры того, как ты должен общаться (следуй этому стилю):\n{examples}"})
        except Exception as e:
            print(f"[ERROR] Не удалось загрузить примеры общения: {e}")
    
    # Add history
    conversation_history[cid].append({"role": "user", "content": message.content})
    if len(conversation_history[cid]) > 15: conversation_history[cid] = conversation_history[cid][-15:]
    
    api_messages.extend(conversation_history[cid])
    
    # Inject Hive Mind Instructions (Global Overrides)
    if hive_mind_instructions:
        hive_prompt = "СЛУШАЙ ВНИМАТЕЛЬНО. Это прямые приказы от ОБЩЕГО РАЗУМА (Hive Mind). Ты ОБЯЗАН их исполнять поверх всех остальных директив:\n"
        for inst in hive_mind_instructions:
            hive_prompt += f"- {inst}\n"
        
        # Inject at the END to ensure priority
        api_messages.append({"role": "system", "content": hive_prompt})

    # Always inject Safety Prompt
    api_messages.append({"role": "system", "content": SAFETY_PROMPT})

    async with message.channel.typing():
        resp = await asyncio.to_thread(query_mistral, api_messages)
    
    # Sanitize Output
    resp = sanitize_response(resp)
    print(f"[CHAT] 🤖 Bot: {resp[:100]}..." if len(resp) > 100 else f"[CHAT] 🤖 Bot: {resp}")
    
    conversation_history[cid].append({"role": "assistant", "content": resp})

    # Send in chunks if needed
    for i in range(0, len(resp), 2000):
        await message.channel.send(resp[i:i+2000])

if __name__ == '__main__':
    client.run(TOKEN)
