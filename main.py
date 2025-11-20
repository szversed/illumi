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

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

PAIR_COOLDOWNS = {}
PAIR_COOLDOWN_SECONDS = 3 * 60
ACCEPT_TIMEOUT = 60
CHANNEL_DURATION = 7 * 60
SAFETY_TIMEOUT = 60 * 30

antilink_ativo = True
text_mutes = {}
invite_cache = {}
convites_por_usuario = {}

user_genders = {}
user_preferences = {}

FLOOD_LIMIT = 10
FLOOD_WINDOW = 10.0
user_msg_times = defaultdict(lambda: deque())

SHORT_MSG_LIMIT = 5
SHORT_MSG_WINDOW = 10.0
user_short_msgs = defaultdict(lambda: deque())

# 🆕 SISTEMA PARA FIGURINHAS
STICKER_FLOOD_LIMIT = 8  # +8 figurinhas diferentes em 10s = MUTE
STICKER_REPEAT_LIMIT = 5  # +5 figurinhas IGUAIS em 15s = MUTE
user_sticker_times = defaultdict(lambda: deque())
user_sticker_repeats = defaultdict(list)  # Para figurinhas repetidas
last_sticker = {}  # Última figurinha enviada

last_msg = {}
last_msg_time = {}
repeat_count = defaultdict(int)
mute_level = defaultdict(int)
user_repeat_msgs = defaultdict(list)

active_users = set()
active_channels = {}

blocked_nick = {}

mute_call_ativo = False
mute_all_ativo = False

CHANNEL_BASE = "pecadores"

SETUP_CHANNELS = {}

music_queues = {}
music_players = {}

def tem_cargo_soberba(member: discord.Member) -> bool:
    try:
        return any(r.name.lower() == "soberba" for r in member.roles)
    except Exception:
        return False

def tem_cargo_ira(member: discord.Member) -> bool:
    try:
        return any(r.name.lower() == "ira" for r in member.roles)
    except Exception:
        return False

def tem_cargo_inveja(member: discord.Member) -> bool:
    try:
        return any(r.name.lower() == "inveja" for r in member.roles)
    except Exception:
        return False

def tem_cargo_boost(member: discord.Member) -> bool:
    try:
        return any(r.name.lower() == "boost" for r in member.roles)
    except Exception:
        return False

def tem_cargo_admin(member: discord.Member) -> bool:
    try:
        return any(r.name.lower() in ["soberba", "ira"] for r in member.roles)
    except Exception:
        return False

def is_exempt(member: discord.Member) -> bool:
    return member.bot or tem_cargo_admin(member)

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

def format_tempo(minutos: int) -> str:
    if minutos <= 0:
        return "0 minutos"
    
    dias = minutos // 1440
    horas = (minutos % 1440) // 60
    mins = minutos % 60
    
    partes = []
    if dias > 0:
        partes.append(f"{dias} dia{'s' if dias > 1 else ''}")
    if horas > 0:
        partes.append(f"{horas} hora{'s' if horas > 1 else ''}")
    if mins > 0:
        partes.append(f"{mins} minuto{'s' if mins > 1 else ''}")
    
    return " e ".join(partes)

async def aplicar_mute_texto(guild: discord.Guild, member: discord.Member, minutos: int, motivo: str = None, canal_log: discord.TextChannel = None):
    fim = datetime.utcnow() + timedelta(minutes=minutos)
    
    canais_afetados = 0
    for canal in guild.text_channels:
        try:
            await canal.set_permissions(member, send_messages=False)
            canais_afetados += 1
        except Exception:
            pass
    
    text_mutes[member.id] = fim
    
    if canal_log:
        tempo_formatado = format_tempo(minutos)
        embed = discord.Embed(
            title="🔇 Mute de Texto Aplicado",
            description=f"{member.mention} mutado em texto por {tempo_formatado}.\n{canais_afetados} canais afetados.\nMotivo: {motivo}",
            color=discord.Color.purple(),
            timestamp=datetime.utcnow()
        )
        try:
            await canal_log.send(embed=embed)
        except Exception:
            pass

async def remover_mute_texto(guild: discord.Guild, member: discord.Member, canal_log: discord.TextChannel = None):
    canais_afetados = 0
    for canal in guild.text_channels:
        try:
            await canal.set_permissions(member, send_messages=None)
            canais_afetados += 1
        except Exception:
            pass
    
    if member.id in text_mutes:
        del text_mutes[member.id]
    
    if canal_log:
        embed = discord.Embed(
            title="🔊 Mute de Texto Removido",
            description=f"{member.mention} teve o mute de texto removido.\n{canais_afetados} canais afetados.",
            color=discord.Color.green(),
            timestamp=datetime.utcnow()
        )
        await canal_log.send(embed=embed)

async def aplicar_mute_call(guild: discord.Guild, voice_channel: discord.VoiceChannel, motivo: str, canal_log: discord.TextChannel = None):
    contador = 0
    for member in voice_channel.members:
        if not is_exempt(member):
            try:
                await member.edit(mute=True)
                contador += 1
            except Exception:
                pass
    
    if canal_log and contador > 0:
        embed = discord.Embed(
            title="🔇 MUTE EM CALL APLICADO",
            description=f"Mute aplicado no canal de voz {voice_channel.mention}.\n{contador} membros mutados.\nMotivo: {motivo}",
            color=discord.Color.orange(),
            timestamp=datetime.utcnow()
        )
        await canal_log.send(embed=embed)

async def remover_mute_call(guild: discord.Guild, voice_channel: discord.VoiceChannel, canal_log: discord.TextChannel = None):
    contador = 0
    for member in voice_channel.members:
        try:
            await member.edit(mute=False)
            contador += 1
        except Exception:
            pass
    
    if canal_log and contador > 0:
        embed = discord.Embed(
            title="🔊 MUTE EM CALL REMOVIDO",
            description=f"Mute removido no canal de voz {voice_channel.mention}.\n{contador} membros desmutados.",
            color=discord.Color.green(),
            timestamp=datetime.utcnow()
        )
        await canal_log.send(embed=embed)

async def bloquear_todos_canais_texto(guild: discord.Guild, motivo: str):
    canais_bloqueados = 0
    canais_protegidos = ["mod-logs"]
    
    for canal in guild.text_channels:
        if canal.name.lower() not in canais_protegidos:
            try:
                await canal.set_permissions(guild.default_role, send_messages=False)
                canais_bloqueados += 1
            except Exception:
                pass
    
    return canais_bloqueados

async def desbloquear_todos_canais_texto(guild: discord.Guild):
    canais_desbloqueados = 0
    
    for canal in guild.text_channels:
        try:
            await canal.set_permissions(guild.default_role, send_messages=None)
            canais_desbloqueados += 1
        except Exception:
            pass
    
    return canais_desbloqueados

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

class MusicView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="⏸️ Pausar", style=discord.ButtonStyle.secondary, custom_id="music_pausar")
    async def pausar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Você só pode controlar sua própria música!", ephemeral=True)
            return
        
        await interaction.response.send_message("⏸️ Música pausada.", ephemeral=True)

    @discord.ui.button(label="▶️ Resumir", style=discord.ButtonStyle.secondary, custom_id="music_resumir")
    async def resumir(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Você só pode controlar sua própria música!", ephemeral=True)
            return
        
        await interaction.response.send_message("▶️ Música resumida.", ephemeral=True)

    @discord.ui.button(label="⏹️ Parar", style=discord.ButtonStyle.danger, custom_id="music_parar")
    async def parar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Você só pode controlar sua própria música!", ephemeral=True)
            return
        
        await interaction.response.send_message("⏹️ Música parada.", ephemeral=True)

def gerar_nome_pecadores(guild: discord.Guild):
    base = CHANNEL_BASE
    existing = {c.name for c in guild.text_channels}
    if base not in existing:
        return base
    
    i = 1
    while True:
        candidate = f"{base}-{i}"
        if candidate not in existing:
            return candidate
        i += 1

async def tentar_formar_dupla(guild: discord.Guild):
    pass

async def _accept_timeout_handler(canal: discord.TextChannel, timeout: int = ACCEPT_TIMEOUT):
    pass

async def _safety_close_if_no_interaction(canal: discord.TextChannel, timeout: int = SAFETY_TIMEOUT):
    pass

async def _auto_close_channel_after(canal: discord.TextChannel, segundos: int):
    pass

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
    if not verificar_text_mutes.is_running():
        verificar_text_mutes.start()
    print("🔁 loop de mutes de texto iniciado.")

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

@tasks.loop(seconds=30)
async def verificar_text_mutes():
    agora = datetime.utcnow()
    expirados = [user_id for user_id, fim in list(text_mutes.items()) if agora >= fim]
    
    for user_id in expirados:
        for guild in bot.guilds:
            member = guild.get_member(user_id)
            if member:
                canal_log = discord.utils.get(guild.text_channels, name="mod-logs")
                await remover_mute_texto(guild, member, canal_log)
                try:
                    del text_mutes[user_id]
                except KeyError:
                    pass

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

    # 🆕 VERIFICAÇÃO DE SPAM DE FIGURINHAS
    if message.stickers:
        # 🚨 Anti-Flood: Muitas figurinhas diferentes em pouco tempo
        sticker_dq = user_sticker_times[member.id]
        sticker_dq.append(now)
        
        # Limpa figurinhas antigas (10 segundos)
        while sticker_dq and now - sticker_dq[0] > FLOOD_WINDOW:
            sticker_dq.popleft()
        
        # Se +8 figurinhas em 10 segundos = MUTE
        if len(sticker_dq) >= STICKER_FLOOD_LIMIT:
            nivel = mute_level.get(member.id, 0)
            minutos = 15 if nivel == 0 else 30 if nivel == 1 else 60
            mute_level[member.id] = min(nivel + 1, 3)
            motivo = f"spam de figurinhas ({len(sticker_dq)} em {FLOOD_WINDOW}s)"
            
            # Tenta deletar as figurinhas recentes
            try:
                async for msg in message.channel.history(limit=20):
                    if msg.author.id == member.id and msg.stickers:
                        try:
                            await msg.delete()
                        except Exception:
                            pass
            except Exception:
                pass
            
            await aplicar_mute_texto(message.guild, member, minutos, motivo, canal_log)
            user_sticker_times.pop(member.id, None)
            
            embed = discord.Embed(
                description=f"🚫 {member.mention} mutado por {minutos}min por spam de figurinhas.", 
                color=discord.Color.red()
            )
            try:
                await message.channel.send(embed=embed, delete_after=10)
            except Exception:
                pass
            return
        
        # 🚨 Anti-Repetição: Figurinhas IGUAIS seguidas (igual mensagens)
        current_sticker_id = message.stickers[0].id if message.stickers else None
        
        if current_sticker_id:
            # Adiciona à lista de figurinhas recentes
            user_sticker_repeats[member.id].append({
                'sticker_id': current_sticker_id,
                'timestamp': now,
                'message': message
            })
            
            # Remove figurinhas antigas (15 segundos)
            user_sticker_repeats[member.id] = [
                s for s in user_sticker_repeats[member.id] 
                if now - s['timestamp'] <= 15.0
            ]
            
            # Conta quantas vezes a MESMA figurinha apareceu nos últimos 15s
            same_sticker_count = sum(
                1 for s in user_sticker_repeats[member.id] 
                if s['sticker_id'] == current_sticker_id
            )
            
            # Se 5+ figurinhas IGUAIS em 15 segundos = MUTE
            if same_sticker_count >= STICKER_REPEAT_LIMIT:
                nivel = mute_level.get(member.id, 0)
                minutos = 5 if nivel == 0 else 10 if nivel == 1 else 20
                mute_level[member.id] = min(nivel + 1, 3)
                motivo = f"repetição de figurinhas ({same_sticker_count}x a mesma em 15s)"
                
                # Deleta todas as figurinhas repetidas
                for sticker_data in user_sticker_repeats[member.id]:
                    if sticker_data['sticker_id'] == current_sticker_id:
                        try:
                            await sticker_data['message'].delete()
                        except Exception:
                            pass
                
                await aplicar_mute_texto(message.guild, member, minutos, motivo, canal_log)
                
                # Limpa os dados
                user_sticker_repeats[member.id] = []
                last_sticker[member.id] = None
                
                embed = discord.Embed(
                    description=f"🚫 {member.mention} mutado por {minutos}min por repetir a mesma figurinha {same_sticker_count}x.", 
                    color=discord.Color.red()
                )
                try:
                    await message.channel.send(embed=embed, delete_after=10)
                except Exception:
                    pass
                return

    if "discord.gg/" in message.content.lower() or "discord.com/invite/" in message.content.lower():
        invite_regex = r'(?:discord\.gg\/|discord\.com\/invite\/)([a-zA-Z0-9]+)'
        matches = re.findall(invite_regex, message.content)
        is_own_server_invite = any(match == "3dpxCUAWxn" for match in matches)
        
        if not is_own_server_invite:
            try:
                await message.delete()
            except Exception:
                pass
            minutos = 60
            motivo = "Tentativa de enviar convite de outro servidor"
            canal_log = discord.utils.get(message.guild.text_channels, name="mod-logs")
            await aplicar_mute_texto(message.guild, member, minutos, motivo, canal_log)
            tempo_formatado = format_tempo(minutos)
            embed = discord.Embed(
                description=f"🚫 {member.mention}, você foi mutado por {tempo_formatado} por enviar um convite de outro servidor.", 
                color=discord.Color.red()
            )
            try:
                await message.channel.send(embed=embed, delete_after=10)
            except Exception:
                pass
            return 
    
    if tem_cargo_inveja(member) or tem_cargo_boost(member):
        await bot.process_commands(message)
        return

    if member.id in text_mutes:
        try:
            await message.delete()
        except Exception:
            pass
        return

    is_command = message.content.startswith("!") or message.content.startswith("/")

    dq = user_msg_times[member.id]
    
    if is_command:
        dq.append(now)
    
    while dq and now - dq[0] > FLOOD_WINDOW:
        dq.popleft()
        
    if len(dq) > FLOOD_LIMIT: 
        try:
            deleted = await message.channel.purge(
                limit=100, 
                check=lambda m: m.author.id == member.id and now - m.created_at.timestamp() <= FLOOD_WINDOW
            )
        except Exception:
            deleted = []
            
        try:
            await message.guild.ban(member, reason=f"Spam de comandos: >{FLOOD_LIMIT} comandos em {FLOOD_WINDOW}s")
            try:
                await message.channel.send(f"🔨 {member.mention} banido por spam de comandos. {len(deleted)} mensagens apagadas.", delete_after=7)
            except Exception:
                pass
        except Exception:
            if canal_log:
                try:
                    await canal_log.send(f"⚠️ Tentativa de ban automático por spam de comandos falhou para {member.mention}.")
                except Exception:
                    pass
        finally:
            user_msg_times.pop(member.id, None)
        return

    # 🚨 Lógica de Anti-Mensagens Curtas (+5 mensagens com <3 caracteres em 10s = Mute)
    dq_short = user_short_msgs[member.id]

    content_clean = message.content.strip()
    if len(content_clean) < 3 and content_clean != "":
        dq_short.append(now)

    while dq_short and now - dq_short[0] > SHORT_MSG_WINDOW:
        dq_short.popleft()

    if len(dq_short) >= SHORT_MSG_LIMIT:
        nivel = mute_level.get(member.id, 0)
        minutos = 5 if nivel == 0 else 10 if nivel == 1 else 20
        mute_level[member.id] = min(nivel + 1, 3)
        motivo = f"muitas mensagens curtas ({len(dq_short)}x) - nível {mute_level[member.id]}"
        
        try:
            async for msg in message.channel.history(limit=50):
                if msg.author.id == member.id and len(msg.content.strip()) < 3 and msg.content.strip() != "":
                    try:
                        await msg.delete()
                    except Exception:
                        pass
        except Exception:
            pass
        
        await aplicar_mute_texto(message.guild, member, minutos, motivo, canal_log)
        user_short_msgs.pop(member.id, None)
        
        embed = discord.Embed(
            description=f"🚫 {member.mention} mutado por {minutos}min por spam de mensagens curtas.", 
            color=discord.Color.red()
        )
        try:
            await message.channel.send(embed=embed, delete_after=10)
        except Exception:
            pass
        return

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

    # 🚨 Lógica de Repetição (5 mensagens iguais em 15 segundos = Mute)
    conteudo = re.sub(r'\s+', ' ', message.content.strip().lower())
    prev = last_msg.get(member.id)
    user_repeat_msgs[member.id].append(message)

    # Atualiza timestamp da última mensagem
    last_msg_time[member.id] = now

    # Remove mensagens antigas da lista de repetição (acima de 15 segundos)
    user_repeat_msgs[member.id] = [msg for msg in user_repeat_msgs[member.id] if now - msg.created_at.timestamp() <= 15.0]

    # Conta apenas mensagens dentro da janela de 15 segundos
    recent_repeats = 0
    if prev and conteudo != "":
        if conteudo == prev:
            # Conta quantas mensagens iguais tem nos últimos 15 segundos
            for msg in user_repeat_msgs[member.id]:
                msg_content = re.sub(r'\s+', ' ', msg.content.strip().lower())
                if msg_content == conteudo:
                    recent_repeats += 1
        else:
            # Nova mensagem diferente, reinicia contagem
            recent_repeats = 1
            last_msg[member.id] = conteudo
            user_repeat_msgs[member.id] = [message]
    else:
        recent_repeats = 1
        last_msg[member.id] = conteudo
        user_repeat_msgs[member.id] = [message]

    # Aplica mute se tiver 5 mensagens iguais nos últimos 15 segundos
    if recent_repeats >= 5:
        if member.id not in mute_level:
            minutos = 5
            mute_level[member.id] = 1
        else:
            minutos = 50
            mute_level[member.id] = 2
        
        motivo = f"repetição ({recent_repeats}x em 15s)"
        
        # Deleta todas as mensagens repetidas recentes
        for msg_to_delete in user_repeat_msgs[member.id]:
            try:
                await msg_to_delete.delete()
            except Exception:
                pass
        
        await aplicar_mute_texto(message.guild, member, minutos, motivo, canal_log)
        
        # Limpa os dados de repetição
        repeat_count[member.id] = 0
        last_msg[member.id] = None
        user_repeat_msgs[member.id] = []
        
        embed = discord.Embed(
            description=f"🚫 {member.mention} mutado por {minutos}min por repetir a mesma mensagem {recent_repeats}x.", 
            color=discord.Color.red()
        )
        try:
            await message.channel.send(embed=embed, delete_after=10)
        except Exception:
            pass
        return

    await bot.process_commands(message)

@bot.tree.command(name="menu_admin", description="menu administrativo")
async def menu_admin(interaction: discord.Interaction):
    if not tem_cargo_admin(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    texto = "🧹 /clear \n🔨 /ban <usuário(s)>\n🔇 /mute <usuário(s)>\n🚫 /link <on|off>\n💬 /falar \n🔊 /mutecall <on|off>\n🌐 /muteall <on|off>"
    embed = discord.Embed(title="👑 Menu Administrativo", description=texto, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clear", description="apaga mensagens")
@app_commands.describe(quantidade="quantas mensagens apagar (1-100)")
async def clear(interaction: discord.Interaction, quantidade: int):
    if not tem_cargo_admin(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=quantidade)
        embed = discord.Embed(title="🧹 Limpeza concluída", description=f"{len(deleted)} mensagens apagadas", color=discord.Color.dark_gray())
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception:
        await interaction.followup.send("erro ao apagar mensagens", ephemeral=True)

@bot.tree.command(name="ban", description="bane usuário(s)")
@app_commands.describe(usuario="usuário a banir (múltiplos se for soberba)")
@app_commands.rename(usuario="usuario")
async def ban(interaction: discord.Interaction, usuario: str):
    if not tem_cargo_admin(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    
    membros_alvo = []
    
    if tem_cargo_soberba(interaction.user):
        mencoes = re.findall(r'<@!?(\d+)>', usuario)
        if not mencoes:
            await interaction.response.send_message("❌ Soberba: Você deve mencionar um ou mais usuários.", ephemeral=True)
            return
        
        for user_id in mencoes:
            member = interaction.guild.get_member(int(user_id))
            if member:
                membros_alvo.append(member)
    else:
        mencoes = re.findall(r'<@!?(\d+)>', usuario)
        if len(mencoes) != 1:
            await interaction.response.send_message("❌ Ira: Você deve mencionar exatamente um usuário.", ephemeral=True)
            return
        
        member = interaction.guild.get_member(int(mencoes[0]))
        if member:
            membros_alvo.append(member)
        else:
            await interaction.response.send_message("❌ Usuário não encontrado.", ephemeral=True)
            return

    if not membros_alvo:
        await interaction.response.send_message("❌ Nenhum usuário válido encontrado para banir.", ephemeral=True)
        return

    banidos = []
    erros = []
    
    for membro in membros_alvo:
        try:
            await interaction.guild.ban(membro, reason=f"Banido por {interaction.user}")
            banidos.append(membro.mention)
        except Exception:
            erros.append(membro.mention)

    if banidos:
        embed = discord.Embed(title="🔨 Banido(s)", description=f"{', '.join(banidos)} foram banidos.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message("❌ Erro ao banir todos os usuários mencionados.", ephemeral=True)

@bot.tree.command(name="mute", description="mute usuário(s)")
@app_commands.describe(tempo="tempo em minutos (1-10080)", usuario="usuário a ser mutado (múltiplos se for soberba)")
@app_commands.rename(usuario="usuario")
async def mute(interaction: discord.Interaction, tempo: app_commands.Range[int, 1, 10080], usuario: str):
    if not tem_cargo_admin(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    
    membros_alvo = []
    
    if tem_cargo_soberba(interaction.user):
        mencoes = re.findall(r'<@!?(\d+)>', usuario)
        if not mencoes:
            await interaction.response.send_message("❌ Soberba: Você deve mencionar um ou mais usuários.", ephemeral=True)
            return
        
        for user_id in mencoes:
            member = interaction.guild.get_member(int(user_id))
            if member:
                membros_alvo.append(member)
    else:
        mencoes = re.findall(r'<@!?(\d+)>', usuario)
        if len(mencoes) != 1:
            await interaction.response.send_message("❌ Ira: Você deve mencionar exatamente um usuário.", ephemeral=True)
            return
        
        member = interaction.guild.get_member(int(mencoes[0]))
        if member:
            membros_alvo.append(member)
        else:
            await interaction.response.send_message("❌ Usuário não encontrado.", ephemeral=True)
            return

    if not membros_alvo:
        await interaction.response.send_message("❌ Nenhum usuário válido encontrado para mutar.", ephemeral=True)
        return

    canal_log = discord.utils.get(interaction.guild.text_channels, name="mod-logs")
    tempo_formatado = format_tempo(tempo)
    mutados = []
    
    for membro in membros_alvo:
        try:
            await aplicar_mute_texto(interaction.guild, membro, tempo, f"Comando por {interaction.user}", canal_log)
            mutados.append(membro.mention)
        except Exception:
            pass

    if mutados:
        embed = discord.Embed(
            title="🔇 Usuário(s) mutado(s) em texto", 
            description=f"{', '.join(mutados)} mutado(s) por {tempo_formatado}.\nO(s) usuário(s) não poderá(ão) enviar mensagens em nenhum canal de texto.",
            color=discord.Color.purple()
        )
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message("❌ Erro ao mutar os usuários mencionados.", ephemeral=True)

@bot.tree.command(name="link", description="ativa/desativa antilink")
@app_commands.describe(estado="on ou off")
async def link(interaction: discord.Interaction, estado: str):
    global antilink_ativo
    if not tem_cargo_admin(interaction.user):
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
    if not tem_cargo_admin(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    await interaction.response.send_message("✅ Mensagem enviada", ephemeral=True)
    try:
        await interaction.channel.send(mensagem)
    except Exception:
        pass

@bot.tree.command(name="mutecall", description="muta/desmuta todos em call")
@app_commands.describe(estado="on ou off")
async def mutecall(interaction: discord.Interaction, estado: str):
    global mute_call_ativo
    if not tem_cargo_admin(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    
    canal_log = discord.utils.get(interaction.guild.text_channels, name="mod-logs")
    
    if estado.lower() == "on":
        mute_call_ativo = True
        for voice_channel in interaction.guild.voice_channels:
            await aplicar_mute_call(interaction.guild, voice_channel, f"Comando por {interaction.user}", canal_log)
        embed = discord.Embed(title="🔇 Mute em Call Ativado", color=discord.Color.orange())
    elif estado.lower() == "off":
        mute_call_ativo = False
        for voice_channel in interaction.guild.voice_channels:
            await remover_mute_call(interaction.guild, voice_channel, canal_log)
        embed = discord.Embed(title="🔊 Mute em Call Desativado", color=discord.Color.green())
    else:
        await interaction.response.send_message("use on ou off.", ephemeral=True)
        return
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="muteall", description="muta/desmuta todos os canais de texto")
@app_commands.describe(estado="on ou off")
async def muteall(interaction: discord.Interaction, estado: str):
    global mute_all_ativo
    if not tem_cargo_admin(interaction.user):
        await interaction.response.send_message("🚫 sem permissão", ephemeral=True)
        return
    
    if estado.lower() == "on":
        mute_all_ativo = True
        canais_bloqueados = await bloquear_todos_canais_texto(interaction.guild, f"Comando por {interaction.user}")
        embed = discord.Embed(
            title="🌐 MUTEALL ATIVADO", 
            description=f"Todos os canais de texto foram bloqueados.\n{canais_bloqueados} canais afetados.",
            color=discord.Color.dark_red()
        )
    elif estado.lower() == "off":
        mute_all_ativo = False
        canais_desbloqueados = await desbloquear_todos_canais_texto(interaction.guild)
        embed = discord.Embed(
            title="🌐 MUTEALL DESATIVADO", 
            description=f"Todos os canais de texto foram desbloqueados.\n{canais_desbloqueados} canais afetados.",
            color=discord.Color.green()
        )
    else:
        await interaction.response.send_message("use on ou off.", ephemeral=True)
        return
    await interaction.response.send_message(embed=embed, ephemeral=True)

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

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if mute_call_ativo and after.channel and not is_exempt(member):
        try:
            await member.edit(mute=True)
        except Exception:
            pass

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    try:
        if before.nick == after.nick:
            b = blocked_nick.get(after.id, None)
            if b is not None:
                if after.nick != b:
                    try:
                        await after.edit(nick=b, reason="revertido por bot: apelido bloqueado por soberba")
                    except Exception:
                        pass
            return

        guild = after.guild
        entry = None
        async for e in guild.audit_logs(limit=5, action=discord.AuditLogAction.member_update):
            if e.target.id == after.id:
                entry = e
                break
        if entry and entry.user:
            actor = entry.user
            if tem_cargo_soberba(actor):
                if after.nick is None:
                    if after.id in blocked_nick:
                        del blocked_nick[after.id]
                else:
                    blocked_nick[after.id] = after.nick
            else:
                b = blocked_nick.get(after.id, None)
                if b is not None and after.nick != b:
                    try:
                        await after.edit(nick=b, reason="revertido por bot: apelido bloqueado por soberba")
                    except Exception:
                        pass
    except Exception:
        return

if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if not token:
        print("❌ variável TOKEN não encontrada. defina TOKEN no ambiente e rode novamente.")
    else:
        bot.run(token)
