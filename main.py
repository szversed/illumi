import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
from datetime import datetime, timedelta
from collections import defaultdict, deque
import time
import re

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------------
# estados/configurações
# -------------------------
antilink_ativo = True
mutes = {}  # user_id -> fim do mute
invite_cache = {}
convites_por_usuario = {}

# flood (ban) mensagens diferentes
FLOOD_LIMIT = 15
FLOOD_WINDOW = 10.0  # 10 segundos
user_msg_times = defaultdict(lambda: deque())

# repetição de mensagens
last_msg = {}
repeat_count = defaultdict(int)
mute_level = defaultdict(int)
user_repeat_msgs = defaultdict(list)

# -------------------------
# utilitários
# -------------------------
def tem_cargo_soberba(member: discord.Member) -> bool:
    return any(r.name.lower() == "soberba" for r in member.roles)

def is_exempt(member: discord.Member) -> bool:
    return member.bot or tem_cargo_soberba(member)

def normalize_message(msg: str) -> str:
    msg = msg.strip().lower()
    msg = re.sub(r'\s+', ' ', msg)
    msg = re.sub(r'[.,!?;:]', '', msg)
    return msg

async def ensure_muted_role(guild: discord.Guild):
    role = discord.utils.get(guild.roles, name="mutado")
    if not role:
        role = await guild.create_role(name="mutado", reason="cargo criado para mutes")
        for canal in guild.channels:
            try:
                await canal.set_permissions(role, send_messages=False, speak=False)
            except Exception:
                pass
    return role

async def aplicar_mute(guild: discord.Guild, member: discord.Member, minutos: int, motivo: str = None, canal_log: discord.TextChannel = None):
    role = await ensure_muted_role(guild)
    fim = datetime.utcnow() + timedelta(minutes=minutos)
    try:
        await member.add_roles(role)
        mutes[member.id] = fim
    except Exception:
        if canal_log:
            await canal_log.send(f"⚠️ não foi possível aplicar role mutado em {member.mention}.")
        return

    if canal_log:
        embed = discord.Embed(
            title="🔇 mute automático aplicado",
            description=f"{member.mention} mutado por **{minutos} minutos**.\nmotivo: {motivo or 'repetição/anti-spam'}",
            color=discord.Color.purple(),
            timestamp=datetime.utcnow()
        )
        await canal_log.send(embed=embed)

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
    print(f"✅ {bot.user} online!")

    for guild in bot.guilds:
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)
    await bot.tree.sync()
    print("✅ comandos sincronizados")

    for guild in bot.guilds:
        await atualizar_convites(guild)

    if not verificar_mutes.is_running():
        verificar_mutes.start()
        print("🔁 loop de mutes iniciado.")

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
            if criador.id not in convites_por_usuario:
                convites_por_usuario[criador.id] = []
            convites_por_usuario[criador.id].append(member.id)
    invite_cache[guild.id] = depois

@bot.event
async def on_member_remove(member):
    for criador_id, lista in list(convites_por_usuario.items()):
        if member.id in lista:
            lista.remove(member.id)
            if not lista:
                del convites_por_usuario[criador_id]
            break

# -------------------------
# loop de mutes
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
                    except Exception:
                        pass
        del mutes[user_id]

# -------------------------
# on_message
# -------------------------
@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        await bot.process_commands(message)
        return

    member = message.author
    if is_exempt(member):
        await bot.process_commands(message)
        return

    now = time.time()
    canal_log = discord.utils.get(message.guild.text_channels, name="mod-logs")

    # flood de mensagens diferentes
    dq = user_msg_times[member.id]
    dq.append(now)
    while dq and now - dq[0] > FLOOD_WINDOW:
        dq.popleft()
    if len(dq) > FLOOD_LIMIT:
        try:
            deleted = await message.channel.purge(limit=100, check=lambda m: m.author.id==member.id and now - m.created_at.timestamp()<=FLOOD_WINDOW)
        except Exception:
            deleted = []
        try:
            await message.guild.ban(member, reason=f"Flood de mensagens diferentes: >{FLOOD_LIMIT} msgs em {FLOOD_WINDOW}s")
            await message.channel.send(f"🔨 {member.mention} banido por flood. {len(deleted)} mensagens apagadas.", delete_after=7)
        except Exception:
            if canal_log:
                await canal_log.send(f"⚠️ tentativa de ban automático falhou para {member.mention}.")
        finally:
            user_msg_times.pop(member.id, None)
        return

    # antilink
    if antilink_ativo and ("http://" in message.content or "https://" in message.content):
        try:
            await message.delete()
        except Exception:
            pass
        embed = discord.Embed(description=f"🚫 {member.mention}, links não são permitidos!", color=discord.Color.red())
        try:
            await message.channel.send(embed=embed, delete_after=5)
        except Exception:
            pass
        return

    # repetição de mensagem
    conteudo = re.sub(r'\s+', ' ', message.content.strip().lower())
    prev = last_msg.get(member.id)
    user_repeat_msgs[member.id].append(message)

    if prev and conteudo != "":
        if conteudo == prev:
            repeat_count[member.id] += 1
        else:
            repeat_count[member.id] = 1
            last_msg[member.id] = conteudo
            user_repeat_msgs[member.id] = [message]
    else:
        repeat_count[member.id] = 1
        last_msg[member.id] = conteudo
        user_repeat_msgs[member.id] = [message]

    if repeat_count[member.id] >= 5:
        nivel = mute_level.get(member.id, 0)
        minutos = 5 if nivel==0 else 10 if nivel==1 else 20
        mute_level[member.id] = min(nivel+1, 3)
        motivo = f"repetição ({repeat_count[member.id]}x) - nível {mute_level[member.id]}"
        for msg_to_delete in user_repeat_msgs[member.id]:
            try: await msg_to_delete.delete()
            except: pass
        await aplicar_mute(message.guild, member, minutos, motivo, canal_log)
        repeat_count[member.id] = 0
        last_msg[member.id] = None
        user_repeat_msgs[member.id] = []
        return

    await bot.process_commands(message)

# -------------------------
# comandos administrativos
# -------------------------
@bot.tree.command(name="menu_admin", description="Menu administrativo")
async def menu_admin(interaction: discord.Interaction):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    texto = "🧹 /clear <quantidade>\n🔨 /ban <usuário>\n🔇 /mute <tempo> <usuário>\n🚫 /link <on|off>\n💬 /falar <mensagem>\n👥 /convidados <usuário>"
    embed = discord.Embed(title="👑 Menu Administrativo", description=texto, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clear", description="Apaga mensagens")
async def clear(interaction: discord.Interaction, quantidade: int):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=quantidade)
    embed = discord.Embed(title="🧹 Limpeza concluída", description=f"{len(deleted)} mensagens apagadas", color=discord.Color.dark_gray())
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="ban", description="Bane usuário")
async def ban(interaction: discord.Interaction, usuario: discord.Member):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    try:
        await interaction.guild.ban(usuario, reason=f"Banido por {interaction.user}")
        embed = discord.Embed(title="🔨 Banido", description=f"{usuario.mention} foi banido.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
    except Exception:
        await interaction.response.send_message("Erro ao banir", ephemeral=True)

@bot.tree.command(name="mute", description="Mute usuário")
async def mute(interaction: discord.Interaction, tempo: int, usuario: discord.Member):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    role = await ensure_muted_role(interaction.guild)
    fim = datetime.utcnow() + timedelta(minutes=tempo)
    try:
        await usuario.add_roles(role)
        mutes[usuario.id] = fim
        embed = discord.Embed(title="🔇 Usuário mutado", description=f"{usuario.mention} mutado por {tempo} minutos.", color=discord.Color.purple())
        await interaction.response.send_message(embed=embed)
    except Exception:
        await interaction.response.send_message("Erro ao mutar", ephemeral=True)

@bot.tree.command(name="link", description="Ativa/Desativa antilink")
async def link(interaction: discord.Interaction, estado: str):
    global antilink_ativo
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
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

@bot.tree.command(name="falar", description="Bot envia mensagem")
async def falar(interaction: discord.Interaction, mensagem: str):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    await interaction.response.send_message("✅ Mensagem enviada", ephemeral=True)
    await interaction.channel.send(mensagem)

@bot.tree.command(name="convidados", description="Mostra convites")
async def convidados_cmd(interaction: discord.Interaction, usuario: discord.Member = None):
    usuario = usuario or interaction.user
    total = len(convites_por_usuario.get(usuario.id, []))
    embed = discord.Embed(title="👥 Convites", description=f"{usuario.mention} manteve **{total} pessoas** no servidor.", color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed)

# -------------------------
# execução
# -------------------------
if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if not token:
        print("❌ variável TOKEN não encontrada")
    else:
        bot.run(token)
