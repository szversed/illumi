# bot_pecadores_complete.py
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
BLOQUEIOS_FILE = "bloqueios.json"

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

# fila e bloqueios e ativos
fila_carentes = []            # lista de user ids na fila (ordem)
bloqueios = defaultdict(set)  # user_id -> set(user_id bloqueados)
active_users = set()          # user ids em conversa (não podem entrar na fila)
active_channels = {}          # channel_id -> (user1_id, user2_id)

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
# persistência bloqueios
# -------------------------
def carregar_bloqueios():
    global bloqueios
    if os.path.exists(BLOQUEIOS_FILE):
        try:
            with open(BLOQUEIOS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                loaded = defaultdict(set)
                for k, v in data.items():
                    try:
                        loaded[int(k)] = set(int(x) for x in v)
                    except Exception:
                        loaded[int(k)] = set(v)
                bloqueios = loaded
        except Exception:
            print("⚠️ falha ao carregar bloqueios.json, começando vazio")
            bloqueios = defaultdict(set)
    else:
        bloqueios = defaultdict(set)

def salvar_bloqueios():
    try:
        with open(BLOQUEIOS_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): list(v) for k, v in bloqueios.items()}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("⚠️ erro ao salvar bloqueios:", e)

carregar_bloqueios()

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
        if cid in active_channels:
            u1, u2 = active_channels.get(cid, (None, None))
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
# views: leave, conversa, encerrar
# -------------------------
class LeaveQueueView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="sair da fila", style=discord.ButtonStyle.danger)
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

class ConversaView(discord.ui.View):
    def __init__(self, canal: discord.TextChannel, user1: discord.Member, user2: discord.Member):
        super().__init__(timeout=None)
        self.canal = canal
        self.user1 = user1
        self.user2 = user2
        self._encerrado = False

    @discord.ui.button(label="aceitar conversa", style=discord.ButtonStyle.success)
    async def aceitar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in {self.user1.id, self.user2.id}:
            await interaction.response.send_message("você não pode interagir aqui.", ephemeral=True)
            return

        try:
            await self.canal.send(
                f"{self.user1.mention} e {self.user2.mention}, conversa aceita!\n"
                "o canal será encerrado automaticamente em 10 minutos. "
                "vocês também podem usar o botão 'encerrar conversa' para finalizar antes.\n"
                "⚠️ se você bloquear a outra pessoa, ela não aparecerá mais na sua fila no futuro."
            )
        except Exception:
            pass

        # substitui os botões por encerrar
        self.clear_items()
        enc_view = EncerrarView(self.canal, self.user1, self.user2)
        self.add_item(enc_view.encerrar_button)
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

        # inicia timer de 10 minutos
        asyncio.create_task(self._encerrar_apos_tempo(600))
        await interaction.response.send_message("conversa iniciada — ok!", ephemeral=True)

    @discord.ui.button(label="bloquear carente", style=discord.ButtonStyle.danger)
    async def bloquear(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in {self.user1.id, self.user2.id}:
            await interaction.response.send_message("você não pode interagir aqui.", ephemeral=True)
            return

        outro = self.user2 if interaction.user.id == self.user1.id else self.user1
        bloqueios[interaction.user.id].add(outro.id)
        salvar_bloqueios()
        try:
            await self.canal.send(f"{outro.mention} foi bloqueado por {interaction.user.mention}.")
        except Exception:
            pass
        await interaction.response.send_message("usuário bloqueado e não aparecerá mais na sua fila.", ephemeral=True)
        await encerrar_canal_e_cleanup(self.canal)

    @discord.ui.button(label="não aceitar conversa", style=discord.ButtonStyle.secondary)
    async def recusar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in {self.user1.id, self.user2.id}:
            await interaction.response.send_message("você não pode interagir aqui.", ephemeral=True)
            return

        try:
            await self.canal.send("conversa recusada. voltando para a fila (se aplicável).")
        except Exception:
            pass

        # volta para fila apenas se não estiver bloqueado pelo outro e não estiver ativo
        if self.user1.id not in bloqueios.get(self.user2.id, set()) and self.user1.id not in active_users:
            fila_carentes.append(self.user1.id)
        if self.user2.id not in bloqueios.get(self.user1.id, set()) and self.user2.id not in active_users:
            fila_carentes.append(self.user2.id)

        await interaction.response.send_message("vocês foram recolocados na fila (se não bloqueados).", ephemeral=True)
        await encerrar_canal_e_cleanup(self.canal)

    async def _encerrar_apos_tempo(self, segundos: int):
        await asyncio.sleep(segundos)
        if not self._encerrado:
            await encerrar_canal_e_cleanup(self.canal)
            self._encerrado = True

class EncerrarView(discord.ui.View):
    def __init__(self, canal: discord.TextChannel, user1: discord.Member, user2: discord.Member):
        super().__init__(timeout=None)
        self.canal = canal
        self.user1 = user1
        self.user2 = user2

    @discord.ui.button(label="encerrar conversa", style=discord.ButtonStyle.danger)
    async def encerrar_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in {self.user1.id, self.user2.id}:
            await interaction.response.send_message("você não pode encerrar.", ephemeral=True)
            return
        await encerrar_canal_e_cleanup(self.canal)
        try:
            await interaction.response.send_message("canal encerrado!", ephemeral=True)
        except Exception:
            pass

# -------------------------
# eventos principais
# -------------------------
@bot.event
async def on_ready():
    print(f"✅ {bot.user} online!")
    carregar_bloqueios()
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
# helper: esta bloqueado
# -------------------------
def esta_bloqueado(u1_id, u2_id):
    return u2_id in bloqueios.get(u1_id, set()) or u1_id in bloqueios.get(u2_id, set())

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
# ticket view (botão público que users clicam)
# -------------------------
class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="entrar na fila 💘", style=discord.ButtonStyle.primary, custom_id="ticket_entrar")
    async def entrar(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        guild = interaction.guild

        # impede se já ativo
        if user.id in active_users:
            await interaction.response.send_message("😅 você já está em uma conversa ativa.", ephemeral=True)
            return

        # impede se já na fila
        if user.id in fila_carentes:
            await interaction.response.send_message("❗ você já está na fila.", ephemeral=True)
            return

        # adiciona à fila
        fila_carentes.append(user.id)
        view_leave = LeaveQueueView(user.id)
        await interaction.response.send_message("💘 você entrou na fila. (apenas você vê esta mensagem)", ephemeral=True, view=view_leave)

        # tenta parear
        await tentar_formar_dupla(guild)

# -------------------------
# tentativa de formar dupla
# -------------------------
async def tentar_formar_dupla(guild: discord.Guild):
    # precisa de pelo menos 2
    if len(fila_carentes) < 2:
        return

    # procura duas pessoas não bloqueadas e não ativas
    for i in range(len(fila_carentes)):
        for j in range(i + 1, len(fila_carentes)):
            u1_id = fila_carentes[i]
            u2_id = fila_carentes[j]
            if u1_id in active_users or u2_id in active_users:
                continue
            if esta_bloqueado(u1_id, u2_id):
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

            # overwrites só pros dois e bot
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                u1: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                u2: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            }

            # tenta manter categoria do canal onde o ticket foi clicado (não temos contexto direto aqui),
            # cria sem category
            try:
                canal = await guild.create_text_channel(nome_canal, overwrites=overwrites, reason="canal pecadores temporário")
            except Exception:
                # fallback para usar guild system channel
                try:
                    canal = await guild.create_text_channel(nome_canal, overwrites=overwrites)
                except Exception:
                    # se falhar, não crie e retorne os usuários à fila
                    fila_carentes.append(u1_id)
                    fila_carentes.append(u2_id)
                    return

            # marca como ativos
            active_users.add(u1_id)
            active_users.add(u2_id)
            active_channels[canal.id] = (u1_id, u2_id)

            embed = discord.Embed(
                title="nova conversa de carentes — pecadores",
                description=f"{u1.mention} e {u2.mention}, vocês foram pareados. aceitem ou recusem a conversa abaixo.",
                color=discord.Color.dark_purple()
            )
            view = ConversaView(canal, u1, u2)
            try:
                await canal.send(embed=embed, view=view)
            except Exception:
                try:
                    # se não conseguir enviar no canal (permissões), fallback: enviar DM ephemeral ao iniciador
                    pass
                except Exception:
                    pass

            # inicia timer para auto-encerrar (caso ninguém aceite) — o encerramento só ocorre após aceitar -> timer é iniciado dentro do aceitar
            # porém garante que, se ninguém interagir em X tempo, o canal seja fechado (safety)
            asyncio.create_task(_safety_close_if_no_interaction(canal, timeout=60*30))  # 30 min safety
            return

# safety: fecha canal se ninguém interagiu em X tempo
async def _safety_close_if_no_interaction(canal: discord.TextChannel, timeout: int = 1800):
    await asyncio.sleep(timeout)
    if canal.id not in active_channels:
        # canal já foi limpo
        return
    # se ainda está ativo e ninguém clicou (voce poderia checar mensagens), simplesmente fecha e libera usuários
    await encerrar_canal_e_cleanup(canal)

# -------------------------
# evento: canal deletado -> cleanup active users
# -------------------------
@bot.event
async def on_guild_channel_delete(channel):
    if not isinstance(channel, discord.TextChannel):
        return
    cid = channel.id
    if cid in active_channels:
        u1, u2 = active_channels.get(cid, (None, None))
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
