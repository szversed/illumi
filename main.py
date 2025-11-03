import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
from datetime import datetime, timedelta

# -------------------------
# Configuração do bot
# -------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guild_messages = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------------
# Configurações gerais
# -------------------------
bots_permitidos = []  # IDs de bots permitidos
antilink_ativo = True
mutes = {}  # {user_id: timestamp_final_do_mute}
GUILD_ID = 1420347024376725526  # ID do seu servidor

# -------------------------
# Funções auxiliares
# -------------------------
def tem_cargo_soberba(member: discord.Member) -> bool:
    return any(r.name.lower() == "soberba" for r in member.roles)

async def ensure_muted_role(guild: discord.Guild):
    role = discord.utils.get(guild.roles, name="mutado")
    if not role:
        role = await guild.create_role(name="mutado", reason="Cargo criado para mutes")
        for canal in guild.channels:
            await canal.set_permissions(role, send_messages=False, speak=False)
    return role

# -------------------------
# Eventos
# -------------------------
@bot.event
async def on_ready():
    print(f"✅ {bot.user} está online e pronto!")
    guild_obj = discord.Object(id=GUILD_ID)
    try:
        synced = await bot.tree.sync(guild=guild_obj)
        print(f"✅ {len(synced)} comandos sincronizados no servidor!")
    except Exception as e:
        print(f"Erro ao sincronizar comandos: {e}")

    verificar_mutes.start()
    print("🔁 Verificação automática de mutes iniciada.")

@bot.event
async def on_message(message):
    global antilink_ativo
    if message.author.bot:
        return
    if antilink_ativo and ("http://" in message.content or "https://" in message.content):
        await message.delete()
        embed = discord.Embed(
            description=f"🚫 {message.author.mention}, links não são permitidos!",
            color=discord.Color.red()
        )
        await message.channel.send(embed=embed, delete_after=5)
    await bot.process_commands(message)

# -------------------------
# Loop de verificação de mutes
# -------------------------
@tasks.loop(seconds=30)
async def verificar_mutes():
    agora = datetime.utcnow()
    expirados = [user_id for user_id, fim in mutes.items() if agora >= fim]

    for user_id in expirados:
        for guild in bot.guilds:
            member = guild.get_member(user_id)
            if member:
                role = discord.utils.get(guild.roles, name="mutado")
                if role in member.roles:
                    try:
                        await member.remove_roles(role)
                        print(f"🔊 {member} foi desmutado automaticamente.")
                    except Exception:
                        pass
        del mutes[user_id]

# -------------------------
# Slash Commands
# -------------------------
@bot.tree.command(name="desbanirtudo", description="Desbanir todos os usuários banidos do servidor (somente soberba).")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def desbanirtudo(interaction: discord.Interaction):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 Permissão negada.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    bans = await guild.bans()
    total_desbanidos = 0

    for ban_entry in bans:
        try:
            await guild.unban(ban_entry.user, reason=f"Desban por {interaction.user}")
            total_desbanidos += 1
            print(f"✅ {ban_entry.user} desbanido")
        except Exception as e:
            print(f"❌ Falha ao desbanir {ban_entry.user}: {e}")

    embed = discord.Embed(
        title="🔓 Desbanimento completo",
        description=f"{total_desbanidos} usuários foram desbanidos do servidor.",
        color=discord.Color.green()
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

# -------------------------
# Run bot
# -------------------------
if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if not token:
        print("❌ ERRO: variável TOKEN não encontrada.")
    else:
        bot.run(token)
