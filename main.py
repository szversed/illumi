import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
from datetime import datetime, timedelta

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

antilink_ativo = True
mutes = {}
invite_cache = {}
convites_por_usuario = {}  # {id_inviter: [ids_de_convidados_que_ainda_estão]}

# -------------------------
# funções
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
    print(f"✅ {bot.user} está online!")

    # sincroniza comandos globais apenas uma vez
    try:
        synced = await bot.tree.sync()
        print(f"✅ {len(synced)} comandos sincronizados globalmente.")
    except Exception as e:
        print(f"erro ao sincronizar comandos: {e}")

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

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if antilink_ativo and ("http://" in message.content or "https://" in message.content):
        await message.delete()
        embed = discord.Embed(description=f"🚫 {message.author.mention}, links não são permitidos!", color=discord.Color.red())
        await message.channel.send(embed=embed, delete_after=5)
    await bot.process_commands(message)

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
# comandos administrativos globais
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
