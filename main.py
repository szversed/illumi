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
# configurações
# -------------------------
bots_permitidos = []
antilink_ativo = True
mutes = {}
invite_cache = {}  # cache para convites

# -------------------------
# funções auxiliares
# -------------------------
def tem_cargo_soberba(member: discord.Member) -> bool:
    return any(r.name.lower() == "soberba" for r in member.roles)

async def ensure_muted_role(guild: discord.Guild):
    role = discord.utils.get(guild.roles, name="mutado")
    if not role:
        role = await guild.create_role(name="mutado", reason="cargo criado para mutes")
        for canal in guild.channels:
            await canal.set_permissions(role, send_messages=False, speak=False)
    return role

async def atualizar_convites(guild: discord.Guild):
    try:
        convites = await guild.invites()
        invite_cache[guild.id] = {i.code: i.uses for i in convites}
    except Exception:
        invite_cache[guild.id] = {}

# -------------------------
# eventos
# -------------------------
@bot.event
async def on_ready():
    print(f"✅ {bot.user} está online e pronto!")
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=1420347024376725526))
        print(f"✅ {len(synced)} comandos sincronizados com sucesso no servidor 1420347024376725526.")
    except Exception as e:
        print(f"erro ao sincronizar comandos: {e}")

    for guild in bot.guilds:
        await atualizar_convites(guild)

    verificar_mutes.start()
    print("🔁 verificação automática de mutes iniciada.")

@bot.event
async def on_invite_create(invite):
    await atualizar_convites(invite.guild)

@bot.event
async def on_invite_delete(invite):
    await atualizar_convites(invite.guild)

@bot.event
async def on_member_join(member):
    guild = member.guild
    antes = invite_cache.get(guild.id, {})
    depois = {}
    try:
        convites = await guild.invites()
        depois = {i.code: i.uses for i in convites}
    except Exception:
        pass

    usado = None
    for codigo, usos in depois.items():
        if codigo in antes and usos > antes[codigo]:
            usado = codigo
            break

    if usado:
        criador = None
        for i in convites:
            if i.code == usado:
                criador = i.inviter
                break
        if criador:
            if not hasattr(criador, "convites_usados"):
                criador.convites_usados = 0
            criador.convites_usados += 1
    invite_cache[guild.id] = depois

    # ban automático de bots não permitidos
    if member.bot and member.id not in bots_permitidos:
        inviter = None
        try:
            async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.bot_add):
                if entry.target.id == member.id:
                    inviter = entry.user
                    break
        except Exception:
            inviter = None

        try:
            await guild.ban(member, reason="bot não permitido")
        except Exception:
            pass

        canal = discord.utils.get(guild.text_channels, name="confessionário")
        if not canal and guild.text_channels:
            canal = guild.text_channels[0]

        if inviter and not inviter.bot:
            try:
                await guild.ban(inviter, reason="adicionou bot não permitido")
            except Exception:
                pass
            embed = discord.Embed(
                title="🚫 bot detectado",
                description=f"o bot `{member.name}` foi banido e {inviter.mention} também foi banido por adicioná-lo.",
                color=discord.Color.red()
            )
        else:
            embed = discord.Embed(
                title="🚫 bot detectado",
                description=f"o bot `{member.name}` foi banido automaticamente (não permitido).",
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
# loop de verificação de mutes
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
# slash commands
# -------------------------
@bot.tree.command(name="menu_admin", description="mostra o menu de comandos administrativos (só soberba).", guild=discord.Object(id=1420347024376725526))
async def menu_admin(interaction: discord.Interaction):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 você não tem permissão.", ephemeral=True)
        return

    texto = """
📜 **comandos administrativos:**

🧹 `/clear <quantidade>` → apaga mensagens  
🔨 `/ban <usuários>` → bane até 5 usuários  
🔇 `/mute <tempo> <usuários>` → muta por x minutos  
🚫 `/link <on|off>` → ativa/desativa antilink  
💬 `/falar <mensagem>` → envia mensagem  
👥 `/convidados <usuário>` → mostra quantas pessoas entraram pelo link do usuário
"""
    embed = discord.Embed(title="👑 menu administrativo", description=texto, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clear", description="apaga mensagens (somente soberba).", guild=discord.Object(id=1420347024376725526))
async def clear(interaction: discord.Interaction, quantidade: int):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 permissão negada.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=quantidade)
    embed = discord.Embed(title="🧹 limpeza concluída", description=f"{len(deleted)} mensagens apagadas.", color=discord.Color.dark_gray())
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="ban", description="bane até 5 usuários (somente soberba).", guild=discord.Object(id=1420347024376725526))
async def ban(interaction: discord.Interaction, usuario1: discord.Member, usuario2: discord.Member = None, usuario3: discord.Member = None, usuario4: discord.Member = None, usuario5: discord.Member = None):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 permissão negada.", ephemeral=True)
        return
    usuarios = [u for u in (usuario1, usuario2, usuario3, usuario4, usuario5) if u]
    nomes = []
    for user in usuarios:
        try:
            await interaction.guild.ban(user, reason=f"banido por {interaction.user}")
            nomes.append(user.name)
        except Exception:
            pass
    embed = discord.Embed(title="🔨 banimento", description=f"{', '.join(nomes)} foram banidos.", color=discord.Color.red())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="mute", description="muta usuários por x minutos (somente soberba).", guild=discord.Object(id=1420347024376725526))
async def mute(interaction: discord.Interaction, tempo: int, usuario1: discord.Member, usuario2: discord.Member = None, usuario3: discord.Member = None, usuario4: discord.Member = None, usuario5: discord.Member = None):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 permissão negada.", ephemeral=True)
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
    embed = discord.Embed(title="🔇 usuários mutados", description=f"{', '.join(nomes)} mutados por {tempo} minutos.", color=discord.Color.purple())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="link", description="ativa/desativa o antilink (somente soberba).", guild=discord.Object(id=1420347024376725526))
async def link(interaction: discord.Interaction, estado: str):
    global antilink_ativo
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 permissão negada.", ephemeral=True)
        return
    if estado.lower() == "on":
        antilink_ativo = True
        embed = discord.Embed(title="🚫 antilink ativado", color=discord.Color.red())
    elif estado.lower() == "off":
        antilink_ativo = False
        embed = discord.Embed(title="✅ antilink desativado", color=discord.Color.green())
    else:
        await interaction.response.send_message("use `on` ou `off`.", ephemeral=True)
        return
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="falar", description="faz o bot enviar uma mensagem (somente soberba).", guild=discord.Object(id=1420347024376725526))
async def falar(interaction: discord.Interaction, mensagem: str):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 permissão negada.", ephemeral=True)
        return
    await interaction.response.send_message("✅ mensagem enviada.", ephemeral=True)
    await interaction.channel.send(mensagem)

# -------------------------
# comando de convidados
# -------------------------
@bot.tree.command(name="convidados", description="mostra quantas pessoas entraram pelo convite de um usuário.", guild=discord.Object(id=1420347024376725526))
async def convidados(interaction: discord.Interaction, usuario: discord.Member):
    guild = interaction.guild
    convites = await guild.invites()
    total = sum(i.uses for i in convites if i.inviter == usuario)
    embed = discord.Embed(
        title="👥 convites",
        description=f"{usuario.mention} trouxe **{total}** pessoas para o servidor.",
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed)

# -------------------------
# run bot
# -------------------------
if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if not token:
        print("❌ erro: variável TOKEN não encontrada.")
    else:
        bot.run(token)
