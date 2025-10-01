import discord
from discord.ext import commands, tasks
import aiosqlite
import asyncio
import os
from datetime import datetime, timedelta
import logging
import json
import re
from dotenv import load_dotenv, find_dotenv

# requirements:
# discord.py>=2.3.0
# aiosqlite>=0.17.0
# python-dotenv>=0.19.0

# Set up logging
logging.basicConfig(level=logging.INFO)

# Bot configuration
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.guild_messages = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Resolve .env explicitly and log outcome
env_path = find_dotenv(usecwd=True)
loaded = load_dotenv(dotenv_path=env_path, override=False, verbose=True)
print(f" 3 \n 2")
token = os.getenv("DISCORD_TOKEN")
print(f" {'1' if token else '..oops'}")

if not token:
    raise RuntimeError("DISCORD_TOKEN not found in environment. Check .env and loading order.")

# Database setup
DB_PATH = "quiz_game.db"

# Game states
active_games = {}
active_challenges = {}
challenge_channels = {}
webhook_user_mappings = {}  # Maps webhook IDs to user IDs
server_logging_channels = {}  # Maps guild IDs to logging channel IDs
user_active_challenges = {}  # Maps user IDs to their current challenge channel IDs
mono_sessions = {}  # Maps channel IDs to mono sessions

# Challenge types with scoring systems
CHALLENGE_TYPES = {
    'classic': {
        'name': 'Classic Challenge',
        'description': 'Standard scoring system',
        'correct_points': 4,
        'wrong_points': -1,
        'time_limit': None
    },
    'speed': {
        'name': 'Speed Challenge',
        'description': 'Fast-paced with time pressure',
        'correct_points': 6,
        'wrong_points': -2,
        'time_limit': 20
    },
    'precision': {
        'name': 'Precision Challenge',
        'description': 'Pure Performance',
        'correct_points': 5,
        'wrong_points': -5,
        'time_limit': None
    },
    'survival': {
        'name': 'Survival Challenge',
        'description': 'No negative points, but lower rewards',
        'correct_points': 3,
        'wrong_points': 0,
        'time_limit': None
    }
}

GAME_MODES = {
    'classic': {'name': 'Classic', 'time_limit': None, 'bonus_multiplier': 1},
    'timed': {'name': 'Timed', 'time_limit': 30, 'bonus_multiplier': 1.2},
    'blitz': {'name': 'Blitz', 'time_limit': 15, 'bonus_multiplier': 1.5},
    'streak': {'name': 'Streak Master', 'time_limit': None, 'bonus_multiplier': 1.3}
}

class MonoSession:
    def __init__(self, creator_id, qbank_code, channel_id, title):
        self.creator_id = creator_id
        self.qbank_code = qbank_code
        self.channel_id = channel_id
        self.title = title
        self.participants = {}  # user_id: MonoParticipant
        self.is_active = True
        self.created_at = datetime.now()

    def add_participant(self, user_id, username):
        if user_id not in self.participants:
            self.participants[user_id] = MonoParticipant(user_id, username)
        return self.participants[user_id]

    def get_leaderboard(self):
        return sorted(self.participants.values(), 
                     key=lambda p: (p.percentage, p.total_score), reverse=True)

class MonoParticipant:
    def __init__(self, user_id, username):
        self.user_id = user_id
        self.username = username
        self.total_score = 0
        self.correct_count = 0
        self.wrong_count = 0
        self.total_questions = 0
        self.percentage = 0.0

class ChallengePlayer:
    def __init__(self, user_id, username):
        self.user_id = user_id
        self.username = username
        self.correct_count = 0
        self.wrong_count = 0
        self.total_points = 0

    def add_correct(self, points):
        self.correct_count += 1
        self.total_points += points

    def add_wrong(self, points):
        self.wrong_count += 1
        self.total_points += points  # points will be negative

class Challenge:
    def __init__(self, challenger_id, challenged_id, challenge_type, qbank_code, main_channel_id):
        self.challenger_id = challenger_id
        self.challenged_id = challenged_id
        self.challenge_type = challenge_type
        self.qbank_code = qbank_code
        self.main_channel_id = main_channel_id
        self.players = {}
        self.is_active = False
        self.private_channel_id = None
        self.config = CHALLENGE_TYPES[challenge_type]

    def add_player(self, user_id, username):
        self.players[user_id] = ChallengePlayer(user_id, username)

    def get_winner(self):
        if len(self.players) < 2:
            return None
        players_list = list(self.players.values())
        if players_list[0].total_points > players_list[1].total_points:
            return players_list[0]
        elif players_list[1].total_points > players_list[0].total_points:
            return players_list[1]
        else:
            return None  # Tie

class Player:
    def __init__(self, user_id, username):
        self.user_id = user_id
        self.username = username
        self.score = 0
        self.streak = 0
        self.ride_or_die_uses = 3
        self.is_ride_or_die = False
        self.on_fire_multiplier = 1.0

    def get_on_fire_multiplier(self):
        if self.streak >= 11:
            return 3.0
        elif self.streak >= 8:
            return 2.5
        elif self.streak >= 5:
            return 1.5
        return 1.0

    def correct_answer(self, base_points=10):
        self.streak += 1
        self.on_fire_multiplier = self.get_on_fire_multiplier()
        points = base_points * self.on_fire_multiplier
        if self.is_ride_or_die:
            points *= 3
            self.is_ride_or_die = False
        self.score += points
        return points

    def wrong_answer(self, base_points=10):
        points_lost = 0
        if self.is_ride_or_die:
            points_lost = base_points * 3
            self.score = max(0, self.score - points_lost)
            self.is_ride_or_die = False
        self.streak = 0
        self.on_fire_multiplier = 1.0
        return points_lost

class GameSession:
    def __init__(self, channel_id, mode='classic'):
        self.channel_id = channel_id
        self.players = {}
        self.mode = mode
        self.is_active = False
        self.current_question = 0
        self.timer_task = None
        self.mode_config = GAME_MODES.get(mode, GAME_MODES['classic'])

    def add_player(self, user_id, username):
        if user_id not in self.players:
            self.players[user_id] = Player(user_id, username)
        return self.players[user_id]

    def get_leaderboard(self):
        sorted_players = sorted(self.players.values(), key=lambda p: p.score, reverse=True)
        return sorted_players

# Helper function to send welcome message
async def send_welcome_message_to_guild(guild, is_startup=False):
    """Send welcome/startup message to a guild"""
    try:
        target_channel = None
        if not target_channel:
            for channel in guild.text_channels:
                if channel.name.lower() == 'harrow':
                    target_channel = channel
                    break
        if not target_channel:
            target_channel = guild.system_channel

        if not target_channel:
            for channel in guild.text_channels:
                if channel.name.lower() in ['general', 'main', 'lobby', 'welcome']:
                    target_channel = channel
                    break

        if not target_channel:
            for channel in guild.text_channels:
                if channel.permissions_for(guild.me).send_messages:
                    target_channel = channel
                    break

        if target_channel:
            title = "Harrow Dawns" if is_startup else "Harrow Scourges"
            description = "Feel the looming death of your imperfection. Become something more." if is_startup else "Feel the looming death of your imperfection. Become something more. We begin now."
            
            embed = discord.Embed(
                title=title,
                description=description,
                color=0x00ff00
            )

            embed.add_field(
                name="What I Do",
                value="• Create **1v1 quiz challenges** with Marrow question banks\n"
                      "• Track scores with multiple challenge types\n"
                      "• Create private battle channels automatically\n"
                      "• Support group quiz games with advanced mechanics\n"
                      "• Persistent Shortcuts Integration\n"
                      "• **Mono** for single-player score tracking\n",
                inline=False
            )

            embed.add_field(
                name="Quick Start",
                value="`!challenge @user [type] [qbank_code]` - 1v1 battle\n"
                      "`!mono [code] [correct] [total] [title]` - Submit results\n"
                      "Example: `!mono 5DLH0B6Q 45 50 Practice Test`\n",
                inline=False
            )

            embed.add_field(
                name="Mono Mode",
                value="• Submit your quiz results directly: `!mono [code] [correct] [total] [title]`\n"
                      "• Anyone can submit their results for the same question bank\n"
                      "• Automatic leaderboard tracking and percentage calculations\n",
                inline=False
            )

            embed.add_field(
                name="Essential Commands",
                value="`!gamehelp` - See all commands\n"
                      "`!getwebhook` - Get your persistent webhook URL\n"
                      "`!qbank [code]` - Generate Marrow link\n"
                      "`!monostats` - View current leaderboard\n\n",
                inline=False
            )

            embed.set_footer(text="Ready to battle? Type !gamehelp to see all commands!")

            await target_channel.send(embed=embed)
            print(f"Sent {'startup' if is_startup else 'welcome'} message to {guild.name}")
    except Exception as e:
        print(f"Error sending {'startup' if is_startup else 'welcome'} message to {guild.name}: {e}")

async def send_welcome_message_to_all_guilds():
    """Send startup message to all guilds the bot is in"""
    try:
        for guild in bot.guilds:
            await send_welcome_message_to_guild(guild, is_startup=True)
            # Small delay to avoid rate limiting
            await asyncio.sleep(0.5)
        print(f"Sent startup messages to {len(bot.guilds)} guilds")
    except Exception as e:
        print(f"Error sending startup messages: {e}")

# Helper function to get member reliably
async def get_member_safely(guild, user_id):
    """Try multiple methods to get a guild member"""
    try:
        member = guild.get_member(user_id)
        if member:
            return member
        try:
            member = await guild.fetch_member(user_id)
            if member:
                return member
        except Exception:
            pass
        try:
            user = await bot.fetch_user(user_id)
            if user:
                member = guild.get_member(user_id)
                if member:
                    return member
                return user
        except Exception:
            pass
        return None
    except Exception as e:
        print(f"Error getting member {user_id}: {e}")
        return None

# Persistent webhook management functions
async def get_or_create_logging_channel(guild):
    """Get or create the persistent logging channel for this server"""
    try:
        # Check if we already have a logging channel stored
        if guild.id in server_logging_channels:
            channel_id = server_logging_channels[guild.id]
            channel = guild.get_channel(channel_id)
            if channel:
                return channel

        # Look for existing logging channel
        for channel in guild.text_channels:
            if channel.name in ['quiz-bot-input', 'bot-logging', 'apple-shortcuts-input']:
                server_logging_channels[guild.id] = channel.id
                await save_logging_channel(guild.id, channel.id)
                return channel

        # Create new logging channel
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_messages=True, manage_webhooks=True)
            }

            channel = await guild.create_text_channel(
                name="quiz-bot-input",
                topic="Shortcuts webhook input channel - Do not delete!",
                overwrites=overwrites,
                reason="Quiz bot persistent logging channel"
            )

            server_logging_channels[guild.id] = channel.id
            await save_logging_channel(guild.id, channel.id)

            # Send setup message
            embed = discord.Embed(
                title="Quiz Bot Logging Channel Created",
                description="This channel is used for Shortcuts integration.\n"
                           "Your personal webhooks will post here, and messages will be relayed to active challenge channels.",
                color=0x3498db
            )
            await channel.send(embed=embed)
            print(f"Created logging channel: {channel.name} in {guild.name}")
            return channel

        except discord.Forbidden:
            print(f"Failed to create logging channel in {guild.name} - no permissions")
            return None
        except Exception as e:
            print(f"Error creating logging channel in {guild.name}: {e}")
            return None
    except Exception as e:
        print(f"Error in get_or_create_logging_channel: {e}")
        return None

async def get_or_create_persistent_webhook(user_id, guild):
    """Get or create a persistent webhook for a user in the server's logging channel"""
    try:
        logging_channel = await get_or_create_logging_channel(guild)
        if not logging_channel:
            return None

        # Check if user already has a webhook in this server
        webhook_data = await get_user_webhook_from_db(user_id, guild.id)
        if webhook_data:
            webhook_id, webhook_url = webhook_data
            # Verify webhook still exists
            try:
                webhook = await bot.fetch_webhook(webhook_id)
                if webhook and webhook.channel_id == logging_channel.id:
                    webhook_user_mappings[webhook_id] = user_id
                    return webhook
            except Exception:
                # Webhook was deleted, remove from database
                await remove_user_webhook_from_db(user_id, guild.id)

        # Create new webhook
        try:
            user = await get_member_safely(guild, user_id)
            username = user.display_name if hasattr(user, 'display_name') else user.name if user else f"User{user_id}"
            webhook = await logging_channel.create_webhook(name=f"{username} Logger")
            webhook_user_mappings[webhook.id] = user_id

            # Save to database
            await save_user_webhook_to_db(user_id, guild.id, webhook.id, webhook.url)
            print(f"Created persistent webhook for {username} in {guild.name}")
            return webhook

        except Exception as e:
            print(f"Error creating webhook for user {user_id} in {guild.name}: {e}")
            return None
    except Exception as e:
        print(f"Error in get_or_create_persistent_webhook: {e}")
        return None

def get_user_from_webhook_message(message):
    """Get the user ID from a webhook message"""
    try:
        if not message.webhook_id:
            return message.author.id
        if message.webhook_id in webhook_user_mappings:
            return webhook_user_mappings[message.webhook_id]
        return None
    except Exception:
        return None

def extract_answer_from_content(content):
    """Extract Y/N answer from message content"""
    try:
        if not content:
            return None
        cleaned_content = re.sub(r'<@!?\d+>\s*', '', content).strip().upper()
        if cleaned_content in ['Y', 'C', '+', 'YES', 'CORRECT']:
            return 'correct'
        elif cleaned_content in ['N', 'W', '-', 'NO', 'WRONG']:
            return 'wrong'
        return None
    except Exception:
        return None

async def relay_message_to_challenge_channels(user_id, answer, original_message):
    """Relay a webhook message to the user's active challenge channel(s)"""
    try:
        if user_id not in user_active_challenges:
            return

        challenge_channel_id = user_active_challenges[user_id]
        if challenge_channel_id not in challenge_channels:
            # Clean up stale mapping
            del user_active_challenges[user_id]
            return

        challenge = challenge_channels[challenge_channel_id]
        if not challenge.is_active or user_id not in challenge.players:
            return

        challenge_channel = bot.get_channel(challenge_channel_id)
        if not challenge_channel:
            return

        # Process the answer in the challenge channel
        player = challenge.players[user_id]

        # Get user display name
        try:
            guild_member = await get_member_safely(original_message.guild, user_id)
            display_name = guild_member.display_name if hasattr(guild_member, 'display_name') else guild_member.name if guild_member else f"User{user_id}"
        except Exception:
            display_name = f"User{user_id}"

        if answer == 'correct':
            points = challenge.config['correct_points']
            player.add_correct(points)
            embed = discord.Embed(
                title="Correct! (via Shortcut)",
                description=f"**{display_name}** +{points} points",
                color=0x00ff00
            )
            embed.add_field(name="Total Score", value=f"{player.total_points}", inline=True)
            embed.add_field(name="Correct/Wrong", value=f"{player.correct_count}/{player.wrong_count}", inline=True)
            await challenge_channel.send(embed=embed)

        elif answer == 'wrong':
            points = challenge.config['wrong_points']
            player.add_wrong(points)
            embed = discord.Embed(
                title="Wrong! (via Shortcut)",
                description=f"**{display_name}** {points} points",
                color=0xff0000
            )
            embed.add_field(name="Total Score", value=f"{player.total_points}", inline=True)
            embed.add_field(name="Correct/Wrong", value=f"{player.correct_count}/{player.wrong_count}", inline=True)
            await challenge_channel.send(embed=embed)
    except Exception as e:
        print(f"Error in relay_message_to_challenge_channels: {e}")

# Challenge View with Accept/Decline buttons
class ChallengeView(discord.ui.View):
    def __init__(self, challenger_id, challenged_id, challenge_type, qbank_code, main_channel_id):
        super().__init__(timeout=300.0)
        self.challenger_id = challenger_id
        self.challenged_id = challenged_id
        self.challenge_type = challenge_type
        self.qbank_code = qbank_code
        self.main_channel_id = main_channel_id

    @discord.ui.button(label='Accept Challenge', style=discord.ButtonStyle.success)
    async def accept_challenge(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.user.id != self.challenged_id:
                await interaction.response.send_message("Only the challenged player can accept this challenge!", ephemeral=True)
                return

            guild = interaction.guild
            challenger = await get_member_safely(guild, self.challenger_id)
            challenged = await get_member_safely(guild, self.challenged_id)
            challenger_name = challenger.display_name if hasattr(challenger, "display_name") else challenger.name if challenger else f"User{self.challenger_id}"
            challenged_name = challenged.display_name if hasattr(challenged, "display_name") else challenged.name if challenged else f"User{self.challenged_id}"

            bot_member = guild.get_member(bot.user.id)
            if not bot_member or not bot_member.guild_permissions.manage_channels:
                await interaction.response.send_message("I do not have permission to create channels! Please ensure I have 'Manage Channels' permission.", ephemeral=True)
                return

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                bot_member: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_messages=True)
            }

            if challenger and hasattr(challenger, "guild_permissions"):
                overwrites[challenger] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
            if challenged and hasattr(challenged, "guild_permissions"):
                overwrites[challenged] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

            channel_name = f"challenge-{challenger_name}-vs-{challenged_name}"
            channel_name = ''.join(c if c.isalnum() or c in '-_' else '-' for c in channel_name.lower())[:100]

            private_channel = await guild.create_text_channel(
                name=channel_name,
                overwrites=overwrites,
                reason="Challenge accepted - private battle channel"
            )

            challenger_webhook = await get_or_create_persistent_webhook(self.challenger_id, guild)
            challenged_webhook = await get_or_create_persistent_webhook(self.challenged_id, guild)

            challenge = Challenge(self.challenger_id, self.challenged_id, self.challenge_type, self.qbank_code, self.main_channel_id)
            challenge.private_channel_id = private_channel.id
            challenge.add_player(self.challenger_id, challenger_name)
            challenge.add_player(self.challenged_id, challenged_name)
            challenge.is_active = True

            active_challenges[private_channel.id] = challenge
            challenge_channels[private_channel.id] = challenge
            user_active_challenges[self.challenger_id] = private_channel.id
            user_active_challenges[self.challenged_id] = private_channel.id

            config = CHALLENGE_TYPES[self.challenge_type]
            marrow_link = f"https://link.marrow.com/join_custom_module/{self.qbank_code}"

            description = (
                f"**{challenger_name}** vs **{challenged_name}**\n\n"
                f"**Question Bank Code:** `{self.qbank_code}`\n"
                f"**Scoring:** +{config['correct_points']} correct, {config['wrong_points']} wrong\n"
                f"{'**Time Limit:** ' + str(config['time_limit']) + 's per question' if config['time_limit'] else '**Time Limit:** None'}\n\n"
                "**How to play:**\n"
                "• Use the Marrow link below to access questions\n"
                "• Mark answers with: `Y/C/+` (correct) or `N/W/-` (wrong)\n"
                "• **Shortcuts will automatically relay here**\n"
                "• Type `!endchallenge` when finished\n\n"
                f"**[Join Question Bank]({marrow_link})**"
            )

            embed = discord.Embed(
                title=f"{config['name']} Started!",
                description=description,
                color=0x00ff00
            )
            embed.set_footer(text="Good luck! May the best player win!")
            await private_channel.send(embed=embed)

            webhook_embed = discord.Embed(
                title="Your Persistent Shortcuts Webhooks",
                description="These webhook URLs are **persistent** - set them up once and reuse for all future challenges!",
                color=0x3498db
            )

            if challenger_webhook:
                webhook_embed.add_field(
                    name=f"{challenger_name}'s Persistent Webhook",
                    value=f"```{challenger_webhook.url}```",
                    inline=False
                )

            if challenged_webhook:
                webhook_embed.add_field(
                    name=f"{challenged_name}'s Persistent Webhook",
                    value=f"```{challenged_webhook.url}```",
                    inline=False
                )

            webhook_embed.add_field(
                name="Shortcuts Setup (One-Time Only)",
                value="1. Create shortcut with 'Get Contents of URL'\n2. Method: POST, Request Body: JSON\n3. JSON: `{\"content\": \"Y\"}` or `{\"content\": \"N\"}`\n4. Add to AssistiveTouch menu\n5. **Reuse this same webhook for all future challenges!**",
                inline=False
            )

            webhook_embed.add_field(
                name="How It Works",
                value="• Your shortcuts post to the persistent logging channel\n• Messages are automatically relayed to this challenge channel\n• Same webhook works for all future challenges on this server",
                inline=False
            )

            await private_channel.send(embed=webhook_embed)

            success_embed = discord.Embed(
                title="Challenge Accepted!",
                description=f"**{challenged_name}** accepted the challenge!\nHead to {private_channel.mention} to begin!",
                color=0x00ff00
            )
            await interaction.response.edit_message(embed=success_embed, view=None)

            # Start timer if needed
            if config['time_limit']:
                asyncio.create_task(start_challenge_timer(private_channel.id, config['time_limit']))

        except Exception as e:
            print(f"Error in accept_challenge: {e}")
            try:
                await interaction.response.send_message("An error occurred while setting up the challenge. Please try again.", ephemeral=True)
            except Exception:
                try:
                    await interaction.followup.send("An error occurred while setting up the challenge. Please try again.", ephemeral=True)
                except Exception:
                    pass

    @discord.ui.button(label='Decline Challenge', style=discord.ButtonStyle.danger)
    async def decline_challenge(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if interaction.user.id != self.challenged_id:
                await interaction.response.send_message("Only the challenged player can decline this challenge!", ephemeral=True)
                return

            guild = interaction.guild
            challenger = await get_member_safely(guild, self.challenger_id)
            challenged = await get_member_safely(guild, self.challenged_id)
            challenger_name = challenger.display_name if hasattr(challenger, "display_name") else challenger.name if challenger else f"User{self.challenger_id}"
            challenged_name = challenged.display_name if hasattr(challenged, "display_name") else challenged.name if challenged else f"User{self.challenged_id}"

            decline_embed = discord.Embed(
                title="Challenge Declined",
                description=f"**{challenged_name}** declined the challenge from **{challenger_name}**.",
                color=0xff0000
            )
            await interaction.response.edit_message(embed=decline_embed, view=None)

        except Exception as e:
            print(f"Error in decline_challenge: {e}")
            try:
                await interaction.response.send_message("An error occurred.", ephemeral=True)
            except Exception:
                try:
                    await interaction.followup.send("An error occurred.", ephemeral=True)
                except Exception:
                    pass

# Database functions
async def init_db():
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS game_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    username TEXT,
                    channel_id INTEGER,
                    score INTEGER,
                    streak INTEGER,
                    game_mode TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS challenge_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    challenger_id INTEGER,
                    challenged_id INTEGER,
                    winner_id INTEGER,
                    challenge_type TEXT,
                    qbank_code TEXT,
                    challenger_correct INTEGER,
                    challenger_wrong INTEGER,
                    challenger_points INTEGER,
                    challenged_correct INTEGER,
                    challenged_wrong INTEGER,
                    challenged_points INTEGER,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS persistent_webhooks (
                    user_id INTEGER,
                    guild_id INTEGER,
                    webhook_id INTEGER,
                    webhook_url TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, guild_id)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS server_logging_channels (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS mono_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    creator_id INTEGER,
                    qbank_code TEXT,
                    channel_id INTEGER,
                    title TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS mono_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER,
                    user_id INTEGER,
                    username TEXT,
                    score INTEGER,
                    correct_count INTEGER,
                    total_questions INTEGER,
                    percentage REAL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES mono_sessions (id)
                )
            """)

            await db.commit()
    except Exception as e:
        print(f"Error initializing database: {e}")

async def save_game_stats(session):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            for player in session.players.values():
                await db.execute("""
                    INSERT INTO game_stats
                    (user_id, username, channel_id, score, streak, game_mode)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (player.user_id, player.username, session.channel_id,
                      player.score, player.streak, session.mode))
            await db.commit()
    except Exception as e:
        print(f"Error saving game stats: {e}")

async def save_challenge_stats(challenge):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            players_list = list(challenge.players.values())
            if len(players_list) >= 2:
                challenger = players_list[0]
                challenged = players_list[1]
                winner = challenge.get_winner()
                winner_id = winner.user_id if winner else None

                await db.execute("""
                    INSERT INTO challenge_stats
                    (challenger_id, challenged_id, winner_id, challenge_type, qbank_code,
                     challenger_correct, challenger_wrong, challenger_points,
                     challenged_correct, challenged_wrong, challenged_points)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (challenge.challenger_id, challenge.challenged_id, winner_id,
                      challenge.challenge_type, challenge.qbank_code,
                      challenger.correct_count, challenger.wrong_count, challenger.total_points,
                      challenged.correct_count, challenged.wrong_count, challenged.total_points))
            await db.commit()
    except Exception as e:
        print(f"Error saving challenge stats: {e}")

async def save_user_webhook_to_db(user_id, guild_id, webhook_id, webhook_url):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR REPLACE INTO persistent_webhooks
                (user_id, guild_id, webhook_id, webhook_url)
                VALUES (?, ?, ?, ?)
            """, (user_id, guild_id, webhook_id, webhook_url))
            await db.commit()
    except Exception as e:
        print(f"Error saving webhook to db: {e}")

async def get_user_webhook_from_db(user_id, guild_id):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT webhook_id, webhook_url FROM persistent_webhooks
                WHERE user_id = ? AND guild_id = ?
            """, (user_id, guild_id)) as cursor:
                row = await cursor.fetchone()
                return row if row else None
    except Exception as e:
        print(f"Error getting webhook from db: {e}")
        return None

async def remove_user_webhook_from_db(user_id, guild_id):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                DELETE FROM persistent_webhooks
                WHERE user_id = ? AND guild_id = ?
            """, (user_id, guild_id))
            await db.commit()
    except Exception as e:
        print(f"Error removing webhook from db: {e}")

async def save_logging_channel(guild_id, channel_id):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR REPLACE INTO server_logging_channels
                (guild_id, channel_id)
                VALUES (?, ?)
            """, (guild_id, channel_id))
            await db.commit()
    except Exception as e:
        print(f"Error saving logging channel: {e}")

async def save_mono_session(session):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("""
                INSERT INTO mono_sessions (creator_id, qbank_code, channel_id, title)
                VALUES (?, ?, ?, ?)
            """, (session.creator_id, session.qbank_code, session.channel_id, session.title))
            session_id = cursor.lastrowid
            await db.commit()
            return session_id
    except Exception as e:
        print(f"Error saving mono session: {e}")
        return None

async def save_mono_score(session_id, user_id, username, score, correct_count, total_questions, percentage):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT INTO mono_scores (session_id, user_id, username, score, correct_count, total_questions, percentage)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (session_id, user_id, username, score, correct_count, total_questions, percentage))
            await db.commit()
    except Exception as e:
        print(f"Error saving mono score: {e}")

async def load_persistent_data():
    """Load persistent webhooks and logging channels from database on startup"""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # Load webhook mappings
            async with db.execute('SELECT webhook_id, user_id FROM persistent_webhooks') as cursor:
                async for row in cursor:
                    webhook_user_mappings[row[0]] = row[1]

            # Load logging channels
            async with db.execute('SELECT guild_id, channel_id FROM server_logging_channels') as cursor:
                async for row in cursor:
                    server_logging_channels[row[0]] = row[1]
    except Exception as e:
        print(f"Error loading persistent data: {e}")

# Bot events
@bot.event
async def on_ready():
    print(f'{bot.user} has connected to Discord!')
    await init_db()
    await load_persistent_data()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    
    # Send welcome message to all guilds on startup
    await send_welcome_message_to_all_guilds()

@bot.event
async def on_guild_join(guild):
    """Send introduction message when bot joins a server"""
    await send_welcome_message_to_guild(guild, is_startup=False)

@bot.event
async def on_message(message):
    try:
        # Prevent duplicate message relay and command processing
        if message.author.bot and not message.webhook_id:
            return

        # Ensure only process each webhook message once
        is_logging = message.webhook_id and message.channel.id in server_logging_channels.values()
        is_challenge_channel = message.channel.id in challenge_channels

        if is_logging and not is_challenge_channel:
            user_id = get_user_from_webhook_message(message)
            if user_id:
                answer = extract_answer_from_content(message.content)
                if answer:
                    await relay_message_to_challenge_channels(user_id, answer, message)
            return

        # Only process direct user input in challenge channel
        if is_challenge_channel:
            challenge = challenge_channels[message.channel.id]
            if not challenge.is_active:
                return

            user_id = message.author.id if not message.webhook_id else get_user_from_webhook_message(message)
            if not user_id or user_id not in challenge.players:
                return

            answer = extract_answer_from_content(message.content)
            if not answer:
                return

            player = challenge.players[user_id]
            try:
                guild_member = await get_member_safely(message.guild, user_id)
                display_name = guild_member.display_name if hasattr(guild_member, 'display_name') else guild_member.name if guild_member else f"User{user_id}"
            except Exception:
                display_name = f"User{user_id}"

            if answer == 'correct':
                points = challenge.config['correct_points']
                player.add_correct(points)
                embed = discord.Embed(
                    title="Correct!",
                    description=f"**{display_name}** +{points} points",
                    color=0x00ff00
                )
                embed.add_field(name="Total Score", value=f"{player.total_points}", inline=True)
                embed.add_field(name="Correct/Wrong", value=f"{player.correct_count}/{player.wrong_count}", inline=True)
                await message.channel.send(embed=embed)

            elif answer == 'wrong':
                points = challenge.config['wrong_points']
                player.add_wrong(points)
                embed = discord.Embed(
                    title="Wrong!",
                    description=f"**{display_name}** {points} points",
                    color=0xff0000
                )
                embed.add_field(name="Total Score", value=f"{player.total_points}", inline=True)
                embed.add_field(name="Correct/Wrong", value=f"{player.correct_count}/{player.wrong_count}", inline=True)
                await message.channel.send(embed=embed)
            return

        # Only process commands for normal user messages
        if not message.webhook_id and not is_challenge_channel:
            await bot.process_commands(message)
    except Exception as e:
        print(f"Error in on_message: {e}")

# Helper function to show mono leaderboard
async def show_mono_leaderboard(ctx, session):
    """Show the current mono session leaderboard"""
    try:
        if not session.participants:
            return

        leaderboard = session.get_leaderboard()
        
        embed = discord.Embed(
            title="Quiz Results Leaderboard",
            description=f"**{session.title}**\nQuestion Bank: `{session.qbank_code}`",
            color=0x3498db
        )

        leaderboard_text = ""
        for i, participant in enumerate(leaderboard, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            leaderboard_text += f"{medal} **{participant.username}** - {participant.percentage:.1f}% "
            leaderboard_text += f"({participant.correct_count}/{participant.total_questions}) - {participant.total_score} pts\n"

        embed.add_field(
            name="Rankings",
            value=leaderboard_text if leaderboard_text else "No results yet",
            inline=False
        )

        marrow_link = f"https://link.marrow.com/join_custom_module/{session.qbank_code}"
        embed.add_field(
            name="Join This Quiz",
            value=f"[Click here to attempt this quiz]({marrow_link})\n"
                  f"Submit your results: `!mono {session.qbank_code} [correct] [total]`",
            inline=False
        )

        embed.set_footer(text=f"Total participants: {len(session.participants)}")

        await ctx.send(embed=embed)
    except Exception as e:
        print(f"Error in show_mono_leaderboard: {e}")

# Mono session commands
@bot.command(name='mono')
async def submit_mono_result(ctx, qbank_code: str, correct_answers: int, total_questions: int, *, title: str = None):
    """Submit your quiz results for a question bank"""
    try:
        if correct_answers < 0 or total_questions <= 0 or correct_answers > total_questions:
            await ctx.send("Invalid input! Correct answers must be between 0 and total questions.")
            return

        # Create or get existing session
        session = None
        if ctx.channel.id in mono_sessions:
            session = mono_sessions[ctx.channel.id]
            if session.qbank_code != qbank_code:
                await ctx.send(f"A different question bank ({session.qbank_code}) is already active in this channel!\n"
                              f"Current session: **{session.title}**")
                return
        else:
            display_title = title or f"Quiz Results - {qbank_code}"
            session = MonoSession(ctx.author.id, qbank_code, ctx.channel.id, display_title)
            mono_sessions[ctx.channel.id] = session
            session_id = await save_mono_session(session)
            session.db_id = session_id

        # Add participant and their result
        participant = session.add_participant(ctx.author.id, ctx.author.display_name)
        
        # Calculate score and percentage
        percentage = (correct_answers / total_questions) * 100
        score = correct_answers * 4 - (total_questions - correct_answers) * 1  # Using classic scoring
        
        participant.correct_count = correct_answers
        participant.wrong_count = total_questions - correct_answers
        participant.total_score = score
        participant.total_questions = total_questions
        participant.percentage = percentage

        # Save to database
        if hasattr(session, 'db_id') and session.db_id:
            await save_mono_score(session.db_id, ctx.author.id, ctx.author.display_name, 
                                 score, correct_answers, total_questions, percentage)

        marrow_link = f"https://link.marrow.com/join_custom_module/{qbank_code}"

        embed = discord.Embed(
            title="Quiz Result Submitted!",
            description=f"**{ctx.author.display_name}** completed the quiz",
            color=0x00ff00
        )

        embed.add_field(
            name="Quiz Details",
            value=f"**{session.title}**\n"
                  f"**Code:** `{qbank_code}`\n"
                  f"**[Join Question Bank]({marrow_link})**",
            inline=False
        )

        embed.add_field(
            name="Your Results",
            value=f"**Score:** {score} points\n"
                  f"**Correct:** {correct_answers}/{total_questions}\n"
                  f"**Percentage:** {percentage:.1f}%",
            inline=True
        )

        embed.add_field(
            name="How Others Can Join",
            value=f"`!mono {qbank_code} [correct] [total] [title]`\n"
                  f"`!monostats` - View leaderboard",
            inline=False
        )

        await ctx.send(embed=embed)

        # Show updated leaderboard if there are multiple participants
        if len(session.participants) > 1:
            await show_mono_leaderboard(ctx, session)

    except Exception as e:
        print(f"Error in submit_mono_result: {e}")
        await ctx.send("An error occurred while submitting your result.")

@bot.command(name='monostats')
async def show_mono_stats(ctx):
    """Show the current mono session leaderboard"""
    try:
        if ctx.channel.id not in mono_sessions:
            await ctx.send("No active mono session in this channel! Start one with `!mono [code] [correct] [total] [title]`")
            return

        session = mono_sessions[ctx.channel.id]
        await show_mono_leaderboard(ctx, session)
    except Exception as e:
        print(f"Error in show_mono_stats: {e}")
        await ctx.send("An error occurred while showing stats.")

@bot.command(name='endmono')
async def end_mono_session(ctx):
    """End the active mono session"""
    try:
        if ctx.channel.id not in mono_sessions:
            await ctx.send("No active mono session in this channel!")
            return

        session = mono_sessions[ctx.channel.id]
        
        if ctx.author.id != session.creator_id and not ctx.author.guild_permissions.manage_messages:
            await ctx.send("Only the session creator or users with Manage Messages permission can end the session!")
            return

        # Show final results
        leaderboard = session.get_leaderboard()
        
        embed = discord.Embed(
            title="Mono Session Ended!",
            description=f"Final results for **{session.title}**\nQuestion Bank: `{session.qbank_code}`",
            color=0xffd700
        )

        if leaderboard:
            winner = leaderboard[0]
            embed.add_field(
                name="Winner",
                value=f"**{winner.username}** with {winner.percentage:.1f}% ({winner.correct_count}/{winner.total_questions})",
                inline=False
            )

            results_text = ""
            for i, participant in enumerate(leaderboard, 1):
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                results_text += f"{medal} **{participant.username}** - {participant.percentage:.1f}% "
                results_text += f"({participant.correct_count}/{participant.total_questions}) - {participant.total_score} pts\n"

            embed.add_field(
                name="Final Rankings",
                value=results_text,
                inline=False
            )

        embed.set_footer(text=f"Total participants: {len(session.participants)} | Session duration: {datetime.now() - session.created_at}")

        await ctx.send(embed=embed)

        # Clean up
        del mono_sessions[ctx.channel.id]
    except Exception as e:
        print(f"Error in end_mono_session: {e}")
        await ctx.send("An error occurred while ending the mono session.")

# Webhook management commands
@bot.command(name='getwebhook')
async def get_user_webhook(ctx, member: discord.Member = None):
    """Get your persistent webhook URL for Shortcuts"""
    try:
        target_user = member or ctx.author

        # Check permissions
        if member and ctx.author.id != member.id and not ctx.author.guild_permissions.manage_webhooks:
            await ctx.send("You can only get webhooks for yourself unless you have Manage Webhooks permission!")
            return

        # Get or create webhook
        webhook = await get_or_create_persistent_webhook(target_user.id, ctx.guild)
        if not webhook:
            await ctx.send("Failed to create webhook! Please check bot permissions.")
            return

        embed = discord.Embed(
            title="Your Persistent Shortcuts Webhook",
            description=f"Webhook for **{target_user.display_name}** - Use this URL for all challenges on this server!",
            color=0x00ff00
        )

        embed.add_field(
            name="Webhook URL",
            value=f"```{webhook.url}```",
            inline=False
        )

        embed.add_field(
            name="Shortcuts Setup (One-Time Only)",
            value="1. Create shortcut: 'Get Contents of URL'\n"
                  "2. Method: POST, Request Body: JSON\n"
                  "3. For 'Correct': `{\"content\": \"Y\"}`\n"
                  "4. For 'Wrong': `{\"content\": \"N\"}`\n"
                  "5. Add both shortcuts to AssistiveTouch menu",
            inline=False
        )

        embed.add_field(
            name="Benefits",
            value="• **Persistent** - works for all future challenges\n"
                  "• **Cross-channel** - automatically relays to active battles\n"
                  "• **No reconfiguration** needed between challenges",
            inline=False
        )

        # Send as DM if possible, otherwise in channel
        try:
            await target_user.send(embed=embed)
            if ctx.channel.type != discord.ChannelType.private:
                await ctx.send(f"Sent webhook URL to {target_user.display_name}'s DMs!")
        except discord.Forbidden:
            await ctx.send(embed=embed)
    except Exception as e:
        print(f"Error in get_user_webhook: {e}")
        await ctx.send("An error occurred while getting the webhook.")

@bot.command(name='createloggingchannel')
async def create_logging_channel_cmd(ctx):
    """Manually create or recreate the logging channel"""
    try:
        if not ctx.author.guild_permissions.manage_channels:
            await ctx.send("You need Manage Channels permission to use this command!")
            return

        channel = await get_or_create_logging_channel(ctx.guild)
        if channel:
            await ctx.send(f"Logging channel ready: {channel.mention}")
        else:
            await ctx.send("Failed to create logging channel. Please check bot permissions.")
    except Exception as e:
        print(f"Error in create_logging_channel_cmd: {e}")
        await ctx.send("An error occurred while creating the logging channel.")

# Challenge commands (enhanced with persistent webhook support)
@bot.command(name='challenge')
async def create_challenge(ctx, member: discord.Member, challenge_type: str = 'classic', qbank_code: str = None):
    """Challenge another player to a quiz battle"""
    try:
        if not qbank_code:
            await ctx.send("Please provide a question bank code!\nUsage: `!challenge @user [type] [qbank_code]`")
            return

        if challenge_type.lower() not in CHALLENGE_TYPES:
            available_types = ', '.join(CHALLENGE_TYPES.keys())
            await ctx.send(f"Invalid challenge type! Available types: {available_types}")
            return

        if member.bot:
            await ctx.send("You can't challenge a bot!")
            return

        if member.id == ctx.author.id:
            await ctx.send("You can't challenge yourself!")
            return

        challenge_type = challenge_type.lower()
        config = CHALLENGE_TYPES[challenge_type]

        embed = discord.Embed(
            title="Quiz Challenge!",
            description=f"**{ctx.author.display_name}** has challenged **{member.display_name}** to a quiz battle!",
            color=0xff6b00
        )

        embed.add_field(
            name="Challenge Details",
            value=f"**Type:** {config['name']}\n"
                  f"**Description:** {config['description']}\n"
                  f"**Scoring:** +{config['correct_points']} correct, {config['wrong_points']} wrong\n"
                  f"**Question Bank:** `{qbank_code}`\n"
                  f"{'**Time Limit:** ' + str(config['time_limit']) + 's per question' if config['time_limit'] else '**Time Limit:** None'}",
            inline=False
        )

        embed.add_field(
            name="What's New?",
            value="• **Persistent webhooks** - use the same Shortcuts forever\n"
                  "• **Auto-relay** - messages appear in the challenge channel\n"
                  "• **No reconfiguration** needed between challenges\n"
                  "• Both players get access to the Marrow link",
            inline=False
        )

        embed.set_footer(text=f"{member.display_name}, do you accept this challenge?")

        view = ChallengeView(ctx.author.id, member.id, challenge_type, qbank_code, ctx.channel.id)
        await ctx.send(f"{member.mention}", embed=embed, view=view)
    except Exception as e:
        print(f"Error in create_challenge: {e}")
        await ctx.send("An error occurred while creating the challenge.")

@bot.command(name='endchallenge')
async def end_challenge(ctx):
    try:
        # Robust: Accepts command from any channel, always finds the correct challenge
        cid = ctx.channel.id
        challenge = None

        if cid in challenge_channels:
            challenge = challenge_channels[cid]
        else:
            for ch in challenge_channels.values():
                if ctx.author.id in ch.players:
                    challenge = ch
                    break
            if not challenge:
                await ctx.send("No active challenge found for this channel or user!")
                return
            cid = challenge.private_channel_id

        players_list = list(challenge.players.values())
        if len(players_list) < 2:
            await ctx.send("Invalid challenge state!")
            return

        player1, player2 = players_list[0], players_list[1]
        winner = challenge.get_winner()
        await save_challenge_stats(challenge)

        embed = discord.Embed(title="Challenge Complete!", color=0xffd700 if winner else 0x888888)
        if winner:
            embed.description = f"**{winner.username}** wins with {winner.total_points} points!"
        else:
            embed.description = "It's a tie! Both players scored equally!"

        embed.add_field(name=f"{player1.username}", value=f"**Score:** {player1.total_points}\n**Correct:** {player1.correct_count}\n**Wrong:** {player1.wrong_count}", inline=True)
        embed.add_field(name=f"{player2.username}", value=f"**Score:** {player2.total_points}\n**Correct:** {player2.correct_count}\n**Wrong:** {player2.wrong_count}", inline=True)

        challenge_config = CHALLENGE_TYPES[challenge.challenge_type]
        embed.add_field(
            name="Challenge Info",
            value=f"**Type:** {challenge_config['name']}\n"
                  f"**Q-Bank:** `{challenge.qbank_code}`",
            inline=False
        )

        chn = bot.get_channel(cid)
        if chn:
            await chn.send(embed=embed)

        main_channel = bot.get_channel(challenge.main_channel_id)
        if main_channel:
            await main_channel.send("**Challenge Results Posted!**\n", embed=embed)

        for uid in list(challenge.players.keys()):
            if uid in user_active_challenges:
                del user_active_challenges[uid]

        if cid in challenge_channels:
            del challenge_channels[cid]
        if cid in active_challenges:
            del active_challenges[cid]

        await asyncio.sleep(10)
        try:
            if chn:
                await chn.delete(reason="Challenge completed")
        except Exception as e:
            print(f"Failed to delete challenge channel: {e}")
    except Exception as e:
        print(f"Error in end_challenge: {e}")
        await ctx.send("An error occurred while ending the challenge.")

# Utility commands
@bot.command(name='qbank')
async def generate_qbank_link(ctx, code: str, member: discord.Member = None):
    """Generate Marrow question bank link from code, optionally tagging someone"""
    try:
        link = f"https://link.marrow.com/join_custom_module/{code}"
        embed = discord.Embed(
            title="Marrow Question Bank",
            description=f"I invite you to solve the custom module using the code **{code}** or click on this link to join",
            color=0x3498db,
            url=link
        )

        embed.add_field(name="Code", value=f"`{code}`", inline=True)
        embed.add_field(name="Direct Link", value=f"[Click here to join]({link})", inline=False)
        embed.add_field(name="Submit Results", value=f"`!mono {code} [correct] [total] [title]`", inline=False)

        if member:
            embed.set_footer(text=f"Challenge sent by {ctx.author.display_name}")
            await ctx.send(f"{member.mention} - You've been invited to a quiz!", embed=embed)
        else:
            await ctx.send(embed=embed)
    except Exception as e:
        print(f"Error in generate_qbank_link: {e}")
        await ctx.send("An error occurred while generating the question bank link.")

# Timer functionality for challenges
async def start_challenge_timer(channel_id, duration):
    """Start a countdown timer for a challenge question"""
    try:
        if channel_id not in challenge_channels:
            return

        channel = bot.get_channel(channel_id)
        if not channel:
            return

        embed = discord.Embed(
            title="Question Timer",
            description=f"Time remaining: {duration} seconds",
            color=0xffff00
        )

        timer_message = await channel.send(embed=embed)

        for remaining in range(duration - 1, 0, -1):
            await asyncio.sleep(1)
            if channel_id not in challenge_channels:
                break

            if remaining <= 5:
                color = 0xff0000
            elif remaining <= 10:
                color = 0xff6600
            else:
                color = 0xffff00

            embed = discord.Embed(
                title="Question Timer",
                description=f"Time remaining: {remaining} seconds",
                color=color
            )

            try:
                await timer_message.edit(embed=embed)
            except Exception:
                break

        if channel_id in challenge_channels:
            embed = discord.Embed(
                title="Time's Up!",
                description="Move to the next question",
                color=0xff0000
            )

            try:
                await timer_message.edit(embed=embed)
            except Exception:
                pass
    except Exception as e:
        print(f"Error in start_challenge_timer: {e}")

# Challenge info commands
@bot.command(name='challengetypes')
async def show_challenge_types(ctx):
    """Show all available challenge types"""
    try:
        embed = discord.Embed(
            title="Challenge Types",
            description="Choose your battle style!",
            color=0x00ff00
        )

        for key, config in CHALLENGE_TYPES.items():
            embed.add_field(
                name=f"{config['name']}",
                value=f"**{config['description']}**\n"
                      f"Correct: +{config['correct_points']} | Wrong: {config['wrong_points']}\n"
                      f"{'Time: ' + str(config['time_limit']) + 's' if config['time_limit'] else 'No time limit'}",
                inline=False
            )

        embed.set_footer(text="Use: !challenge @user [type] [qbank_code]")
        await ctx.send(embed=embed)
    except Exception as e:
        print(f"Error in show_challenge_types: {e}")
        await ctx.send("An error occurred while showing challenge types.")

# Group game management commands
@bot.command(name='startgame')
async def start_game(ctx, mode: str = 'classic'):
    """Start a new quiz game session"""
    try:
        channel_id = ctx.channel.id

        if mode.lower() not in GAME_MODES:
            available_modes = ', '.join(GAME_MODES.keys())
            await ctx.send(f"Invalid game mode! Available modes: {available_modes}")
            return

        if channel_id in active_games:
            await ctx.send("A game is already active in this channel! Use `!endgame` to end it first.")
            return

        session = GameSession(channel_id, mode.lower())
        active_games[channel_id] = session
        session.is_active = True

        mode_info = GAME_MODES[mode.lower()]
        embed = discord.Embed(
            title="Quiz Game Started!",
            description=f"**Mode:** {mode_info['name']}\n"
                       f"{'**Time Limit:** ' + str(mode_info['time_limit']) + 's per question' if mode_info['time_limit'] else '**Time Limit:** None'}\n"
                       f"**Bonus Multiplier:** {mode_info['bonus_multiplier']}x\n\n"
                       "Players can join using `!join`",
            color=0x00ff00
        )

        await ctx.send(embed=embed)
    except Exception as e:
        print(f"Error in start_game: {e}")
        await ctx.send("An error occurred while starting the game.")

@bot.command(name='join')
async def join_game(ctx):
    """Join the active game in this channel"""
    try:
        channel_id = ctx.channel.id

        if channel_id not in active_games:
            await ctx.send("No active game in this channel! Use `!startgame` to start one.")
            return

        session = active_games[channel_id]
        player = session.add_player(ctx.author.id, ctx.author.display_name)

        embed = discord.Embed(
            title="Joined Game!",
            description=f"{ctx.author.display_name} has joined the game!",
            color=0x00ff00
        )

        embed.add_field(name="Ride or Die Uses", value=f"{player.ride_or_die_uses}", inline=True)
        await ctx.send(embed=embed)
    except Exception as e:
        print(f"Error in join_game: {e}")
        await ctx.send("An error occurred while joining the game.")

# Help command
@bot.command(name='gamehelp')
async def game_help(ctx):
    """Show all available commands"""
    try:
        embed = discord.Embed(
            title="Quiz Game Bot Commands",
            description="Complete command reference for Harrow",
            color=0x00ff00
        )

        embed.add_field(
            name="1v1 Challenges",
            value="`!challenge @user [type] [code]` - Start 1v1 battle\n"
                  "`!challengetypes` - Show all challenge types\n"
                  "`!endchallenge` - End current challenge\n"
                  "`!qbank [code] [@user]` - Generate Marrow link",
            inline=False
        )

        embed.add_field(
            name="Mono Challenges",
            value="`!mono [code] [correct] [total] [title]` - Submit quiz results\n"
                  "`!monostats` - View current leaderboard\n"
                  "`!endmono` - End mono session\n"
                  "Example: `!mono 5DLH0B6Q 45 50 Practice Test`",
            inline=False
        )

        embed.add_field(
            name="Persistent Shortcuts",
            value="`!getwebhook [@user]` - Get persistent webhook URL\n"
                  "`!createloggingchannel` - Create/recreate logging channel\n"
                  "**One-time setup** - works for all future challenges!",
            inline=False
        )

        embed.add_field(
            name="Group Games",
            value="`!startgame [mode]` - Start group game\n"
                  "`!join` - Join the active game\n"
                  "`!endgame` - End the current game\n"
                  "`!timer [seconds]` - Start countdown timer",
            inline=False
        )

        embed.add_field(
            name="Challenge Types",
            value="**Classic** (+4/-1) - Standard scoring\n"
                  "**Speed** (+6/-2, 20s) - Time pressure\n"
                  "**Precision** (+5/-5) - Performance\n"
                  "**Survival** (+3/0) - No penalties",
            inline=False
        )

        embed.add_field(
            name="How Persistent Webhooks Work",
            value="• **One webhook per user per server** (persistent)\n"
                  "• **Auto-relay** to active challenge channels\n"
                  "• **Cross-channel messaging** - works everywhere\n"
                  "• **No reconfiguration** between challenges",
            inline=False
        )

        embed.set_footer(text="Chill the world to death -Harrow Manifesto 20:11, p62")
        await ctx.send(embed=embed)
    except Exception as e:
        print(f"Error in game_help: {e}")
        await ctx.send("An error occurred while showing help.")

# Error handling
@bot.event
async def on_command_error(ctx, error):
    try:
        if isinstance(error, commands.CommandNotFound):
            return
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Missing required argument! Use `!gamehelp` for command usage.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("Invalid argument! Use `!gamehelp` for command usage.")
        else:
            print(f"Error in {ctx.command}: {error}")
            await ctx.send(f"An error occurred: {str(error)}")
    except Exception as e:
        print(f"Error in error handler: {e}")

# Main loop
if __name__ == "__main__":
    bot.run(token)
