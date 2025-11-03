import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
from datetime import datetime, timedelta

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------------
# Configurações
# -------------------------
bots_permitidos = []  # IDs de bots permitidos
antilink_ativo = True
mutes = {}  # {user_id: timestamp_final_do_mute}

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
    try:
        synced = await bot.tree.sync()
        print(f"✅ {len(synced)} comandos sincronizados com sucesso.")
    except Exception as e:
        print(f"Erro ao sincronizar comandos: {e}")

@bot.event
async def on_member_join(member: discord.Member):
    # Ban automático de bots não permitidos
    if member.bot and member.id not in bots_permitidos:
        guild = member.guild
        inviter = None
        try:
            async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.bot_add):
                if entry.target.id == member.id:
                    inviter = entry.user
                    break
        except Exception:
            inviter = None

        try:
            await guild.ban(member, reason="Bot não permitido")
        except Exception:
            pass

        canal = discord.utils.get(guild.text_channels, name="confessionário")
        if not canal and guild.text_channels:
            canal = guild.text_channels[0]

        if inviter and not inviter.bot:
            try:
                await guild.ban(inviter, reason="Adicionou bot não permitido")
            except Exception:
                pass
            embed = discord.Embed(
                title="🚫 bot detectado",
                description=f"O bot `{member.name}` foi banido automaticamente e {inviter.mention} também foi banido por adicioná-lo.",
                color=discord.Color.red()
            )
        else:
            embed = discord.Embed(
                title="🚫 bot detectado",
                description=f"O bot `{member.name}` foi banido automaticamente (não permitido).",
                color=discord.Color.red()
            )
        await canal.send(embed=embed)

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
# Slash Commands
# -------------------------

@bot.tree.command(name="menu_admin", description="Mostra o menu de comandos administrativos (só soberba).")
async def menu_admin(interaction: discord.Interaction):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 Você não tem permissão para ver este menu.", ephemeral=True)
        return

    texto = """
📜 **Comandos administrativos disponíveis:**

🧹 `/clear <quantidade>` → Apaga mensagens no canal  
🔨 `/ban <usuários>` → Bane até 5 usuários  
🔇 `/mute <tempo> <usuários>` → Mutar usuários por X minutos  
🚫 `/link <on|off>` → Ativa ou desativa o antilink  
💬 `/falar <mensagem>` → Faz o bot enviar mensagem
"""
    embed = discord.Embed(title="👑 Menu Administrativo", description=texto, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Clear
@bot.tree.command(name="clear", description="Apaga mensagens no canal (somente soberba).")
@app_commands.describe(quantidade="Quantidade de mensagens a apagar")
async def clear(interaction: discord.Interaction, quantidade: int):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 Permissão negada (soberba necessária).", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=quantidade)
    embed = discord.Embed(
        title="🧹 Limpeza concluída",
        description=f"{len(deleted)} mensagens apagadas.",
        color=discord.Color.dark_gray()
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

# Ban
@bot.tree.command(name="ban", description="Bane até 5 usuários (somente soberba).")
@app_commands.describe(usuario1="Usuário 1", usuario2="Usuário 2", usuario3="Usuário 3", usuario4="Usuário 4", usuario5="Usuário 5")
async def ban(interaction: discord.Interaction, usuario1: discord.Member, usuario2: discord.Member = None, usuario3: discord.Member = None, usuario4: discord.Member = None, usuario5: discord.Member = None):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 Permissão negada (soberba necessária).", ephemeral=True)
        return

    usuarios = [u for u in (usuario1, usuario2, usuario3, usuario4, usuario5) if u]
    nomes = []
    for user in usuarios:
        try:
            await interaction.guild.ban(user, reason=f"Banido por {interaction.user}")
            nomes.append(user.name)
        except Exception:
            pass

    embed = discord.Embed(
        title="🔨 Banimento",
        description=f"{', '.join(nomes)} foram banidos e suas mensagens removidas.",
        color=discord.Color.red()
    )
    await interaction.response.send_message(embed=embed)

# Mute
@bot.tree.command(name="mute", description="Mutar usuários por X minutos (somente soberba).")
@app_commands.describe(tempo="Tempo em minutos", usuario1="Usuário 1", usuario2="Usuário 2", usuario3="Usuário 3", usuario4="Usuário 4", usuario5="Usuário 5")
async def mute(interaction: discord.Interaction, tempo: int, usuario1: discord.Member, usuario2: discord.Member = None, usuario3: discord.Member = None, usuario4: discord.Member = None, usuario5: discord.Member = None):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 Permissão negada (soberba necessária).", ephemeral=True)
        return

    role = await ensure_muted_role(interaction.guild)
    usuarios = [u for u in (usuario1, usuario2, usuario3, usuario4, usuario5) if u]
    nomes = []
    fim = datetime.utcnow() + timedelta(minutes=tempo)
    for user in usuarios:
        try:
            await user.add_roles(role)
            mutes[user.id] = fim
            nomes.append(user.name)
        except Exception:
            pass

    embed = discord.Embed(
        title="🔇 Usuários mutados",
        description=f"{', '.join(nomes)} foram mutados por {tempo} minutos.",
        color=discord.Color.purple()
    )
    await interaction.response.send_message(embed=embed)

# Link
@bot.tree.command(name="link", description="Ativa ou desativa o antilink (somente soberba).")
@app_commands.describe(estado="on ou off")
async def link(interaction: discord.Interaction, estado: str):
    global antilink_ativo
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 Permissão negada (soberba necessária).", ephemeral=True)
        return

    if estado.lower() == "on":
        antilink_ativo = True
        embed = discord.Embed(title="🚫 Antilink ativado", color=discord.Color.red())
    elif estado.lower() == "off":
        antilink_ativo = False
        embed = discord.Embed(title="✅ Antilink desativado", color=discord.Color.green())
    else:
        await interaction.response.send_message("Use `on` ou `off`.", ephemeral=True)
        return

    await interaction.response.send_message(embed=embed)

# Falar
@bot.tree.command(name="falar", description="Faz o bot enviar uma mensagem (somente soberba).")
@app_commands.describe(mensagem="O que o bot deve dizer")
async def falar(interaction: discord.Interaction, mensagem: str):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 Permissão negada (soberba necessária).", ephemeral=True)
        return

    await interaction.response.send_message("✅ Mensagem enviada.", ephemeral=True)
    await interaction.channel.send(mensagem)

# -------------------------
# Run bot
# -------------------------
if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if not token:
        print("❌ ERRO: variável TOKEN não encontrada.")
    else:
        bot.run(token)
