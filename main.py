import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
from datetime import datetime, timedelta
from collections import defaultdict, deque
import time
import asyncio

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------------
# configurações / estados
# -------------------------
antilink_ativo = True
mutes = {}  # user_id -> datetime fim do mute (utc)
invite_cache = {}
convites_por_usuario = {}

# flood (ban) config
FLOOD_LIMIT = 10    # mais de 10 mensagens
FLOOD_WINDOW = 2.0  # em 2 segundos
user_msg_times = defaultdict(lambda: deque())

# regra de mensagens duplicadas
# armazena última mensagem e contador consecutivo por usuário
last_msg = {}  # user_id -> last message content (str)
repeat_count = defaultdict(int)  # user_id -> quantas vezes consecutivas da mesma mensagem

# níveis de mute por repetição
# 0 = sem mute aplicado ainda, 1 = já recebeu 5min, 2 = já recebeu 10min, 3 = já recebeu 20min (aplica 20 novamente se repetir)
mute_level = defaultdict(int)  # user_id -> nivel

# utilitários para isenção
def tem_cargo_soberba(member: discord.Member) -> bool:
    return any(r.name.lower() == "soberba" for r in member.roles)

def is_exempt(member: discord.Member) -> bool:
    if member.bot:
        return True
    if tem_cargo_soberba(member):
        return True
    return False

# -------------------------
# funções auxiliares
# -------------------------
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
        # se não conseguiu aplicar role, tenta notificar canal de mod
        if canal_log:
            await canal_log.send(f"⚠️ não foi possível aplicar role mutado em {member.mention}. provavelmente falta permissão.")
        return

    # mensagem pública curta (tenta enviar)
    try:
        razo = f"mutado por {minutos} minutos"
        if motivo:
            razo += f" — {motivo}"
        await member.guild.system_channel.send(f"🔇 {member.mention} {razo}")
    except Exception:
        pass

    # log detalhado se canal_log fornecido
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
    print(f"✅ {bot.user} está online!")

    # limpa comandos das guilds e sincroniza
    for guild in bot.guilds:
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)

    # sincroniza comandos globais
    await bot.tree.sync()
    print("✅ comandos globais sincronizados, guilds limpas")

    # atualiza cache de convites
    for guild in bot.guilds:
        await atualizar_convites(guild)

    # inicia loop de mutes
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
# loop de mutes (libera quando expira)
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
# on_message integrado (flood + antilink + duplicados)
# -------------------------
@bot.event
async def on_message(message):
    # ignore bots
    if message.author.bot:
        return

    # proteção: processa apenas mensagens em guild (não DMs)
    if not message.guild:
        await bot.process_commands(message)
        return

    member = message.author

    # isenção de cargos/bots
    if is_exempt(member):
        await bot.process_commands(message)
        return

    # --- FLOOD BAN (>10 msgs em 2s) ---
    now = time.time()
    dq = user_msg_times[member.id]
    dq.append(now)
    # remove timestamps fora da janela
    while dq and now - dq[0] > FLOOD_WINDOW:
        dq.popleft()

    if len(dq) > FLOOD_LIMIT:
        # tenta deletar última msg para limpar chat
        try:
            await message.delete()
        except Exception:
            pass
        # tenta banir
        try:
            await message.guild.ban(member, reason=f"flood automático: >{FLOOD_LIMIT} msgs em {FLOOD_WINDOW}s")
            try:
                await message.channel.send(f"🔨 {member.mention} banido automaticamente por flood.", delete_after=7)
            except Exception:
                pass
        except Exception:
            # se falhar (perms), tenta avisar canal de mod
            try:
                canal = discord.utils.get(message.guild.text_channels, name="mod-logs")
                if canal:
                    await canal.send(f"⚠️ tentativa de ban automático falhou para {member.mention}. verifique permissões.")
            except Exception:
                pass
        finally:
            user_msg_times.pop(member.id, None)
        return

    # --- antilink (mantive comportamento anterior) ---
    if antilink_ativo and ("http://" in message.content or "https://" in message.content):
        try:
            await message.delete()
        except Exception:
            pass
        embed = discord.Embed(description=f"🚫 {message.author.mention}, links não são permitidos!", color=discord.Color.red())
        try:
            await message.channel.send(embed=embed, delete_after=5)
        except Exception:
            pass
        return

    # --- detecção de mensagens duplicadas consecutivas ---
    # normaliza conteúdo (pode ajustar normalização se quiser ignorar espaços/maiusculas)
    conteudo = message.content.strip()
    prev = last_msg.get(member.id)

    if prev is not None and conteudo != "":
        if conteudo == prev:
            repeat_count[member.id] += 1
        else:
            repeat_count[member.id] = 1
            last_msg[member.id] = conteudo
    else:
        # primeira mensagem ou sem prev
        repeat_count[member.id] = 1
        last_msg[member.id] = conteudo

    # se chegou a 5 repetições consecutivas -> aplicar mute conforme nivel
    if repeat_count[member.id] >= 5:
        nivel = mute_level.get(member.id, 0)
        minutos = 5
        if nivel == 0:
            minutos = 5
            mute_level[member.id] = 1
        elif nivel == 1:
            minutos = 10
            mute_level[member.id] = 2
        else:
            # nivel >=2 -> aplica 20 minutos (mantém no nivel 3)
            minutos = 20
            mute_level[member.id] = max(nivel, 3)

        # tenta deletar a mensagem que causou o disparo
        try:
            await message.delete()
        except Exception:
            pass

        # aplica mute e loga
        canal_log = discord.utils.get(message.guild.text_channels, name="mod-logs")
        motivo = f"repetição de mensagem ({repeat_count[member.id]}x) - nível {mute_level[member.id]}"
        await aplicar_mute(message.guild, member, minutos, motivo=motivo, canal_log=canal_log)

        # reseta contador de repetição para evitar múltiplos disparos imediatos
        repeat_count[member.id] = 0
        last_msg[member.id] = None

        return

    # -- processa comandos finalmente --
    await bot.process_commands(message)

# -------------------------
# comandos globais (manteve os seus)
# -------------------------
@bot.tree.command(name="menu_admin", description="menu de comandos administrativos.")
async def menu_admin(interaction: discord.Interaction):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão.", ephemeral=True)
        return
    texto = """
📜 **comandos administrativos:**

🧹 /clear <quantidade>  
🔨 /ban <usuário>  
🔇 /mute <tempo> <usuário>  
🚫 /link <on|off>  
💬 /falar <mensagem>  
👥 /convidados <usuário>
"""
    embed = discord.Embed(title="👑 menu administrativo", description=texto, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clear", description="apaga mensagens.")
async def clear(interaction: discord.Interaction, quantidade: int):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=quantidade)
    embed = discord.Embed(title="🧹 limpeza concluída", description=f"{len(deleted)} mensagens apagadas.", color=discord.Color.dark_gray())
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="ban", description="bane usuário.")
async def ban(interaction: discord.Interaction, usuario: discord.Member):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão.", ephemeral=True)
        return
    try:
        await interaction.guild.ban(usuario, reason=f"banido por {interaction.user}")
        embed = discord.Embed(title="🔨 banido", description=f"{usuario.mention} foi banido.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
    except Exception:
        await interaction.response.send_message("erro ao banir.", ephemeral=True)

@bot.tree.command(name="mute", description="muta usuário por x minutos.")
async def mute(interaction: discord.Interaction, tempo: int, usuario: discord.Member):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão.", ephemeral=True)
        return
    role = await ensure_muted_role(interaction.guild)
    fim = datetime.utcnow() + timedelta(minutes=tempo)
    try:
        await usuario.add_roles(role)
        mutes[usuario.id] = fim
        embed = discord.Embed(title="🔇 usuário mutado", description=f"{usuario.mention} mutado por {tempo} minutos.", color=discord.Color.purple())
        await interaction.response.send_message(embed=embed)
    except Exception:
        await interaction.response.send_message("erro ao mutar.", ephemeral=True)

@bot.tree.command(name="link", description="ativa/desativa o antilink.")
async def link(interaction: discord.Interaction, estado: str):
    global antilink_ativo
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão.", ephemeral=True)
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

@bot.tree.command(name="falar", description="faz o bot enviar uma mensagem.")
async def falar(interaction: discord.Interaction, mensagem: str):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão.", ephemeral=True)
        return
    await interaction.response.send_message("✅ mensagem enviada.", ephemeral=True)
    await interaction.channel.send(mensagem)

@bot.tree.command(name="convidados", description="mostra quantos membros o usuário manteve no servidor.")
async def convidados_cmd(interaction: discord.Interaction, usuario: discord.Member = None):
    usuario = usuario or interaction.user
    total = len(convites_por_usuario.get(usuario.id, []))
    embed = discord.Embed(
        title="👥 convites",
        description=f"{usuario.mention} manteve **{total} pessoas** no servidor.",
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed)

# -------------------------
# execução
# -------------------------
if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if not token:
        print("❌ erro: variável TOKEN não encontrada.")
    else:
        bot.run(token)
