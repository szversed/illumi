import discord
from discord.ext import commands
import os

# -------------------------
# Configuração do bot
# -------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------------
# Variáveis do servidor
# -------------------------
GUILD_ORIGINAL_ID = 1420347024376725526  # ID do servidor que será clonado
GUILD_CLONE_NAME = "Servidor Clone"       # Nome do novo servidor
TOKEN = os.getenv("TOKEN")

# -------------------------
# Função de clonagem
# -------------------------
async def clonar_servidor():
    original = bot.get_guild(GUILD_ORIGINAL_ID)
    if not original:
        print("❌ Servidor original não encontrado.")
        return

    # Cria novo servidor vazio
    clone = await bot.create_guild(name=GUILD_CLONE_NAME)
    print(f"✅ Servidor clone criado: {clone.name}")

    # Copiar cargos
    cargos = sorted(original.roles, key=lambda r: r.position)
    for role in cargos:
        if role.is_default():
            continue
        try:
            await clone.create_role(
                name=role.name,
                permissions=role.permissions,
                colour=role.color,
                hoist=role.hoist,
                mentionable=role.mentionable,
                reason="Clone de servidor"
            )
        except Exception as e:
            print(f"❌ Erro ao clonar cargo {role.name}: {e}")

    print("✅ Todos os cargos foram clonados.")

    # Copiar categorias e canais
    for category in original.categories:
        try:
            new_category = await clone.create_category(
                name=category.name,
                overwrites=None,
                reason="Clone de servidor"
            )
        except Exception as e:
            print(f"❌ Erro ao criar categoria {category.name}: {e}")
            continue

        for channel in category.channels:
            overwrites = channel.overwrites
            try:
                if isinstance(channel, discord.TextChannel):
                    await clone.create_text_channel(
                        name=channel.name,
                        category=new_category,
                        overwrites=overwrites,
                        reason="Clone de servidor"
                    )
                elif isinstance(channel, discord.VoiceChannel):
                    await clone.create_voice_channel(
                        name=channel.name,
                        category=new_category,
                        overwrites=overwrites,
                        bitrate=channel.bitrate,
                        user_limit=channel.user_limit,
                        reason="Clone de servidor"
                    )
            except Exception as e:
                print(f"❌ Erro ao clonar canal {channel.name}: {e}")

    # Canais fora de categoria
    for channel in original.channels:
        if channel.category is None:
            try:
                if isinstance(channel, discord.TextChannel):
                    await clone.create_text_channel(
                        name=channel.name,
                        overwrites=channel.overwrites,
                        reason="Clone de servidor"
                    )
                elif isinstance(channel, discord.VoiceChannel):
                    await clone.create_voice_channel(
                        name=channel.name,
                        overwrites=channel.overwrites,
                        bitrate=channel.bitrate,
                        user_limit=channel.user_limit,
                        reason="Clone de servidor"
                    )
            except Exception as e:
                print(f"❌ Erro ao clonar canal {channel.name}: {e}")

    print("✅ Todos os canais foram clonados. Nenhuma mensagem foi copiada.")

# -------------------------
# Evento on_ready
# -------------------------
@bot.event
async def on_ready():
    print(f"✅ {bot.user} está online e pronto!")
    await clonar_servidor()
    print("✅ Clonagem do servidor finalizada.")

# -------------------------
# Run bot
# -------------------------
if not TOKEN:
    print("❌ ERRO: variável TOKEN não encontrada.")
else:
    bot.run(TOKEN)
