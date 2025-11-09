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

# intents & bot
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# constantes / configurações
PAIR_COOLDOWNS = {} # frozenset({u1,u2}) -> timestamp until they can't re-pair
PAIR_COOLDOWN_SECONDS = 3 * 60 # 3 minutos de cooldown após recusar
ACCEPT_TIMEOUT = 60 # 1 minuto para ambos aceitarem
CHANNEL_DURATION = 7 * 60 # 7 minutos de conversa após ambos aceitarem
SAFETY_TIMEOUT = 60 * 30 # 30 minutos safety para canais pendentes

# estados / estruturas
antilink_ativo = True
mutes = {} # user_id -> datetime fim do mute
invite_cache = {}
convites_por_usuario = {}

# gêneros dos usuários
user_genders = {} # user_id -> "homem" ou "mulher"
user_preferences = {} # user_id -> "homem", "mulher" ou "ambos"

# anti-flood (mensagens diferentes)
FLOOD_LIMIT = 10
FLOOD_WINDOW = 10.0 # segundos
user_msg_times = defaultdict(lambda: deque())

# repetição de mensagem
last_msg = {}
repeat_count = defaultdict(int)
mute_level = defaultdict(int)
user_repeat_msgs = defaultdict(list)

# fila e ativos
fila_carentes = [] # lista de dicts {user_id, gender, preference} na fila
active_users = set() # user ids que estão em um canal criado (pendente ou ativo)
active_channels = {} # channel_id -> dict {u1,u2,accepted,message_id,created_at,started}

# nickname lock: quando soberba altera, guardamos o valor bloqueado
blocked_nick = {} # user_id -> nick (None means allowed again)

# nome base para canais (será numerado: pecadores, pecadores-1, pecadores-2...)
CHANNEL_BASE = "pecadores"

# (opcional) mapping guild.id -> setup channel id (mantido para compatibilidade)
SETUP_CHANNELS = {}

# filas de música por usuário
music_queues = {} # user_id -> list de musicas
music_players = {} # user_id -> current player

# utilitários
def tem_cargo_soberba(member: discord.Member) -> bool:
    try:
        return any(r.name.lower() == "soberba" for r in member.roles)
    except Exception:
        return False

def is_exempt(member: discord.Member) -> bool:
    return member.bot or tem_cargo_soberba(member)

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

def get_gender_display(gender):
    return "pecador" if gender == "homem" else "pecadora"

async def ensure_muted_role(guild: discord.Guild):
    role = discord.utils.get(guild.roles, name="mutado")
    if not role:
        try:
            role = await guild.create_role(name="mutado", reason="cargo criado para mutes")
            for canal in guild.text_channels:
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
            description=f"{member.mention} mutado por {minutos} minutos.\nmotivo: {motivo or 'repetição/anti-spam'}",
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

async def atualizar_convites_safe(guild: discord.Guild):
    try:
        convites = await guild.invites()
        invite_cache[guild.id] = {i.code: i.uses for i in convites}
    except Exception:
        invite_cache[guild.id] = {}

# views: leave, ticket, conversation buttons
class LeaveQueueView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="sair da fila", style=discord.ButtonStyle.danger, custom_id="leavefila_button")
    async def sair(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("isso é só pra você.", ephemeral=True)
            return
        removed = False
        for entry in list(fila_carentes):
            if entry["user_id"] == self.user_id:
                fila_carentes.remove(entry)
                removed = True
                break
        if removed:
            await interaction.response.send_message("você saiu da fila.", ephemeral=True)
        else:
            await interaction.response.send_message("você não estava mais na fila.", ephemeral=True)
        try:
            button.disabled = True
            await interaction.message.edit(view=self)
        except Exception:
            pass

class GenderSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Homem", style=discord.ButtonStyle.primary, custom_id="gender_homem")
    async def set_homem(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_genders[interaction.user.id] = "homem"
        await interaction.response.send_message("✅ Seu gênero foi definido como **Homem**. Agora escolha qual gênero você quer encontrar:", 
                                              view=PreferenceSetupView(), ephemeral=True)

    @discord.ui.button(label="Mulher", style=discord.ButtonStyle.primary, custom_id="gender_mulher")
    async def set_mulher(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_genders[interaction.user.id] = "mulher"
        await interaction.response.send_message("✅ Seu gênero foi definido como **Mulher**. Agora escolha qual gênero você quer encontrar:", 
                                              view=PreferenceSetupView(), ephemeral=True)

class PreferenceSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Homens", style=discord.ButtonStyle.primary, custom_id="pref_homem")
    async def pref_homem(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_preferences[interaction.user.id] = "homem"
        gender = user_genders.get(interaction.user.id, "homem")
        gender_display = get_gender_display(gender)
        
        embed = discord.Embed(
            title="🎯 Configuração Concluída!",
            description=f"Você é **{gender_display}** e quer encontrar **homens**.\n\nAgora você pode entrar na fila para conversar.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Mulheres", style=discord.ButtonStyle.primary, custom_id="pref_mulher")
    async def pref_mulher(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_preferences[interaction.user.id] = "mulher"
        gender = user_genders.get(interaction.user.id, "homem")
        gender_display = get_gender_display(gender)
        
        embed = discord.Embed(
            title="🎯 Configuração Concluída!",
            description=f"Você é **{gender_display}** e quer encontrar **mulheres**.\n\nAgora você pode entrar na fila para conversar.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Ambos", style=discord.ButtonStyle.primary, custom_id="pref_ambos")
    async def pref_ambos(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_preferences[interaction.user.id] = "ambos"
        gender = user_genders.get(interaction.user.id, "homem")
        gender_display = get_gender_display(gender)
        
        embed = discord.Embed(
            title="🎯 Configuração Concluída!",
            description=f"Você é **{gender_display}** e quer encontrar **ambos os gêneros**.\n\nAgora você pode entrar na fila para conversar.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Configurar Gênero 💘", style=discord.ButtonStyle.primary, custom_id="config_gender")
    async def config_gender(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🎯 Primeiro, escolha seu gênero:", 
                                              view=GenderSetupView(), ephemeral=True)

    @discord.ui.button(label="Entrar na Fila 💘", style=discord.ButtonStyle.success, custom_id="ticket_entrar")
    async def entrar(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        guild = interaction.guild

        if user.id not in user_genders or user.id not in user_preferences:
            await interaction.response.send_message("❌ Você precisa configurar seu gênero e preferência primeiro!", 
                                                  ephemeral=True)
            return

        if user.id in active_users:
            await interaction.response.send_message("😅 você já está em um chat ativo.", ephemeral=True)
            return
        
        # Verificar se já está na fila
        for entry in fila_carentes:
            if entry["user_id"] == user.id:
                await interaction.response.send_message("❗ você já está na fila.", ephemeral=True)
                return

        # Adicionar à fila com informações de gênero
        fila_entry = {
            "user_id": user.id,
            "gender": user_genders[user.id],
            "preference": user_preferences[user.id]
        }
        fila_carentes.append(fila_entry)
        
        gender_display = get_gender_display(user_genders[user.id])
        embed = discord.Embed(
            title="✅ Entrou na Fila",
            description=f"Você é **{gender_display}** procurando **{user_preferences[user.id]}**.\nAguarde enquanto encontramos alguém compatível para você!",
            color=discord.Color.green()
        )
        
        view_leave = LeaveQueueView(user.id)
        await interaction.response.send_message(embed=embed, view=view_leave, ephemeral=True)
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
        
        # atualiza a mesma mensagem com status
        try:
            msg = await self.canal.fetch_message(self.message_id)
            embed = discord.Embed(
                title="pecadores — confirmação",
                description=(
                    f"{self.u1.mention} {'✅' if self.u1.id in accepted else '❌'}\n"
                    f"{self.u2.mention} {'✅' if self.u2.id in accepted else '❌'}\n\n"
                    "aguardando ambos aceitarem..."
                ),
                color=discord.Color.purple()
            )
            await msg.edit(embed=embed, view=self)
        except Exception:
            pass
        
        # se ambos aceitaram
        if self.u1.id in accepted and self.u2.id in accepted:
            # permite envio de mensagens para ambos
            try:
                await self.canal.set_permissions(self.u1, send_messages=True, view_channel=True)
                await self.canal.set_permissions(self.u2, send_messages=True, view_channel=True)
            except Exception:
                pass
            
            # substitui view por encerrar e edita embed de início
            enc_view = EncerrarView(self.canal, self.u1, self.u2)
            try:
                msg = await self.canal.fetch_message(self.message_id)
                embed = discord.Embed(
                    title="conversa iniciada — pecadores",
                    description=(
                        f"{self.u1.mention} e {self.u2.mention} — a conversa foi liberada. "
                        f"vocês têm {int(CHANNEL_DURATION/60)} minutos. clique em **encerrar agora** para fechar."
                    ),
                    color=discord.Color.green()
                )
                await msg.edit(embed=embed, view=enc_view)
            except Exception:
                pass
            
            # marca início e agendar fechamento automático em CHANNEL_DURATION
            active_channels[cid]["started"] = True
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

        # aplica cooldown entre os dois para não reaparecerem por 3 minutos
        set_pair_cooldown(self.u1.id, self.u2.id)
        
        # edita a mesma mensagem mostrando recusa e fecha canal
        try:
            msg = await self.canal.fetch_message(self.message_id)
            embed = discord.Embed(
                title="conversa recusada",
                description=(
                    f"{interaction.user.mention} recusou a conversa. o canal será encerrado.\n\n"
                    "vocês poderão tentar se encontrar novamente somente após 3 minutos."
                ),
                color=discord.Color.dark_red()
            )
            await msg.edit(embed=embed, view=None)
        except Exception:
            pass
        
        # pequeno delay para permitir visualização da edição
        await asyncio.sleep(1)
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

        # edita última mensagem opcionalmente e fecha
        data = active_channels.get(self.canal.id, {})
        try:
            msg = None
            if data and data.get("message_id"):
                try:
                    msg = await self.canal.fetch_message(data["message_id"])
                except Exception:
                    msg = None
            if msg:
                embed = discord.Embed(
                    title="canal encerrado",
                    description="o canal foi encerrado pelo usuário.",
                    color=discord.Color.dark_gray()
                )
                await msg.edit(embed=embed, view=None)
        except Exception:
            pass
        
        await encerrar_canal_e_cleanup(self.canal)
        await interaction.response.send_message("canal encerrado.", ephemeral=True)

class MusicView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="⏸️ Pausar", style=discord.ButtonStyle.secondary, custom_id="music_pausar")
    async def pausar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Você só pode controlar sua própria música!", ephemeral=True)
            return
        
        # Lógica para pausar música vai aqui
        await interaction.response.send_message("⏸️ Música pausada.", ephemeral=True)

    @discord.ui.button(label="▶️ Resumir", style=discord.ButtonStyle.secondary, custom_id="music_resumir")
    async def resumir(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Você só pode controlar sua própria música!", ephemeral=True)
            return
        
        # Lógica para resumir música vai aqui
        await interaction.response.send_message("▶️ Música resumida.", ephemeral=True)

    @discord.ui.button(label="⏹️ Parar", style=discord.ButtonStyle.danger, custom_id="music_parar")
    async def parar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Você só pode controlar sua própria música!", ephemeral=True)
            return
        
        # Lógica para parar música vai aqui
        await interaction.response.send_message("⏹️ Música parada.", ephemeral=True)

# geração de nome: pecadores, pecadores-1, pecadores-2, ...
def gerar_nome_pecadores(guild: discord.Guild):
    base = CHANNEL_BASE
    existing = {c.name for c in guild.text_channels}
    if base not in existing:
        return base
    
    # encontra o menor índice livre começando por 1
    i = 1
    while True:
        candidate = f"{base}-{i}"
        if candidate not in existing:
            return candidate
        i += 1

# tentativa de formar dupla
async def tentar_formar_dupla(guild: discord.Guild):
    if len(fila_carentes) < 2:
        return

    # procura duas pessoas que sejam compatíveis
    for i in range(len(fila_carentes)):
        for j in range(i + 1, len(fila_carentes)):
            entry1 = fila_carentes[i]
            entry2 = fila_carentes[j]
            
            u1_id = entry1["user_id"]
            u2_id = entry2["user_id"]
            
            # Verificar compatibilidade de gênero
            compatible = False
            pref1 = entry1["preference"]
            pref2 = entry2["preference"]
            gender1 = entry1["gender"]
            gender2 = entry2["gender"]
            
            # Usuário 1 quer o gênero do usuário 2 OU ambos
            if pref1 == gender2 or pref1 == "ambos":
                # Usuário 2 quer o gênero do usuário 1 OU ambos
                if pref2 == gender1 or pref2 == "ambos":
                    compatible = True
            
            if not compatible:
                continue
                
            if u1_id in active_users or u2_id in active_users:
                continue
            if not can_pair(u1_id, u2_id):
                continue

            # remove ambos da fila (se ainda lá)
            try:
                fila_carentes.remove(entry1)
                fila_carentes.remove(entry2)
            except ValueError:
                pass
            
            u1 = guild.get_member(u1_id)
            u2 = guild.get_member(u2_id)
            if not u1 or not u2:
                # se não existirem mais (sairam do servidor), continua
                continue
            
            # gera nome "pecadores", "pecadores-1", ...
            nome_canal = gerar_nome_pecadores(guild)
            
            # overwrites: ambos podem ver, mas não enviar até aceitarem
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                u1: discord.PermissionOverwrite(view_channel=True, send_messages=False),
                u2: discord.PermissionOverwrite(view_channel=True, send_messages=False),
            }
            
            try:
                canal = await guild.create_text_channel(nome_canal, overwrites=overwrites, reason="canal pecadores temporário")
            except Exception:
                # se falhar, devolve os usuários à fila
                fila_carentes.append(entry1)
                fila_carentes.append(entry2)
                return
            
            # marca como ativos (impede entrar novamente na fila)
            active_users.add(u1_id)
            active_users.add(u2_id)
            active_channels[canal.id] = {
                "u1": u1_id,
                "u2": u2_id,
                "accepted": set(),
                "message_id": None,
                "created_at": time.time(),
                "started": False
            }
            
            # envia única mensagem com botões (será editada)
            gender1_display = get_gender_display(gender1)
            gender2_display = get_gender_display(gender2)
            
            embed = discord.Embed(
                title="pecadores — confirmação",
                description=(
                    f"{u1.mention} ({gender1_display}) & {u2.mention} ({gender2_display})\n\n"
                    "aguardando confirmação: ambos têm que aceitar para poderem conversar.\n\n"
                    "ninguém poderá enviar mensagens até os dois aceitarem.\n"
                    f"vocês têm {int(ACCEPT_TIMEOUT)} segundos para aceitar; caso contrário o canal será encerrado."
                ),
                color=discord.Color.purple()
            )
            view = ConversationView(canal, u1, u2, message_id=0)
            try:
                msg = await canal.send(embed=embed, view=view)
                active_channels[canal.id]["message_id"] = msg.id
                view.message_id = msg.id
            except Exception:
                # cleanup e devolver à fila
                await encerrar_canal_e_cleanup(canal)
                fila_carentes.append(entry1)
                fila_carentes.append(entry2)
                return
            
            # novo: avisa os dois por dm (mensagem privada) contendo a menção do canal
            aviso_text = f"💞 **par encontrado!** vocês foram levados para {canal.mention}"
            try:
                await u1.send(aviso_text)
            except Exception:
                # se dm falhar (usuário bloqueou dms), ignora
                pass
            try:
                await u2.send(aviso_text)
            except Exception:
                pass
            
            # iniciar timer de accept timeout (1 minuto)
            asyncio.create_task(_accept_timeout_handler(canal, timeout=ACCEPT_TIMEOUT))
            # safety close long timeout
            asyncio.create_task(_safety_close_if_no_interaction(canal, timeout=SAFETY_TIMEOUT))
            return

# timers / handlers
async def _accept_timeout_handler(canal: discord.TextChannel, timeout: int = ACCEPT_TIMEOUT):
    await asyncio.sleep(timeout)
    data = active_channels.get(canal.id)
    if not data:
        return
    
    # se ainda não foi iniciado (ou seja, não ambos aceitaram)
    if not data.get("started", False):
        accepted = data.get("accepted", set())
        if len(accepted) < 2:
            # aplica cooldown entre os dois para evitar pair imediato
            u1 = data.get("u1")
            u2 = data.get("u2")
            if u1 and u2:
                set_pair_cooldown(u1, u2)
            
            # edita mensagem para avisar timeout e fecha
            try:
                msg = await canal.fetch_message(data["message_id"])
                embed = discord.Embed(
                    title="canal encerrado (não houve aceitação)",
                    description="o tempo para aceitar expirou. o canal será encerrado.\n"
                    "vocês poderão tentar novamente após 3 minutos.",
                    color=discord.Color.dark_gray()
                )
                await msg.edit(embed=embed, view=None)
            except Exception:
                pass
            await asyncio.sleep(1)
            await encerrar_canal_e_cleanup(canal)

async def _safety_close_if_no_interaction(canal: discord.TextChannel, timeout: int = SAFETY_TIMEOUT):
    await asyncio.sleep(timeout)
    data = active_channels.get(canal.id)
    if not data:
        return
    
    # se não começou nem houve accepted, fecha por safety
    if not data.get("started", False):
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

async def _auto_close_channel_after(canal: discord.TextChannel, segundos: int):
    await asyncio.sleep(segundos)
    if canal.id not in active_channels:
        return
    try:
        data = active_channels.get(canal.id)
        if data:
            # edita mensagem final
            try:
                msg = await canal.fetch_message(data["message_id"])
                embed = discord.Embed(
                    title="tempo esgotado",
                    description="seu tempo de conversa terminou. canal encerrado.",
                    color=discord.Color.dark_gray()
                )
                await msg.edit(embed=embed, view=None)
            except Exception:
                pass
            await asyncio.sleep(1)
            await encerrar_canal_e_cleanup(canal)
    except Exception:
        pass

# eventos principais
@bot.event
async def on_ready():
    print(f"✅ {bot.user} online!")
    
    # sincroniza commands por guild para evitar inconsistências no client
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
async def on_member_join(member: discord.Member):
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
async def on_member_remove(member: discord.Member):
    for criador_id, lista in list(convites_por_usuario.items()):
        if member.id in lista:
            lista.remove(member.id)
            if not lista:
                del convites_por_usuario[criador_id]
            break

# loop de mutes
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

# on_message: anti-flood antilink repeat
@bot.event
async def on_message(message: discord.Message):
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
            deleted = await message.channel.purge(limit=100, check=lambda m: m.author.id == member.id and now - m.created_at.timestamp() <= FLOOD_WINDOW)
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

# comandos administrativos (tree) - atualizados com app_commands.describe / Range
@bot.tree.command(name="menu_admin", description="menu administrativo")
async def menu_admin(interaction: discord.Interaction):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    texto = "🧹 /clear \n🔨 /ban <usuário>\n🔇 /mute <usuário>\n🚫 /link <on|off>\n💬 /falar "
    embed = discord.Embed(title="👑 Menu Administrativo", description=texto, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clear", description="apaga mensagens")
@app_commands.describe(quantidade="quantas mensagens apagar (1-100)")
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
@app_commands.describe(usuario="usuário a banir")
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
@app_commands.describe(tempo="tempo em minutos (1-10080)", usuario="usuário a ser mutado")
async def mute(interaction: discord.Interaction, tempo: app_commands.Range[int, 1, 10080], usuario: discord.Member):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    role = await ensure_muted_role(interaction.guild)
    fim = datetime.utcnow() + timedelta(minutes=int(tempo))
    try:
        if role:
            await usuario.add_roles(role)
            mutes[usuario.id] = fim
            embed = discord.Embed(title="🔇 Usuário mutado", description=f"{usuario.mention} mutado por {tempo} minutos.", color=discord.Color.purple())
            await interaction.response.send_message(embed=embed)
    except Exception:
        await interaction.response.send_message("erro ao mutar", ephemeral=True)

@bot.tree.command(name="link", description="ativa/desativa antilink")
@app_commands.describe(estado="on ou off")
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
        await interaction.response.send_message("use on ou off.", ephemeral=True)
        return
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="falar", description="bot envia mensagem")
@app_commands.describe(mensagem="mensagem a ser enviada")
async def falar(interaction: discord.Interaction, mensagem: str):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    await interaction.response.send_message("✅ Mensagem enviada", ephemeral=True)
    try:
        await interaction.channel.send(mensagem)
    except Exception:
        pass

# comando /sairfila (resolve caso usuário 'ignorar' a msg e não consiga usar o botão)
@bot.tree.command(name="sairfila", description="sair da fila de carentes")
async def sairfila(interaction: discord.Interaction):
    uid = interaction.user.id
    # Remove da fila baseado no user_id
    for entry in list(fila_carentes):
        if entry["user_id"] == uid:
            fila_carentes.remove(entry)
            await interaction.response.send_message("você saiu da fila.", ephemeral=True)
            return
    await interaction.response.send_message("você não estava na fila.", ephemeral=True)

# comando /setupcarente (centro de tickets) - mantém o envio do painel
@bot.tree.command(name="setupcarente", description="configura o sistema de carentes (admin)")
async def setupcarente(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("🚫 apenas administradores podem usar isso", ephemeral=True)
        return
    embed = discord.Embed(
        title="💔 está se sentindo carente?",
        description="**Primeiro configure seu gênero**, depois entre na fila para conversar com alguém.\nninguém além de você verá a confirmação.",
        color=discord.Color.purple()
    )
    view = TicketView()
    try:
        sent = await interaction.channel.send(embed=embed, view=view)
        # opcional: salvar o canal de setup para compatibilidade (não usado para DM flow)
        SETUP_CHANNELS[interaction.guild.id] = interaction.channel.id
        await interaction.response.send_message("✅ sistema configurado (mensagem enviada neste canal).", ephemeral=True)
    except Exception:
        await interaction.response.send_message("erro ao enviar a mensagem de setup.", ephemeral=True)

# comando /sync (apenas admins) -> força sync das application commands no guild
@bot.tree.command(name="sync", description="sincroniza comandos (admin)")
async def sync(interaction: discord.Interaction, guild_id: int = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("🚫 apenas administradores podem usar isso", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        if guild_id:
            guild = discord.Object(id=guild_id)
            await bot.tree.sync(guild=guild)
            await interaction.followup.send(f"✅ comandos sincronizados no guild {guild_id}", ephemeral=True)
        else:
            await bot.tree.sync()
            await interaction.followup.send("✅ comandos sincronizados globalmente", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"erro ao sincronizar: {e}", ephemeral=True)

# comando para ver configuração atual
@bot.tree.command(name="meu_perfil", description="ver sua configuração atual de gênero e preferência")
async def meu_perfil(interaction: discord.Interaction):
    gender = user_genders.get(interaction.user.id)
    preference = user_preferences.get(interaction.user.id)
    
    if not gender or not preference:
        await interaction.response.send_message("❌ Você ainda não configurou seu gênero e preferência!", ephemeral=True)
        return
    
    gender_display = get_gender_display(gender)
    embed = discord.Embed(
        title="👤 Seu Perfil",
        description=f"**Gênero:** {gender_display}\n**Procurando:** {preference}",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# evento: canal deletado -> cleanup active users
@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
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

# nickname lock: monitorar alterações feitas por soberba e impedir usuários de mudarem
@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    # detectar mudança de nickname
    try:
        # se não mudou nada de nickname, pode ser outras atualizações
        if before.nick == after.nick:
            # se houver bloqueio e after.nick != blocked value, reverter silenciosamente
            b = blocked_nick.get(after.id, None)
            if b is not None:
                if after.nick != b:
                    try:
                        await after.edit(nick=b, reason="revertido por bot: apelido bloqueado por soberba")
                    except Exception:
                        pass
            return

        # se mudou nick, verificar quem mudou via audit logs
        guild = after.guild
        entry = None
        async for e in guild.audit_logs(limit=5, action=discord.AuditLogAction.member_update):
            if e.target.id == after.id:
                entry = e
                break
        if entry and entry.user:
            actor = entry.user
            # se quem alterou tem cargo soberba, bloqueamos o usuário para não poder mudar
            if tem_cargo_soberba(actor):
                # if soberba removed nick (after.nick is None), remove block
                if after.nick is None:
                    if after.id in blocked_nick:
                        del blocked_nick[after.id]
                else:
                    blocked_nick[after.id] = after.nick
            else:
                # alteração feita pelo próprio ou por outro sem soberba: se existe bloqueio, reverter
                b = blocked_nick.get(after.id, None)
                if b is not None and after.nick != b:
                    try:
                        await after.edit(nick=b, reason="revertido por bot: apelido bloqueado por soberba")
                    except Exception:
                        pass
    except Exception:
        # segurança: não deixar a função explodir
        return

# execução
if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if not token:
        print("❌ variável TOKEN não encontrada. defina TOKEN no ambiente e rode novamente.")
    else:
        bot.run(token)
