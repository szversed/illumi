# arquivo: bot_pecadores_final.py
# requer: discord.py 2.6+ (python 3.9+)
# defina: export TOKEN="seu_token_aqui"

import os
import re
import json
import time
import asyncio
import random
from datetime import datetime, timedelta
from collections import defaultdict, deque

import discord
from discord.ext import commands, tasks
from discord import app_commands

# -------------------------
# intents & bot
# -------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------------
# arquivos / constantes
# -------------------------
# removido bloqueio permanente por pedido; usamos cooldowns temporários
PAIR_COOLDOWNS = {}  # frozenset({u1,u2}) -> timestamp (seconds) until they can't re-pair
PAIR_COOLDOWN_SECONDS = 3 * 60  # 3 minutos
CHANNEL_DURATION = 7 * 60  # 7 minutos

# -------------------------
# estados / estruturas
# -------------------------
antilink_ativo = True
mutes = {}  # user_id -> datetime fim do mute
invite_cache = {}
convites_por_usuario = {}

# anti-flood (mensagens diferentes)
FLOOD_LIMIT = 10
FLOOD_WINDOW = 10.0  # segundos
user_msg_times = defaultdict(lambda: deque())

# repetição de mensagem
last_msg = {}
repeat_count = defaultdict(int)
mute_level = defaultdict(int)
user_repeat_msgs = defaultdict(list)

# fila e ativos
fila_carentes = []            # lista de user ids na fila (ordem)
active_users = set()          # user ids que estão em um canal criado (pendente ou ativo)
active_channels = {}          # channel_id -> dict {u1,u2,message_id,accepted_set,created_at}

# templates de nome estilo seven
NOME_TEMPLATES = [
    "pecadores-{}-{}",
    "pecado-{}-{}",
    "crime-{}-{}",
    "case-seven-{}-{}",
    "enigmatic-{}-{}",
    "investiga-{}-{}",
    "sins-{}-{}",
]

# -------------------------
# utilitários
# -------------------------
def tem_cargo_soberba(member: discord.Member) -> bool:
    try:
        return any(r.name.lower() == "soberba" for r in member.roles)
    except Exception:
        return False

def is_exempt(member: discord.Member) -> bool:
    return member.bot or tem_cargo_soberba(member)

def sanitize_name(name: str, max_len=8) -> str:
    s = re.sub(r'[^a-zA-Z0-9\-]', '', name)
    s = s[:max_len].lower() or "u"
    return s

def pair_key(u1_id: int, u2_id: int):
    return frozenset({u1_id, u2_id})

def can_pair(u1_id: int, u2_id: int) -> bool:
    key = pair_key(u1_id, u2_id)
    ts = PAIR_COOLDOWNS.get(key)
    if not ts:
        return True
    return time.time() >= ts

def set_pair_cooldown(u1_id: int, u2_id: int):
    key = pair_key(u1_id, u2_id)
    PAIR_COOLDOWNS[key] = time.time() + PAIR_COOLDOWN_SECONDS

async def ensure_muted_role(guild: discord.Guild):
    role = discord.utils.get(guild.roles, name="mutado")
    if not role:
        try:
            role = await guild.create_role(name="mutado", reason="cargo criado para mutes")
            for canal in guild.channels:
                try:
                    await canal.set_permissions(role, send_messages=False, speak=False)
                except Exception:
                    pass
        except Exception:
            return None
    return role

async def aplicar_mute(guild: discord.Guild, member: discord.Member, minutos: int, motivo: str = None, canal_log: discord.TextChannel = None):
    role = await ensure_muted_role(guild)
    fim = datetime.utcnow() + timedelta(minutes=minutos)
    try:
        if role:
            await member.add_roles(role)
        mutes[member.id] = fim
    except Exception:
        if canal_log:
            try:
                await canal_log.send(f"⚠️ não foi possível aplicar role mutado em {member.mention}.")
            except Exception:
                pass
        return

    if canal_log:
        embed = discord.Embed(
            title="🔇 mute automático aplicado",
            description=f"{member.mention} mutado por **{minutos} minutos**.\nmotivo: {motivo or 'repetição/anti-spam'}",
            color=discord.Color.purple(),
            timestamp=datetime.utcnow()
        )
        try:
            await canal_log.send(embed=embed)
        except Exception:
            pass

async def encerrar_canal_e_cleanup(canal: discord.abc.GuildChannel):
    try:
        cid = canal.id
        data = active_channels.get(cid)
        if data:
            u1 = data.get("u1")
            u2 = data.get("u2")
            if u1:
                active_users.discard(u1)
            if u2:
                active_users.discard(u2)
            try:
                del active_channels[cid]
            except Exception:
                pass
    except Exception:
        pass
    try:
        await canal.delete()
    except Exception:
        pass

# -------------------------
# carregamento seguro convites
# -------------------------
async def atualizar_convites_safe(guild: discord.Guild):
    try:
        convites = await guild.invites()
        invite_cache[guild.id] = {i.code: i.uses for i in convites}
    except Exception:
        invite_cache[guild.id] = {}

# -------------------------
# views: leave, ticket, conversation buttons
# -------------------------
class LeaveQueueView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="sair da fila", style=discord.ButtonStyle.danger, custom_id=None)
    async def sair(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("isso é só pra você.", ephemeral=True)
            return
        removed = False
        for uid in list(fila_carentes):
            if uid == self.user_id:
                fila_carentes.remove(uid)
                removed = True
        if removed:
            await interaction.response.send_message("você saiu da fila.", ephemeral=True)
        else:
            await interaction.response.send_message("você não estava mais na fila.", ephemeral=True)
        try:
            button.disabled = True
            await interaction.message.edit(view=self)
        except Exception:
            pass

class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="entrar na fila 💘", style=discord.ButtonStyle.primary, custom_id="ticket_entrar")
    async def entrar(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        guild = interaction.guild

        if user.id in active_users:
            await interaction.response.send_message("😅 você já está em um chat ativo.", ephemeral=True)
            return

        if user.id in fila_carentes:
            await interaction.response.send_message("❗ você já está na fila.", ephemeral=True)
            return

        fila_carentes.append(user.id)
        view_leave = LeaveQueueView(user.id)
        await interaction.response.send_message("💘 você entrou na fila. (apenas você vê esta mensagem)", ephemeral=True, view=view_leave)

        await tentar_formar_dupla(guild)

class ConversationView(discord.ui.View):
    def __init__(self, canal: discord.TextChannel, u1: discord.Member, u2: discord.Member, message_id: int):
        super().__init__(timeout=None)
        self.canal = canal
        self.u1 = u1
        self.u2 = u2
        self.message_id = message_id

    @discord.ui.button(label="aceitar 💞", style=discord.ButtonStyle.success, custom_id="conv_aceitar")
    async def aceitar(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        cid = self.canal.id
        if uid not in (self.u1.id, self.u2.id):
            await interaction.response.send_message("você não pode interagir aqui.", ephemeral=True)
            return

        data = active_channels.get(cid)
        if not data:
            await interaction.response.send_message("estado inválido.", ephemeral=True)
            return

        accepted = data.setdefault("accepted", set())
        accepted.add(uid)
        # edit embed to show status (same message)
        try:
            msg = await self.canal.fetch_message(self.message_id)
            embed = discord.Embed(
                title="pecadores — status",
                description=f"{self.u1.mention} {'✅' if self.u1.id in accepted else '❌'}\n{self.u2.mention} {'✅' if self.u2.id in accepted else '❌'}\n\naguardando ambos aceitarem..." ,
                color=discord.Color.purple()
            )
            await msg.edit(embed=embed, view=self)
        except Exception:
            pass

        # if both accepted -> enable sending
        if self.u1.id in accepted and self.u2.id in accepted:
            # allow send_messages
            try:
                await self.canal.set_permissions(self.u1, send_messages=True, view_channel=True)
                await self.canal.set_permissions(self.u2, send_messages=True, view_channel=True)
            except Exception:
                pass

            # replace view with EncerrarView (single button) and edit embed to show started
            enc_view = EncerrarView(self.canal, self.u1, self.u2)
            try:
                msg = await self.canal.fetch_message(self.message_id)
                embed = discord.Embed(
                    title="conversa iniciada — pecadores",
                    description=f"{self.u1.mention} e {self.u2.mention} — a conversa foi liberada. vocês têm 7 minutos. clique em **encerrar** para fechar agora.",
                    color=discord.Color.green()
                )
                await msg.edit(embed=embed, view=enc_view)
            except Exception:
                pass

            # start 7 minute timer
            active_channels[cid]["started_at"] = time.time()
            active_channels[cid]["accepted"] = set([self.u1.id, self.u2.id])
            asyncio.create_task(_auto_close_channel_after(canal=self.canal, segundos=CHANNEL_DURATION))

        await interaction.response.send_message("sua resposta foi registrada (apenas você vê).", ephemeral=True)

    @discord.ui.button(label="recusar 💔", style=discord.ButtonStyle.danger, custom_id="conv_recusar")
    async def recusar(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        cid = self.canal.id
        if uid not in (self.u1.id, self.u2.id):
            await interaction.response.send_message("você não pode interagir aqui.", ephemeral=True)
            return

        # apply pair cooldown
        set_pair_cooldown(self.u1.id, self.u2.id)

        # inform via editing same message then delete channel
        try:
            msg = await self.canal.fetch_message(self.message_id)
            embed = discord.Embed(
                title="conversa recusada",
                description=f"{interaction.user.mention} recusou a conversa. o canal será encerrado e vocês poderão tentar novamente depois de 3 minutos.",
                color=discord.Color.dark_red()
            )
            await msg.edit(embed=embed, view=None)
        except Exception:
            pass

        # cleanup
        await asyncio.sleep(1)  # pequeno delay para que a edição seja vista
        await encerrar_canal_e_cleanup(self.canal)
        await interaction.response.send_message("você recusou a conversa (apenas você vê).", ephemeral=True)

class EncerrarView(discord.ui.View):
    def __init__(self, canal: discord.TextChannel, u1: discord.Member, u2: discord.Member):
        super().__init__(timeout=None)
        self.canal = canal
        self.u1 = u1
        self.u2 = u2

    @discord.ui.button(label="encerrar agora", style=discord.ButtonStyle.danger, custom_id="encerrar_agora")
    async def encerrar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self.u1.id, self.u2.id):
            await interaction.response.send_message("você não pode encerrar.", ephemeral=True)
            return
        try:
            msg = await self.canal.history(limit=1).next()
        except Exception:
            msg = None
        await encerrar_canal_e_cleanup(self.canal)
        await interaction.response.send_message("canal encerrado.", ephemeral=True)

# -------------------------
# tentativa de formar dupla
# -------------------------
async def tentar_formar_dupla(guild: discord.Guild):
    # precisa de pelo menos 2
    if len(fila_carentes) < 2:
        return

    # procura duas pessoas não bloqueadas por cooldown e não ativas
    for i in range(len(fila_carentes)):
        for j in range(i + 1, len(fila_carentes)):
            u1_id = fila_carentes[i]
            u2_id = fila_carentes[j]
            if u1_id in active_users or u2_id in active_users:
                continue
            if not can_pair(u1_id, u2_id):
                continue

            # remove ambos da fila (se ainda lá)
            try:
                fila_carentes.remove(u1_id)
            except ValueError:
                pass
            try:
                fila_carentes.remove(u2_id)
            except ValueError:
                pass

            u1 = guild.get_member(u1_id)
            u2 = guild.get_member(u2_id)
            if not u1 or not u2:
                # se alguém saiu do servidor, continue procurando
                continue

            # cria nome de canal com template seven-style
            clean1 = sanitize_name(u1.name, max_len=8)
            clean2 = sanitize_name(u2.name, max_len=8)
            template = random.choice(NOME_TEMPLATES)
            nome_canal = template.format(clean1, clean2)
            existing = discord.utils.get(guild.text_channels, name=nome_canal)
            suffix = 1
            while existing:
                nome_canal = f"{template.format(clean1, clean2)}-{suffix}"
                existing = discord.utils.get(guild.text_channels, name=nome_canal)
                suffix += 1

            # overwrites só pros dois e bot; inicialmente bloqueia envio (send_messages=False)
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                u1: discord.PermissionOverwrite(view_channel=True, send_messages=False),
                u2: discord.PermissionOverwrite(view_channel=True, send_messages=False),
            }

            try:
                canal = await guild.create_text_channel(nome_canal, overwrites=overwrites, reason="canal pecadores temporário")
            except Exception:
                # se falhar na criação, retorna ambos para fila (safety)
                fila_carentes.append(u1_id)
                fila_carentes.append(u2_id)
                return

            # marca como ativos (impede entrar novamente na fila)
            active_users.add(u1_id)
            active_users.add(u2_id)
            active_channels[canal.id] = {
                "u1": u1_id,
                "u2": u2_id,
                "accepted": set(),
                "message_id": None,
                "created_at": time.time()
            }

            # envia uma única mensagem embed no canal (será editada ao longo do fluxo)
            embed = discord.Embed(
                title="pecadores — confirmação",
                description=f"{u1.mention} & {u2.mention}\n\naguardando confirmação: ambos têm que aceitar para poderem conversar.\n\nbotões abaixo para aceitar ou recusar. ninguém poderá enviar mensagens até os dois aceitarem.",
                color=discord.Color.purple()
            )
            view = ConversationView(canal, u1, u2, message_id=0)
            try:
                msg = await canal.send(embed=embed, view=view)
                # store message_id and set view.message_id
                active_channels[canal.id]["message_id"] = msg.id
                view.message_id = msg.id
            except Exception:
                # se falhar, cleanup e retorna à fila
                await encerrar_canal_e_cleanup(canal)
                fila_carentes.append(u1_id)
                fila_carentes.append(u2_id)
                return

            # safety close if nobody interacts in longer time (30m)
            asyncio.create_task(_safety_close_if_no_interaction(canal, timeout=60*30))
            return

# safety: fecha canal se ninguém interagiu em X tempo
async def _safety_close_if_no_interaction(canal: discord.TextChannel, timeout: int = 1800):
    await asyncio.sleep(timeout)
    data = active_channels.get(canal.id)
    if not data:
        return
    # if accepted empty, or not started, close and cleanup
    if not data.get("accepted"):
        try:
            # edit message to notify
            try:
                msg = await canal.fetch_message(data["message_id"])
                embed = discord.Embed(
                    title="canal encerrado (inatividade)",
                    description="ninguém aceitou a conversa a tempo — canal encerrado.",
                    color=discord.Color.dark_gray()
                )
                await msg.edit(embed=embed, view=None)
            except Exception:
                pass
            await asyncio.sleep(1)
            await encerrar_canal_e_cleanup(canal)
        except Exception:
            pass

# auto close after accepted started
async def _auto_close_channel_after(canal: discord.TextChannel, segundos: int):
    await asyncio.sleep(segundos)
    if canal.id not in active_channels:
        return
    # close and cleanup
    try:
        data = active_channels.get(canal.id)
        if data:
            # edit final message to indicate timeout
            try:
                msg = await canal.fetch_message(data["message_id"])
                embed = discord.Embed(
                    title="tempo esgotado",
                    description="seu tempo de conversa de 7 minutos terminou. canal encerrado.",
                    color=discord.Color.dark_gray()
                )
                await msg.edit(embed=embed, view=None)
            except Exception:
                pass
        await asyncio.sleep(1)
        await encerrar_canal_e_cleanup(canal)
    except Exception:
        pass

# -------------------------
# eventos principais
# -------------------------
@bot.event
async def on_ready():
    print(f"✅ {bot.user} online!")
    for guild in bot.guilds:
        try:
            bot.tree.clear_commands(guild=guild)
            await bot.tree.sync(guild=guild)
        except Exception:
            pass
    try:
        await bot.tree.sync()
    except Exception:
        pass
    print("✅ comandos sincronizados")
    for guild in bot.guilds:
        await atualizar_convites_safe(guild)
    if not verificar_mutes.is_running():
        verificar_mutes.start()
        print("🔁 loop de mutes iniciado.")

@bot.event
async def on_member_join(member):
    guild = member.guild
    antes = invite_cache.get(guild.id, {})
    depois = {}
    convites = []
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
    expirados = [user_id for user_id, fim in list(mutes.items()) if agora >= fim]
    for user_id in expirados:
        for guild in bot.guilds:
            member = guild.get_member(user_id)
            if member:
                role = discord.utils.get(guild.roles, name="mutado")
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role)
                    except Exception:
                        pass
        try:
            del mutes[user_id]
        except KeyError:
            pass

# -------------------------
# on_message: anti-flood antilink repeat
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

    # flood mensagens diferentes
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
            try:
                await message.channel.send(f"🔨 {member.mention} banido por flood. {len(deleted)} mensagens apagadas.", delete_after=7)
            except Exception:
                pass
        except Exception:
            if canal_log:
                try:
                    await canal_log.send(f"⚠️ tentativa de ban automático falhou para {member.mention}.")
                except Exception:
                    pass
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
        minutos = 5 if nivel == 0 else 10 if nivel == 1 else 20
        mute_level[member.id] = min(nivel + 1, 3)
        motivo = f"repetição ({repeat_count[member.id]}x) - nível {mute_level[member.id]}"
        for msg_to_delete in list(user_repeat_msgs[member.id]):
            try:
                await msg_to_delete.delete()
            except Exception:
                pass
        await aplicar_mute(message.guild, member, minutos, motivo, canal_log)
        repeat_count[member.id] = 0
        last_msg[member.id] = None
        user_repeat_msgs[member.id] = []
        return

    await bot.process_commands(message)

# -------------------------
# comandos administrativos (tree) - mantidos, exceto /convidados
# -------------------------
@bot.tree.command(name="menu_admin", description="menu administrativo")
async def menu_admin(interaction: discord.Interaction):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    texto = "🧹 /clear <quantidade>\n🔨 /ban <usuário>\n🔇 /mute <tempo> <usuário>\n🚫 /link <on|off>\n💬 /falar <mensagem>"
    embed = discord.Embed(title="👑 Menu Administrativo", description=texto, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clear", description="apaga mensagens")
async def clear(interaction: discord.Interaction, quantidade: int):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=quantidade)
        embed = discord.Embed(title="🧹 Limpeza concluída", description=f"{len(deleted)} mensagens apagadas", color=discord.Color.dark_gray())
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception:
        await interaction.followup.send("erro ao apagar mensagens", ephemeral=True)

@bot.tree.command(name="ban", description="bane usuário")
async def ban(interaction: discord.Interaction, usuario: discord.Member):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    try:
        await interaction.guild.ban(usuario, reason=f"Banido por {interaction.user}")
        embed = discord.Embed(title="🔨 Banido", description=f"{usuario.mention} foi banido.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
    except Exception:
        await interaction.response.send_message("erro ao banir", ephemeral=True)

@bot.tree.command(name="mute", description="mute usuário")
async def mute(interaction: discord.Interaction, tempo: int, usuario: discord.Member):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    role = await ensure_muted_role(interaction.guild)
    fim = datetime.utcnow() + timedelta(minutes=tempo)
    try:
        if role:
            await usuario.add_roles(role)
        mutes[usuario.id] = fim
        embed = discord.Embed(title="🔇 Usuário mutado", description=f"{usuario.mention} mutado por {tempo} minutos.", color=discord.Color.purple())
        await interaction.response.send_message(embed=embed)
    except Exception:
        await interaction.response.send_message("erro ao mutar", ephemeral=True)

@bot.tree.command(name="link", description="ativa/desativa antilink")
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
        await interaction.response.send_message("use `on` ou `off`.", ephemeral=True)
        return
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="falar", description="bot envia mensagem")
async def falar(interaction: discord.Interaction, mensagem: str):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    await interaction.response.send_message("✅ Mensagem enviada", ephemeral=True)
    try:
        await interaction.channel.send(mensagem)
    except Exception:
        pass

# -------------------------
# comando /setupcarente (centro de tickets)
# -------------------------
@bot.tree.command(name="setupcarente", description="configura o sistema de carentes (admin)")
async def setupcarente(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("🚫 apenas administradores podem usar isso", ephemeral=True)
        return

    embed = discord.Embed(
        title="💔 está se sentindo carente?",
        description="clique em **entrar na fila 💘** pra conversar com alguém.\nninguém além de você verá a confirmação.",
        color=discord.Color.purple()
    )
    view = TicketView()
    try:
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message("✅ sistema configurado (mensagem enviada neste canal).", ephemeral=True)
    except Exception:
        await interaction.response.send_message("erro ao enviar a mensagem de setup.", ephemeral=True)

# -------------------------
# evento: canal deletado -> cleanup active users
# -------------------------
@bot.event
async def on_guild_channel_delete(channel):
    if not isinstance(channel, discord.TextChannel):
        return
    cid = channel.id
    if cid in active_channels:
        data = active_channels.get(cid, {})
        u1 = data.get("u1")
        u2 = data.get("u2")
        if u1:
            active_users.discard(u1)
        if u2:
            active_users.discard(u2)
        try:
            del active_channels[cid]
        except Exception:
            pass

# -------------------------
# execução
# -------------------------
if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if not token:
        print("❌ variável TOKEN não encontrada. defina TOKEN no ambiente e rode novamente.")
    else:
        bot.run(token)
