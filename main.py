import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
from datetime import datetime, timedelta
from collections import defaultdict, deque
import time
import re
import json
import asyncio

# -------------------------
# intents & bot
# -------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------------
# estados/configurações
# -------------------------
antilink_ativo = True
mutes = {}  # user_id -> fim do mute (datetime)
invite_cache = {}
convites_por_usuario = {}

# flood (ban) mensagens diferentes
FLOOD_LIMIT = 10
FLOOD_WINDOW = 10.0  # 10 segundos
user_msg_times = defaultdict(lambda: deque())

# repetição de mensagens
last_msg = {}
repeat_count = defaultdict(int)
mute_level = defaultdict(int)
user_repeat_msgs = defaultdict(list)

# -------------------------
# fila de carentes / bloqueios
# -------------------------
fila_carentes = []  # lista de discord.Member
bloqueios = defaultdict(set)  # user_id -> set(bloqueado_ids)
BLOQUEIOS_FILE = "bloqueios.json"

# carrega bloqueios persistentes
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
            print("⚠️ falha ao carregar bloqueios.json, começando com vazio")
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

def normalize_message(msg: str) -> str:
    msg = msg.strip().lower()
    msg = re.sub(r'\s+', ' ', msg)
    msg = re.sub(r'[.,!?;:]', '', msg)
    return msg

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
            await canal_log.send(f"⚠️ não foi possível aplicar role mutado em {member.mention}.")
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

async def encerrar_canal(canal: discord.abc.GuildChannel):
    try:
        await canal.delete()
    except Exception:
        pass

# -------------------------
# carentes / matchmaking views
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
        # remove da fila se presente
        removed = False
        for u in list(fila_carentes):
            if u.id == self.user_id:
                fila_carentes.remove(u)
                removed = True
        if removed:
            await interaction.response.send_message("você saiu da fila.", ephemeral=True)
        else:
            await interaction.response.send_message("você não estava mais na fila.", ephemeral=True)
        # opção: desabilitar o botão depois
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

        # envia aviso público no canal
        try:
            await self.canal.send(
                f"{self.user1.mention} e {self.user2.mention}, conversa aceita!\n"
                "o canal será encerrado automaticamente em 10 minutos. "
                "vocês também podem usar o botão 'encerrar conversa' para finalizar antes.\n"
                "⚠️ se você bloquear a outra pessoa, ela não aparecerá mais na sua fila no futuro."
            )
        except Exception:
            pass

        # substitui botões por apenas o encerrar
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
        await encerrar_canal(self.canal)

    @discord.ui.button(label="não aceitar conversa", style=discord.ButtonStyle.secondary)
    async def recusar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in {self.user1.id, self.user2.id}:
            await interaction.response.send_message("você não pode interagir aqui.", ephemeral=True)
            return

        try:
            await self.canal.send("conversa recusada. voltando para a fila (se aplicável).")
        except Exception:
            pass

        # volta para fila apenas se não estiver bloqueado pelo outro
        if self.user1.id not in bloqueios.get(self.user2.id, set()):
            fila_carentes.append(self.user1)
        if self.user2.id not in bloqueios.get(self.user1.id, set()):
            fila_carentes.append(self.user2)

        await interaction.response.send_message("vocês foram recolocados na fila (se não bloqueados).", ephemeral=True)
        await encerrar_canal(self.canal)

    async def _encerrar_apos_tempo(self, segundos: int):
        await asyncio.sleep(segundos)
        if not self._encerrado:
            await encerrar_canal(self.canal)
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
        await encerrar_canal(self.canal)
        try:
            await interaction.response.send_message("canal encerrado!", ephemeral=True)
        except Exception:
            pass

# -------------------------
# comandos e eventos principais
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

async def atualizar_convites_safe(guild: discord.Guild):
    try:
        convites = await guild.invites()
        invite_cache[guild.id] = {i.code: i.uses for i in convites}
    except Exception:
        invite_cache[guild.id] = {}

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
# on_message (anti-spam, antilink, repeat)
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
            try:
                await message.channel.send(f"🔨 {member.mention} banido por flood. {len(deleted)} mensagens apagadas.", delete_after=7)
            except Exception:
                pass
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
# comandos administrativos (tree)
# -------------------------
@bot.tree.command(name="menu_admin", description="menu administrativo")
async def menu_admin(interaction: discord.Interaction):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    texto = "🧹 /clear <quantidade>\n🔨 /ban <usuário>\n🔇 /mute <tempo> <usuário>\n🚫 /link <on|off>\n💬 /falar <mensagem>\n👥 /convidados <usuário>"
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

@bot.tree.command(name="convidados", description="mostra convites")
async def convidados_cmd(interaction: discord.Interaction, usuario: discord.Member = None):
    usuario = usuario or interaction.user
    total = len(convites_por_usuario.get(usuario.id, []))
    embed = discord.Embed(title="👥 Convites", description=f"{usuario.mention} manteve **{total} pessoas** no servidor.", color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed)

# -------------------------
# comando /carente (matchmaking)
# -------------------------
@bot.tree.command(name="carente", description="entrar na fila de carentes")
async def carente(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user = interaction.user

    # verifica se já está na fila
    if any(u.id == user.id for u in fila_carentes):
        await interaction.followup.send("você já está na fila de carentes.", ephemeral=True)
        return

    # adiciona à fila
    fila_carentes.append(user)
    # resposta privada com botão para sair da fila
    view_leave = LeaveQueueView(user.id)
    await interaction.followup.send("você entrou na fila de carentes! (apenas você vê esta mensagem)\nclique em 'sair da fila' se quiser sair.", view=view_leave, ephemeral=True)

    # tenta achar par imediatamente
    # percorre uma cópia da lista para evitar problemas
    for outro in list(fila_carentes):
        if outro.id == user.id:
            continue
        # respeita bloqueios mútuos
        if user.id in bloqueios.get(outro.id, set()) or outro.id in bloqueios.get(user.id, set()):
            continue

        # remove ambos da fila (se ainda estiverem)
        try:
            fila_carentes.remove(user)
        except ValueError:
            pass
        try:
            fila_carentes.remove(outro)
        except ValueError:
            pass

        guild = interaction.guild
        # nome do canal: pecadores-<user>-<outro> (apenas os 5 primeiros chars para evitar nomes longos)
        clean1 = re.sub(r'[^a-zA-Z0-9\-]', '', user.name)[:8].lower() or "u1"
        clean2 = re.sub(r'[^a-zA-Z0-9\-]', '', outro.name)[:8].lower() or "u2"
        nome_canal = f"pecadores-{clean1}-{clean2}"
        # evita colisão: se já existir, adiciona sufixo numérico
        existing = discord.utils.get(guild.text_channels, name=nome_canal)
        suffix = 1
        while existing:
            nome_canal = f"pecadores-{clean1}-{clean2}-{suffix}"
            existing = discord.utils.get(guild.text_channels, name=nome_canal)
            suffix += 1

        # cria canal de texto
        try:
            canal = await guild.create_text_channel(nome_canal)
        except Exception:
            # fallback: usa canal atual do interaction
            canal = interaction.channel

        embed = discord.Embed(
            title="nova conversa de carentes! 💌",
            description=f"{user.mention} e {outro.mention}, vocês foram pareados. aceitem ou recusem a conversa abaixo.",
            color=discord.Color.purple()
        )
        view = ConversaView(canal, user, outro)
        try:
            await canal.send(embed=embed, view=view)
        except Exception:
            # se enviar falhar, tenta enviar no channel do interaction
            try:
                await interaction.channel.send(embed=embed, view=view)
            except Exception:
                pass
        break

# -------------------------
# utilitários extras usados antes
# -------------------------
async def atualizar_convites(guild: discord.Guild):
    try:
        convites = await guild.invites()
        invite_cache[guild.id] = {i.code: i.uses for i in convites}
    except Exception:
        invite_cache[guild.id] = {}

# -------------------------
# execução
# -------------------------
if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if not token:
        print("❌ variável TOKEN não encontrada. defina TOKEN no ambiente e rode novamente.")
    else:
        bot.run(token)
