import discord
from discord.ext import commands
from discord import app_commands
import asyncio

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Logado como {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Comandos sincronizados: {len(synced)}")
    except Exception as e:
        print(f"Erro ao sincronizar comandos: {e}")

# -------------------------
# Comando para clonar servidor
# -------------------------
@bot.tree.command(name="clonarserver", description="Clona cargos, categorias e canais do servidor atual (sem mensagens)")
async def clonarserver(interaction: discord.Interaction):
    await interaction.response.send_message("Iniciando clonagem...", ephemeral=True)
    guild = interaction.guild
    clone_name = f"{guild.name} - Clone"

    try:
        new_guild = await bot.create_guild(clone_name)
        await asyncio.sleep(5)  # Espera o novo servidor ser criado
    except Exception as e:
        await interaction.followup.send(f"❌ Não foi possível criar o servidor: {e}")
        return

    # -------------------------
    # Clonar cargos
    # -------------------------
    for role in guild.roles:
        if role.is_default():  # Pula o @everyone
            continue
        try:
            await new_guild.create_role(
                name=role.name,
                permissions=role.permissions,
                colour=role.color,
                hoist=role.hoist,
                mentionable=role.mentionable
            )
        except Exception as e:
            print(f"Erro ao criar cargo {role.name}: {e}")

    # -------------------------
    # Clonar categorias e canais
    # -------------------------
    for category in guild.categories:
        try:
            new_category = await new_guild.create_category(
                name=category.name,
                position=category.position
            )
            # Clonar canais da categoria
            for channel in category.channels:
                if isinstance(channel, discord.TextChannel):
                    await new_guild.create_text_channel(
                        name=channel.name,
                        category=new_category,
                        topic=channel.topic,
                        nsfw=channel.nsfw,
                        slowmode_delay=channel.slowmode_delay,
                        position=channel.position
                    )
                elif isinstance(channel, discord.VoiceChannel):
                    await new_guild.create_voice_channel(
                        name=channel.name,
                        category=new_category,
                        bitrate=channel.bitrate,
                        user_limit=channel.user_limit,
                        position=channel.position
                    )
        except Exception as e:
            print(f"Erro ao criar categoria {category.name}: {e}")

    await interaction.followup.send(f"✅ Servidor `{guild.name}` clonado com sucesso para `{clone_name}`!")

# -------------------------
# Run bot
# -------------------------
TOKEN = "SEU_TOKEN_AQUI"
if not TOKEN:
    print("❌ ERRO: variável TOKEN não encontrada.")
else:
    bot.run(TOKEN)
